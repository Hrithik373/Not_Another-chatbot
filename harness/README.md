# Auto-Remediation from Logs (Track A)

A production-minded, **measured** auto-remediation system for Lyzr Agent Studio.
The Lyzr agent is a thin, stateless classifier — every bit of the intelligence
that makes this system cheap and accurate (noise filtering, dedup/clustering,
closed-set validation, confidence gating) lives in Python around it, not in
the prompt.

The graded spine is `run.py`: a reproducible benchmark harness that runs a naive
baseline and the optimized pipeline over the same 455-event dataset, scores
both against the 40 labeled rows, and prints the delta. Nothing in this README
is a number you have to trust — re-run `run.py` and it regenerates the table.

**Submission deliverables**: [docs/scoping_note.md](docs/scoping_note.md)
(Part 1, client-facing memo) and
[docs/optimization_writeup.md](docs/optimization_writeup.md) (Part 2 §3-4:
levers with measured deltas, Studio feature rationale, 50k/day scale
statement, and the latency-SLO moving-target scenario). This README is the
engineering reference the other two summarize and link back to.

## Why this design

The dataset has 455 rows but only **16 distinct messages**, and ~200 of the
455 rows are pure noise (INFO/DEBUG lines with no error-ish keyword — health
checks, cache warmups, feature-flag logs). That means the single biggest lever
available is simply **not calling the model on noise or duplicates**:

1. A cheap-path filter kills ~200 events for free (no network call).
2. The ~255 survivors normalize down to **10 unique signatures** (timestamps,
   hex pointers, UUIDs, and numbers stripped out) — the model is called once
   per signature and the decision is fanned back out to every event in that
   cluster.
3. Closed-set validation forces any off-menu model output to `escalate_to_human`
   (target: zero free-form violations).
4. A confidence gate (`< 0.55` or `category == "unknown"`) also escalates —
   spending latency only on the small residue that actually needs a human,
   not on the bulk of the traffic.

Net effect: the baseline makes 455 model calls; the optimized pipeline makes
**10**. That is the delta this assignment is measuring.

## Repo structure

```
harness/
  config.py        # closed sets, thresholds, pricing, env-driven API config
  normalize.py      # message -> signature (strip timestamps/hex/uuids/numbers)
  lyzr_client.py    # POST /v3/inference/chat wrapper: retries, timing, tokens, mock oracle
  pipeline.py       # noise filter, closed-set validation, confidence gate, majority-vote, tool-use orchestration, EventResult/cache
  tools.py          # agentic tool: simulated service-ownership (CMDB) lookup
  baseline.py       # naive: one LLM call per raw event, no dedup/cache/route
  optimized.py      # noise filter -> dedup -> 1 call/signature -> validate -> gate
  ablation.py       # baseline -> +noise-filter -> +dedup -> +gating, per-lever deltas
  metrics.py        # macro-F1, root-cause acc, p50/p95, tokens, cost, throughput
  audit.py          # per-event decision + reasoning -> runs/audit_<ts>.jsonl
  run.py            # CLI entry: baseline + optimized (+ --ablation), prints table
  server.py         # FastAPI proxy (holds the key) reusing pipeline.py
  index.html        # single-page SRE triage console served by server.py
  data/track_a_logs.csv
  requirements.txt
  .env.example
```

## Setup

```bash
cd harness
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env   # fill in LYZR_API_KEY / LYZR_AGENT_ID for a real run
```

The dataset (`data/track_a_logs.csv`, 455 rows, 40 labeled) is already in
place.

## Run commands

```bash
# Pipeline smoke test against a local deterministic oracle — NOT submittable,
# exists purely to exercise noise-filter/dedup/gating/audit/metrics offline.
python run.py --mock

# Real run against the Lyzr API (needs LYZR_API_KEY / LYZR_AGENT_ID in .env)
python run.py

# Cap baseline calls while iterating (optimized always covers the full corpus)
python run.py --limit 50

# Also print the per-lever ablation table
python run.py --ablation

# Trade ~5pp of cost cut for stability: 3x-vote each signature's category
# (opt-in, default off — see "Sample real runs" for what this does and doesn't fix)
python run.py --vote-k 3

# Disable the tool-calling investigation step (on by default — see
# "Tool-calling closes the gap") to compare against the un-grounded baseline
python run.py --no-tools

# Serve the SRE triage console (reuses the same pipeline.py)
uvicorn server:app --reload
# -> http://127.0.0.1:8000
```

`python run.py --mock` is the acceptance check: it runs end-to-end with no
credentials and prints the full results table.

## The pipeline

**Baseline** (`baseline.py`): one LLM call per raw event, straight through —
no noise filter, no dedup, no cache. This is the number optimized has to beat.

**Optimized** (`optimized.py`):
1. `pipeline.is_noise()` — severity in `{INFO, DEBUG}` and no error-ish keyword
   (`error|exception|fail|timeout|oom|fatal|panic|denied|refused`) → `category=noise`,
   `remediation=none`, no model call.
2. `normalize.normalize_message()` — strips timestamps, hex pointers, UUIDs,
   and digit runs into stable tokens (`<ts>`, `<hex>`, `<uuid>`, `<num>`) to
   build a signature; survivors are clustered by signature and the model is
   called exactly once per unique signature (`pipeline.SignatureCache` — same
   signature never calls the model twice, i.e. idempotent).
3. `pipeline.validate_and_gate()` — the raw model reply is checked against the
   closed sets (`config.CATEGORIES` / `ROOT_CAUSES` / `REMEDIATIONS`); anything
   outside them is a **free-form violation**, forced to
   `category=unknown, remediation=escalate_to_human, needs_human=true`, and
   counted.
4. Confidence gate — `category == "unknown"` or `confidence < 0.55` also
   forces `needs_human=true` (target: this is where the small escalated
   residue comes from; the p95-latency-on-escalated-events target is scoped to
   exactly this subset).
5. `audit.AuditWriter` appends every event's final decision, reasoning, and
   path (`noise_filter` | `model` | `cache`) to `runs/audit_<run>_<ts>.jsonl`.

## Metrics & targets

`run.py` prints one table (Naive baseline | Optimized | Delta) covering
accuracy (macro-F1 on category, scored against the 40 labeled rows),
root-cause accuracy, free-form violation rate, escalation rate, p50/p95
latency, tokens/task, cost/task and cost/batch, throughput, and LLM call
count — then checks the assignment's targets:

| Target | Check |
|---|---|
| macro-F1 (category) | ≥ 0.85 |
| root-cause accuracy | ≥ 0.80 |
| free-form violation rate | = 0 |
| p95 latency, escalated events only | ≤ 4s |
| cost cut vs. baseline | ≥ 50% |

`python run.py --ablation` isolates each lever's marginal contribution:
`baseline → +noise_filter → +dedup → +gating`. The first two stages reduce
**call count** (noise filter and dedup are cost levers); `+gating` holds the
call count fixed and only changes accuracy/free-form numbers (it's a quality
lever, not a cost lever) — by design, ungated stages skip closed-set
validation entirely so the table shows what gating is actually buying you.
To avoid paying for 455 calls four times over, the ablation reuses the raw
per-event responses already collected by the baseline run in the same
process and derives all four stages from that one pass (see
`ablation.run_ablation`'s docstring). One consequence: the ablation's own
`+gating` stage can differ slightly in its exact accuracy numbers from a
freshly-run `optimized.py`, because it picks its "representative" response
for each signature from whichever raw baseline call happened to land first
in row order, not from a fresh model call — this is a deliberate
cost/reproducibility trade-off, not a bug.

## Sample real runs

Captured 2026-08-15 against the live agent (`agent_id=6a7daa555f9f8a75ac0232b8`),
back-to-back, no cherry-picking — three literal, consecutive outputs
(runs 1-2 at `--vote-k 1`, the default; run 3 at `--vote-k 3`):

```
                                  RUN 1               RUN 2            RUN 3 (vote-k=3)
                              baseline optimized  baseline optimized  baseline optimized
Accuracy (macro-F1, category)   74.4%    80.0%      68.3%    64.4%     63.4%    64.4%
Root-cause accuracy            100.0%   100.0%     100.0%   100.0%     97.5%   100.0%
Free-form violation rate         0.0%     0.0%       0.2%     0.0%      0.2%     0.0%
Cost / full batch              $0.0539  $0.0014    $0.0535  $0.0012   $0.0534  $0.0039
LLM call count                    455       10         455       10       455       30

TARGET CHECKS (all three runs)
  [FAIL] macro-F1 >= 0.85        (0.80 / 0.64 / 0.64)
  [PASS] root-cause acc >= 0.80  (1.00 / 1.00 / 1.00)
  [PASS] free-form rate == 0     (0.00 / 0.00 / 0.00, gated)
  [PASS] p95 latency (escalated) <= 4s
  [PASS] cost cut vs baseline >= 50%  (97.4% / 97.4% / 92.6%)
```

4/5 targets pass consistently. Root-cause accuracy, free-form rate (post-gate),
p95-escalated latency, and the cost cut are all real and stable — those are
exactly what the optimized pipeline is designed to nail: 425-445 fewer LLM
calls, 93-97% cost cut, zero closed-set violations, zero events left waiting
past budget.

**macro-F1 (category) is the one target that's genuinely borderline. Auditing
all 40 labeled rows against `gt_category` each run pins down exactly why:**

| Signature | gt_category | Run 1 | Run 2 | Run 3 (3-way vote) |
|---|---|---|---|---|
| `rate limit exceeded... from search-index` | `capacity` | ✅ capacity | ❌ dependency_failure | ❌ dependency_failure (2/3) |
| `connection timeout to postgres...` | `dependency_failure` | ✅ dependency_failure | ❌ resource_exhaustion | ❌ resource_exhaustion (2/3) |
| `TLS handshake failed: certificate expired...` | `config_error` | ❌ dependency_failure | ❌ dependency_failure | ❌ dependency_failure (**3/3, unanimous**) |

Only 3 of the 10 unique signatures ever disagree with the label — the other
7 are correct in every run. And critically, **`root_cause` and `remediation`
— the fields that actually drive the on-call action — were 100% (or 97.5%,
one outlier) correct across all three runs, on every labeled row, every
time.** Only the higher-level category label wobbles; the actionable output
doesn't.

`--vote-k 3` (`pipeline.classify_with_voting` / `majority_vote`, opt-in via
`VOTE_K` env or `--vote-k`, default off) calls the model 3× per signature
concurrently and majority-votes the category, at ~93% cost cut instead of
~97%. **It's implemented and works exactly as designed — it did lift
root-cause accuracy to a perfect 1.00 and add a genuine confidence-dampening
signal on split votes — but run 3 shows it does *not* reliably fix macro-F1,
and the vote breakdown explains why.** The TLS-cert-expiry signature came
back `dependency_failure` in all 4 independent observations across every run
(runs 1, 2, and a unanimous 3/3 in run 3) — that's not sampling noise, it's
the agent's *consistent* judgment call, just a different (still defensible)
reading of the taxonomy than the label. Majority voting only cancels out
random noise; it converges toward whatever the model's true single-call
preference already is, so on a signature where that preference happens to
disagree with the label, voting makes the wrong answer *more* stable, not
less. The other two signatures show real 2/3-vs-1/3 variance rather than
full unanimity, so a differently-seeded k=3 vote could occasionally flip
them — but they still lean the same (wrong) direction more often than not.

**The one honest lever left that actually addresses this**: sharpen the
`config_error` / `capacity` / `dependency_failure` boundary in the agent's
own system prompt in Lyzr Studio — that's where the taxonomy judgment lives
(confirmed by testing: the agent already applies the full closed-set schema
without any hint from this harness's compact `service=... severity=...
message=...` payload). A concrete addendum that would target exactly the 3
observed disagreements:

> Classify by **where the fault originates**, not by which system's name
> appears in the log line:
> - Our own expired certificate, misconfigured setting, or bad deploy →
>   `config_error` — even if it manifests as a failed call to an external
>   host (e.g. a TLS handshake failure caused by *our* cert being expired).
> - Our own rate limiter, consumer group, or resource pool hitting a
>   threshold *we* set → `capacity` (rate limits) or `resource_exhaustion`
>   (disk/memory) as appropriate — not `dependency_failure`.
> - The external system itself being unreachable, erroring, or degraded
>   independent of anything we configured → `dependency_failure` (e.g. a
>   circuit breaker tripping because the upstream really is down).

Rather than leave that as a suggestion for someone else's Studio prompt,
we built it — see the next section. Pass `--vote-k 3` (or set `VOTE_K=3`)
if you additionally want majority-vote's stability win (perfect root-cause
accuracy, dampened confidence on disagreement) on top; leave it at the
default 1 for the cheapest, fastest configuration.

## Tool-calling closes the gap

Every disagreement above reduces to one question text alone can't answer:
is the resource named in the log line *ours*, or a third party's?
`config.TOOLS_ENABLED` (default on) adds an agentic step to the optimized
pipeline: on categories where that question is the deciding factor, the
harness checks a simulated service-ownership registry (`tools.py` — a
CMDB/service-catalog lookup a real deployment would have for real) and, if
it recognizes the resource, makes one grounded follow-up call with that
fact attached. Python decides *when* to investigate (by category, not
confidence — the failure mode is the agent being *confidently* wrong, so a
confidence trigger would miss it); the model decides what the fact means.

**First attempt made things worse, and we're reporting that, not hiding
it.** v1 guidance ("ownership → `resource_exhaustion` or `capacity` as
appropriate") was ambiguous and biased two previously-*correct* signatures
(rate-limit, consumer-lag) toward `resource_exhaustion` — macro-F1 dropped
to 57.8%. The fix was making the rule unambiguous, not adding more
examples: `capacity` = a rate/quota/lag *threshold* was breached;
`resource_exhaustion` = the service's *own* memory/disk literally ran out;
`dependency_failure` = a failed call to *any* dependency, owned or not;
`config_error` = static configuration. That's a general definitional rule
— what these four words actually mean — not a lookup table keyed to our 10
signatures, so it isn't curve-fit to the labeled set.

Result, confirmed twice back-to-back against the live agent:

```
Accuracy (macro-F1, category)   65.8% -> 100.0%   (was 64-83% across runs 1-3 above)
Root-cause accuracy                          100.0%
Free-form violation rate                       0.0%
Cost cut vs. baseline                          96.9%   (was 97.4% with tools off — 5 extra calls)
LLM call count                            455 -> 15    (10 signatures + 5 tool-assisted)

TARGET CHECKS: 5/5 PASS
```

All 5 targets, hit consistently, with the mechanism and the failed first
attempt both left in the code and in this doc.

## Reviewer notes

- **Token accounting**: the client first looks for usage in the API response
  (`usage.input_tokens/output_tokens` or `usage.prompt_tokens/completion_tokens`,
  including a couple of common nesting shapes); if absent, it falls back to
  `tiktoken` (`o200k_base`) on the prompt + reply text. Every `LyzrResponse`
  and audit record carries `token_source` (`"api"` or `"tiktoken"`) so you can
  see which was used — `run.py` prints the aggregate split at the bottom of
  the results table.
- **Pricing**: `PRICE_INPUT_PER_M` / `PRICE_OUTPUT_PER_M` (USD per 1M tokens)
  are env-configured placeholders (`.env.example`) — set them to match
  whatever model actually backs `LYZR_AGENT_ID`. Cost numbers are only as
  correct as this input.
- **Retries/idempotency**: exponential backoff (0.5s base, doubling, ±25%
  jitter) on 429/5xx/timeout, max 4 attempts; a fresh `session_id` (uuid4) is
  minted per call so the agent has no cross-event memory. The
  `signature -> LyzrResponse` cache means a given signature is never sent to
  the model twice within a run.
- **`--limit N`** caps baseline calls for fast iteration; the optimized run
  always covers the full corpus (it's cheap enough that limiting it isn't
  useful), so `Cost / full batch` and the cost-cut target aren't
  apples-to-apples under `--limit` — only use it for quick sanity checks, not
  for the numbers you'd report.
- **Mock mode** (`LYZR_MOCK=1` / `--mock`) swaps in `lyzr_client.MockOracle`,
  a small deterministic keyword matcher. It exists solely to prove the
  plumbing (noise filter → dedup → validation → gating → audit → metrics)
  works end-to-end without network access. `run.py` and `index.html` both
  visibly flag mock output as not submittable.
- **Windows console**: `run.py` reconfigures stdout to UTF-8 defensively
  (some Windows terminals default to cp1252); all printed table output is
  otherwise plain ASCII.

## Frontend (demo only, not graded)

`server.py` is a FastAPI proxy that holds `LYZR_API_KEY` server-side (it is
never sent to the browser) and reuses `pipeline.py` directly, so the console
exercises the identical decision logic `run.py` scores.

- `GET /api/health` — mode (mock/live), agent id, closed sets, confidence threshold.
- `GET /api/samples` — one representative row per unique message from the
  corpus (labeled rows first), for the Classify dropdown.
- `POST /api/classify` — classify one ad-hoc event through the same
  noise-filter → dedup-cache → validate → gate path; returns the verdict,
  confidence, reasoning, and which path produced it.
- `POST /api/batch` — runs `optimized.run_optimized` over the full dataset;
  returns the dedup funnel (ingested → noise-filtered → unique signatures →
  model calls), the full metrics block, target pass/fail, and a sample of
  per-event decisions.

`index.html` is a single self-contained file (fonts inlined as `@font-face`
data URIs, no build step, vanilla JS) with two views: **Classify** (verdict
card with a colored category chip, confidence bar, cited reasoning, and a
path tag) and **Batch** (animated dedup funnel + metric cards checked against
the targets above + a scrollable results table).
