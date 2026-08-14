"""
CLI entry point. Runs the naive baseline and the optimized pipeline over the
same dataset, scores both against the 40 labeled rows, and prints a
side-by-side results table with deltas and target pass/fail flags.

    python run.py --mock                 # local oracle, pipeline smoke test (not submittable)
    python run.py                        # real Lyzr API, full 455-event run
    python run.py --limit 50             # cap baseline calls while iterating
    python run.py --ablation             # also print per-lever marginal deltas
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

# Make console output robust on Windows terminals that default to cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import pandas as pd

import config
from audit import AuditWriter
from baseline import run_baseline
from lyzr_client import make_client
from metrics import MetricsReport, compute_metrics
from optimized import run_optimized

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("run")


def _fmt(v, kind: str) -> str:
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "ms":
        return f"{v:.0f} ms"
    if kind == "usd":
        return f"${v:.6f}"
    if kind == "usd2":
        return f"${v:.4f}"
    if kind == "num":
        return f"{v:.1f}"
    if kind == "int":
        return f"{v:d}"
    return str(v)


def print_results_table(baseline_m: MetricsReport, optimized_m: MetricsReport) -> None:
    rows = [
        ("Accuracy (macro-F1, category)", baseline_m.macro_f1_category, optimized_m.macro_f1_category, "pct"),
        ("Root-cause accuracy", baseline_m.root_cause_accuracy, optimized_m.root_cause_accuracy, "pct"),
        ("Free-form violation rate", baseline_m.free_form_rate, optimized_m.free_form_rate, "pct"),
        ("Escalation rate (needs_human)", baseline_m.escalation_rate, optimized_m.escalation_rate, "pct"),
        ("p50 latency / task", baseline_m.p50_latency_ms, optimized_m.p50_latency_ms, "ms"),
        ("p95 latency / task", baseline_m.p95_latency_ms, optimized_m.p95_latency_ms, "ms"),
        ("p95 latency, escalated only", baseline_m.p95_latency_escalated_ms, optimized_m.p95_latency_escalated_ms, "ms"),
        ("Avg tokens / task", baseline_m.avg_tokens_per_task, optimized_m.avg_tokens_per_task, "num"),
        ("Cost / task", baseline_m.cost_per_task, optimized_m.cost_per_task, "usd"),
        ("Cost / full batch", baseline_m.total_cost, optimized_m.total_cost, "usd2"),
        ("Throughput (events/min)", baseline_m.throughput_events_per_min, optimized_m.throughput_events_per_min, "num"),
        ("LLM call count", baseline_m.llm_call_count, optimized_m.llm_call_count, "int"),
        ("Tool-assisted signatures", 0, optimized_m.tool_assisted_signatures, "int"),
    ]

    name_w = max(len(r[0]) for r in rows) + 2
    col_w = 18
    header = f"{'Metric':<{name_w}}{'Naive baseline':>{col_w}}{'Optimized':>{col_w}}{'Delta':>{col_w}}"
    print("\n" + "=" * len(header))
    print(f"RESULTS  (baseline agent_id={config.LYZR_BASELINE_AGENT_ID or '<mock>'}, "
          f"optimized agent_id={config.LYZR_AGENT_ID or '<mock>'})")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for name, b, o, kind in rows:
        if kind == "int":
            delta = f"{o - b:+d}"
        else:
            delta = f"{o - b:+.4f}" if kind in ("pct",) else f"{(o - b):+.2f}"
            if kind == "pct":
                delta = f"{(o - b) * 100:+.1f}pp"
            elif kind == "ms":
                delta = f"{(o - b):+.0f} ms"
            elif kind in ("usd", "usd2"):
                delta = f"{(o - b):+.6f}" if kind == "usd" else f"{(o - b):+.4f}"
            elif kind == "num":
                delta = f"{(o - b):+.2f}"
        print(f"{name:<{name_w}}{_fmt(b, kind):>{col_w}}{_fmt(o, kind):>{col_w}}{delta:>{col_w}}")
    print("=" * len(header))

    cost_cut = 1 - (optimized_m.total_cost / baseline_m.total_cost) if baseline_m.total_cost > 0 else (
        1.0 if optimized_m.total_cost == 0 else 0.0
    )
    print("\nTARGET CHECKS")
    checks = [
        ("macro-F1 >= 0.85", optimized_m.macro_f1_category >= config.TARGET_MACRO_F1, optimized_m.macro_f1_category),
        ("root-cause acc >= 0.80", optimized_m.root_cause_accuracy >= config.TARGET_ROOT_CAUSE_ACC, optimized_m.root_cause_accuracy),
        ("free-form rate == 0", optimized_m.free_form_rate <= config.TARGET_FREE_FORM_RATE, optimized_m.free_form_rate),
        ("p95 latency (escalated) <= 4s", optimized_m.p95_latency_escalated_ms <= config.TARGET_P95_ESCALATED_SECONDS * 1000, optimized_m.p95_latency_escalated_ms),
        ("cost cut vs baseline >= 50%", cost_cut >= config.TARGET_COST_CUT_PCT, cost_cut),
    ]
    for label, passed, val in checks:
        flag = "PASS" if passed else "FAIL"
        print(f"  [{flag}] {label}  (actual: {val:.4f})")
    print(f"\nScored against {optimized_m.n_labeled_scored} labeled rows.")
    print(f"Token source (optimized): {optimized_m.token_source_counts}")
    print(f"Token source (baseline):  {baseline_m.token_source_counts}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-Remediation from Logs — benchmark harness")
    parser.add_argument("--mock", action="store_true", help="Use local MockOracle instead of the real Lyzr API (not submittable)")
    parser.add_argument("--ablation", action="store_true", help="Also run and print the per-lever ablation table")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of baseline calls (iteration speed)")
    parser.add_argument("--max-workers", type=int, default=8, help="Concurrent model calls")
    parser.add_argument("--vote-k", type=int, default=None, help="Majority-vote k calls per signature in the optimized pipeline (default: VOTE_K env, else 1 = off)")
    parser.add_argument("--no-tools", action="store_true", help="Disable the tool-grounded investigation pass (default: on — see config.TOOLS_ENABLED)")
    args = parser.parse_args()

    if args.no_tools:
        config.TOOLS_ENABLED = False

    if args.mock:
        config.LYZR_MOCK = True

    df = pd.read_csv(config.DATA_PATH, dtype=str).fillna("")
    logger.info("Loaded %d events from %s (%d labeled)", len(df), config.DATA_PATH, (df["is_labeled"] == "yes").sum())

    baseline_client = make_client(config.LYZR_BASELINE_AGENT_ID)
    optimized_client = make_client(config.LYZR_AGENT_ID)

    with AuditWriter("baseline") as baseline_audit:
        t0 = time.perf_counter()
        baseline_results, baseline_raw_responses = run_baseline(df, baseline_client, audit_writer=baseline_audit, limit=args.limit, max_workers=args.max_workers)
        baseline_wall = time.perf_counter() - t0
    logger.info("baseline done: %d events, %d model calls, %.1fs wall clock", len(baseline_results), sum(r.model_call for r in baseline_results), baseline_wall)

    with AuditWriter("optimized") as optimized_audit:
        t0 = time.perf_counter()
        optimized_results, sig_cache = run_optimized(df, optimized_client, audit_writer=optimized_audit, max_workers=args.max_workers, vote_k=args.vote_k)
        optimized_wall = time.perf_counter() - t0
    logger.info("optimized done: %d events, %d unique signatures, %d LLM calls, %.1fs wall clock", len(optimized_results), len(sig_cache), sum(r.api_calls for r in optimized_results), optimized_wall)

    # Score baseline only over the subset it actually ran (relevant when --limit is used).
    baseline_df = df if args.limit is None else df.head(args.limit)
    baseline_m = compute_metrics(baseline_results, baseline_df, baseline_wall, "baseline")
    optimized_m = compute_metrics(optimized_results, df, optimized_wall, "optimized")

    if config.LYZR_MOCK:
        print("\n*** LYZR_MOCK=1: results below are from the local oracle - NOT submittable, pipeline smoke test only. ***")

    print_results_table(baseline_m, optimized_m)

    if args.ablation:
        from ablation import run_ablation
        # Reuse the baseline's raw per-event responses so the ablation doesn't
        # re-pay for another 455 (or --limit N) model calls. Only valid when
        # the baseline covered the full dataset; with --limit, ablation still
        # works but is scored over the same limited subset.
        ablation_df = df if args.limit is None else df.head(args.limit)
        run_ablation(ablation_df, raw_responses=baseline_raw_responses, max_workers=args.max_workers)


if __name__ == "__main__":
    main()
