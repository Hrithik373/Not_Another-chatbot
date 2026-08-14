"""
Scoring: macro-F1 (category) and root-cause accuracy on the 40 labeled rows,
plus operational metrics (free-form rate, latency percentiles, tokens, cost,
throughput, call count) over the full run. Everything printed by run.py comes
out of this module so the numbers are reproducible, not hand-picked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

import config
from pipeline import EventResult


def macro_f1(y_true: list[str], y_pred: list[str]) -> float:
    """Unweighted mean of per-class F1, averaged over labels present in
    y_true (standard macro-F1 definition)."""
    if not y_true:
        return 0.0
    labels = sorted(set(y_true))
    f1_scores = []
    for label in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred) if p == label and t == label)
        fp = sum(1 for t, p in zip(y_true, y_pred) if p == label and t != label)
        fn = sum(1 for t, p in zip(y_true, y_pred) if p != label and t == label)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        f1_scores.append(f1)
    return sum(f1_scores) / len(f1_scores)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(pct / 100 * (len(s) - 1))))
    return s[idx]


@dataclass
class MetricsReport:
    run_label: str
    n_events: int
    macro_f1_category: float
    root_cause_accuracy: float
    n_labeled_scored: int
    free_form_rate: float
    escalation_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    p95_latency_escalated_ms: float
    avg_input_tokens_per_task: float
    avg_output_tokens_per_task: float
    avg_tokens_per_task: float
    cost_per_task: float
    total_cost: float
    throughput_events_per_min: float
    llm_call_count: int
    tool_assisted_signatures: int = 0
    token_source_counts: dict = field(default_factory=dict)


def compute_metrics(
    results: list[EventResult],
    df: pd.DataFrame,
    wall_clock_seconds: float,
    run_label: str,
) -> MetricsReport:
    n = len(results)

    gt_lookup = {
        row["event_id"]: row
        for row in df[df["is_labeled"] == "yes"].to_dict("records")
    }
    y_true_cat, y_pred_cat = [], []
    rc_correct, rc_total = 0, 0
    for r in results:
        gt = gt_lookup.get(r.event_id)
        if gt is None:
            continue
        y_true_cat.append(gt["gt_category"])
        y_pred_cat.append(r.category)
        rc_total += 1
        if r.root_cause == gt["gt_root_cause"]:
            rc_correct += 1

    f1 = macro_f1(y_true_cat, y_pred_cat)
    rc_acc = (rc_correct / rc_total) if rc_total else 0.0

    n_violations = sum(1 for r in results if r.free_form_violation)
    n_escalated = sum(1 for r in results if r.needs_human)
    free_form_rate = n_violations / n if n else 0.0
    escalation_rate = n_escalated / n if n else 0.0

    all_latencies = [r.latency_ms for r in results]
    escalated_latencies = [r.latency_ms for r in results if r.needs_human]

    call_results = [r for r in results if r.model_call]
    # Real API calls made, not "decisions produced" — these differ when
    # vote_k > 1 (majority-vote already aggregates the k responses' tokens
    # into the one representative call_results row, so cost accounting
    # below is unaffected; this only fixes the call-count metric itself).
    n_calls = sum(r.api_calls for r in results)
    avg_in = sum(r.input_tokens for r in call_results) / n if n else 0.0
    avg_out = sum(r.output_tokens for r in call_results) / n if n else 0.0

    total_cost = sum(
        (r.input_tokens * config.PRICE_INPUT_PER_M + r.output_tokens * config.PRICE_OUTPUT_PER_M) / 1_000_000
        for r in call_results
    )
    cost_per_task = total_cost / n if n else 0.0

    throughput = (n / (wall_clock_seconds / 60)) if wall_clock_seconds > 0 else float("inf")

    token_source_counts: dict = {}
    for r in call_results:
        token_source_counts[r.token_source] = token_source_counts.get(r.token_source, 0) + 1

    return MetricsReport(
        run_label=run_label,
        n_events=n,
        macro_f1_category=f1,
        root_cause_accuracy=rc_acc,
        n_labeled_scored=rc_total,
        free_form_rate=free_form_rate,
        escalation_rate=escalation_rate,
        p50_latency_ms=percentile(all_latencies, 50),
        p95_latency_ms=percentile(all_latencies, 95),
        p95_latency_escalated_ms=percentile(escalated_latencies, 95),
        avg_input_tokens_per_task=avg_in,
        avg_output_tokens_per_task=avg_out,
        avg_tokens_per_task=avg_in + avg_out,
        cost_per_task=cost_per_task,
        total_cost=total_cost,
        throughput_events_per_min=throughput,
        llm_call_count=n_calls,
        tool_assisted_signatures=sum(1 for r in call_results if r.tool_used),
        token_source_counts=token_source_counts,
    )
