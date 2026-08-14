"""
Per-lever ablation: baseline -> +noise-filter -> +dedup -> +gating.

To keep this cheap (and avoid re-paying for 455 model calls four times), it
reuses one set of raw per-event LyzrResponses gathered once — either passed
in from a baseline run.py already did, or fetched fresh if run standalone —
and derives all four stages analytically from that single pass:

  raw            : every event, whatever the model said, unvalidated.
  +noise_filter  : noisy events skip the model entirely (still one raw call
                   per SURVIVING raw event — dedup not yet applied).
  +dedup         : survivors collapse to unique signatures; only the first
                   occurrence of each signature "counts" as a call.
  +gating        : same call pattern as +dedup, but closed-set validation
                   and the confidence gate are applied on the way out.

Only stage 0 -> 1 -> 2 change the LLM call count (the actual cost lever).
Stage 3 (+gating) is a quality lever: same call count as +dedup, different
accuracy/free-form numbers.
"""
from __future__ import annotations

import logging

import pandas as pd

from audit import AuditWriter
from lyzr_client import LyzrResponse, make_client
from metrics import MetricsReport, compute_metrics
from normalize import normalize_message
from pipeline import EventResult, is_noise, make_model_result, make_noise_result, make_raw_result

logger = logging.getLogger("ablation")


def _stage_raw(df: pd.DataFrame, raw: dict[str, LyzrResponse]) -> list[EventResult]:
    """Stage 0: model called on every raw event, no filtering, no gating."""
    results = []
    for row in df.to_dict("records"):
        resp = raw[row["event_id"]]
        results.append(make_raw_result(
            event_id=row["event_id"], service=row["service"], severity=row["severity"],
            message=row["message"], signature=normalize_message(row["message"]),
            resp=resp, path="model", model_call=True,
        ))
    return results


def _stage_noise_filter(df: pd.DataFrame, raw: dict[str, LyzrResponse]) -> list[EventResult]:
    """Stage 1: noisy events filtered for free; survivors still called
    per-raw-event (no dedup yet), ungated."""
    results = []
    for row in df.to_dict("records"):
        service, severity, message = row["service"], row["severity"], row["message"]
        if is_noise(severity, message):
            results.append(make_noise_result(row["event_id"], service, severity, message))
        else:
            resp = raw[row["event_id"]]
            results.append(make_raw_result(
                event_id=row["event_id"], service=service, severity=severity, message=message,
                signature=normalize_message(message), resp=resp, path="model", model_call=True,
            ))
    return results


def _stage_dedup(df: pd.DataFrame, raw: dict[str, LyzrResponse], gated: bool) -> list[EventResult]:
    """Stage 2 (gated=False) / Stage 3 (gated=True): noise filter + dedup by
    signature, one representative raw response per unique signature."""
    rows = df.to_dict("records")
    signature_of: dict[str, str] = {}
    representative_resp: dict[str, LyzrResponse] = {}
    results: list[EventResult] = [None] * len(rows)  # type: ignore

    for i, row in enumerate(rows):
        service, severity, message = row["service"], row["severity"], row["message"]
        if is_noise(severity, message):
            results[i] = make_noise_result(row["event_id"], service, severity, message)
        else:
            sig = normalize_message(message)
            signature_of[i] = sig
            if sig not in representative_resp:
                representative_resp[sig] = raw[row["event_id"]]

    emitted: set[str] = set()
    builder = make_model_result if gated else make_raw_result
    for i, sig in signature_of.items():
        row = rows[i]
        model_call = sig not in emitted
        path = "model" if model_call else "cache"
        emitted.add(sig)
        results[i] = builder(
            event_id=row["event_id"], service=row["service"], severity=row["severity"],
            message=row["message"], signature=sig, resp=representative_resp[sig],
            path=path, model_call=model_call,
        )
    return results  # type: ignore


def run_ablation(df: pd.DataFrame, raw_responses: dict[str, LyzrResponse] | None = None, max_workers: int = 8) -> dict[str, MetricsReport]:
    if raw_responses is None:
        logger.info("No cached baseline responses supplied; fetching one raw call per event for the ablation.")
        import config
        client = make_client(config.LYZR_AGENT_ID)
        from baseline import run_baseline
        _, raw_responses = run_baseline(df, client, max_workers=max_workers)

    stages = {
        "0_baseline": _stage_raw(df, raw_responses),
        "1_+noise_filter": _stage_noise_filter(df, raw_responses),
        "2_+dedup": _stage_dedup(df, raw_responses, gated=False),
        "3_+gating (=optimized)": _stage_dedup(df, raw_responses, gated=True),
    }

    reports: dict[str, MetricsReport] = {}
    for name, results in stages.items():
        # Wall clock isn't meaningful for analytically-derived stages (no
        # new calls were made) — use the summed per-event latency instead so
        # throughput/latency figures stay internally consistent per stage.
        wall = sum(r.latency_ms for r in results) / 1000.0 or 0.001
        reports[name] = compute_metrics(results, df, wall, name)

    name_w, calls_w, pct_w, cost_w = 24, 20, 11, 22
    total_w = name_w + calls_w + pct_w * 3 + cost_w
    print("\n" + "=" * total_w)
    print("ABLATION  (baseline -> +noise-filter -> +dedup -> +gating)")
    print("=" * total_w)
    header = (
        f"{'Stage':<{name_w}}{'LLM calls':>{calls_w}}{'macro-F1':>{pct_w}}"
        f"{'root-cause':>{pct_w}}{'free-form':>{pct_w}}{'cost/batch':>{cost_w}}"
    )
    print(header)
    print("-" * total_w)
    prev_calls, prev_cost = None, None
    for name, m in reports.items():
        calls_str = str(m.llm_call_count) if prev_calls is None else f"{m.llm_call_count} ({m.llm_call_count - prev_calls:+d})"
        cost_str = f"${m.total_cost:.6f}" if prev_cost is None else f"${m.total_cost:.6f} ({m.total_cost - prev_cost:+.6f})"
        print(
            f"{name:<{name_w}}{calls_str:>{calls_w}}{m.macro_f1_category * 100:>{pct_w - 1}.1f}%"
            f"{m.root_cause_accuracy * 100:>{pct_w - 1}.1f}%{m.free_form_rate * 100:>{pct_w - 1}.1f}%{cost_str:>{cost_w}}"
        )
        prev_calls, prev_cost = m.llm_call_count, m.total_cost
    print("=" * total_w)

    return reports
