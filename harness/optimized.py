"""
Optimized pipeline: cheap-path noise filter -> normalize+cluster by
signature -> ONE model call per unique signature (optionally majority-voted,
optionally followed by a tool-grounded investigation call — see
pipeline.classify_event) -> fan the decision back to every event in that
cluster -> closed-set validation -> confidence gate -> audit. This is where
nearly all of the cost/latency win over baseline.py comes from: with ~455
events collapsing to a handful of unique signatures, the model is called a
couple orders of magnitude less often.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd

import config
from audit import AuditWriter
from lyzr_client import LyzrResponse
from normalize import normalize_message
import tools
from pipeline import EventResult, SignatureCache, classify_event, is_noise, make_model_result, make_noise_result

logger = logging.getLogger("optimized")


def run_optimized(
    df: pd.DataFrame,
    client,
    audit_writer: Optional[AuditWriter] = None,
    max_workers: int = 8,
    vote_k: Optional[int] = None,
) -> tuple[list[EventResult], SignatureCache]:
    vote_k = vote_k if vote_k is not None else config.VOTE_K
    rows = df.to_dict("records")
    cache = SignatureCache()

    # Pass 1: cheap-path noise filter. Survivors get their signature computed
    # up front so pass 2 only has to think about unique signatures.
    noise_results: dict[int, EventResult] = {}
    survivor_idx: list[int] = []
    survivor_sig: dict[int, str] = {}
    for i, row in enumerate(rows):
        service, severity, message = row["service"], row["severity"], row["message"]
        if is_noise(severity, message):
            noise_results[i] = make_noise_result(row["event_id"], service, severity, message)
        else:
            survivor_idx.append(i)
            survivor_sig[i] = normalize_message(message)

    unique_signatures = sorted({survivor_sig[i] for i in survivor_idx})
    logger.info(
        "noise filter: %d/%d events filtered without a model call; %d survivors collapse to %d unique signatures",
        len(noise_results), len(rows), len(survivor_idx), len(unique_signatures),
    )

    # Pass 2: exactly one model call per unique signature, run concurrently.
    # Use the first survivor row with each signature as the representative
    # message sent to the model (post-normalization the content is equivalent
    # within a cluster by construction).
    representative_message: dict[str, dict] = {}
    for i in survivor_idx:
        sig = survivor_sig[i]
        if sig not in representative_message:
            representative_message[sig] = rows[i]

    def _classify_signature(sig: str) -> tuple[str, LyzrResponse, int, Optional[tools.OwnershipResult]]:
        row = representative_message[sig]
        resp, n_calls, ownership = classify_event(client, row["service"], row["severity"], row["message"], vote_k)
        return sig, resp, n_calls, ownership

    calls_per_signature: dict[str, int] = {}
    ownership_per_signature: dict[str, Optional[tools.OwnershipResult]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_classify_signature, sig) for sig in unique_signatures]
        for fut in as_completed(futures):
            sig, resp, n_calls, ownership = fut.result()
            cache.put(sig, resp)
            calls_per_signature[sig] = n_calls
            ownership_per_signature[sig] = ownership

    # Pass 3: fan the cached decision back out to every survivor event, in
    # original row order. The row that happens to be processed first for a
    # given signature is tagged path=model conceptually (the call happened
    # once for the cluster); every row is validated/gated independently
    # since gating depends only on the (shared) response content.
    emitted_signature_as_model: set[str] = set()
    results: list[Optional[EventResult]] = [None] * len(rows)
    for i in noise_results:
        results[i] = noise_results[i]
    for i in survivor_idx:
        sig = survivor_sig[i]
        resp = cache.peek(sig)
        row = rows[i]
        if sig not in emitted_signature_as_model:
            path = "model"
            model_call = True
            api_calls = calls_per_signature[sig]
            ownership = ownership_per_signature[sig]
            emitted_signature_as_model.add(sig)
        else:
            path = "cache"
            model_call = False
            api_calls = 0
            ownership = None
        results[i] = make_model_result(
            event_id=row["event_id"], service=row["service"], severity=row["severity"],
            message=row["message"], signature=sig, resp=resp, path=path, model_call=model_call,
            api_calls=api_calls,
            tool_used=ownership is not None,
            tool_evidence=(f"{ownership.resource} -> {ownership.owner} ({ownership.system})" if ownership else ""),
        )

    final_results: list[EventResult] = results  # type: ignore

    if audit_writer is not None:
        for r in final_results:
            audit_writer.write(r)

    return final_results, cache
