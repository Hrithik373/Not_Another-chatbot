"""
Naive baseline: one LLM call per RAW event. No noise filter, no dedup, no
cache — every one of the 455 rows (or --limit N of them) hits the model.
This is the number optimized.py has to beat; the delta between the two is
the actual deliverable of this assignment.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd

from audit import AuditWriter
from lyzr_client import LyzrResponse
from normalize import normalize_message
from pipeline import EventResult, make_model_result

logger = logging.getLogger("baseline")


def run_baseline(
    df: pd.DataFrame,
    client,
    audit_writer: Optional[AuditWriter] = None,
    limit: Optional[int] = None,
    max_workers: int = 8,
) -> tuple[list[EventResult], dict[str, LyzrResponse]]:
    """Returns (gated EventResults, raw per-event LyzrResponse keyed by
    event_id). The raw responses are handed to ablation.py so it can derive
    the noise-filter/dedup/gating stages without re-calling the model."""
    rows = df.to_dict("records") if limit is None else df.head(limit).to_dict("records")

    def _classify_one(row: dict) -> tuple[EventResult, LyzrResponse]:
        service, severity, message = row["service"], row["severity"], row["message"]
        resp = client.classify(service, severity, message)
        result = make_model_result(
            event_id=row["event_id"], service=service, severity=severity, message=message,
            signature=normalize_message(message), resp=resp, path="model", model_call=True,
        )
        return result, resp

    results: list[EventResult] = [None] * len(rows)  # type: ignore
    raw_responses: dict[str, LyzrResponse] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {pool.submit(_classify_one, row): i for i, row in enumerate(rows)}
        done = 0
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            result, resp = fut.result()
            results[idx] = result
            raw_responses[result.event_id] = resp
            done += 1
            if done % 50 == 0 or done == len(rows):
                logger.info("baseline progress: %d/%d", done, len(rows))

    if audit_writer is not None:
        for r in results:
            audit_writer.write(r)

    return results, raw_responses
