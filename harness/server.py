"""
FastAPI proxy for the SRE triage console (index.html). Holds the Lyzr API
key server-side (NEVER sent to the browser) and reuses pipeline.py so the
demo exercises the exact same noise-filter/dedup/gating logic run.py scores.

    uvicorn server:app --reload
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
from lyzr_client import make_client
from metrics import compute_metrics
from normalize import normalize_message
from optimized import run_optimized
from pipeline import SignatureCache, classify_event, is_noise, make_model_result, make_noise_result

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("server")

app = FastAPI(title="Auto-Remediation Console API")

_client = None
_cache = SignatureCache()  # process-lifetime idempotency for the live demo
_df: Optional[pd.DataFrame] = None
_last_batch_call_ts = 0.0  # global cooldown gate for POST /api/batch — see config.BATCH_MIN_INTERVAL_SECONDS


def get_client():
    global _client
    if _client is None:
        _client = make_client(config.LYZR_AGENT_ID)
    return _client


def get_df() -> pd.DataFrame:
    global _df
    if _df is None:
        _df = pd.read_csv(config.DATA_PATH, dtype=str).fillna("")
    return _df


class ClassifyRequest(BaseModel):
    service: str = "unknown-service"
    severity: str = "ERROR"
    message: str


class ClassifyResponse(BaseModel):
    event_id: str = "adhoc"
    service: str
    severity: str
    message: str
    signature: str
    category: str
    root_cause: str
    remediation: str
    confidence: float
    needs_human: bool
    reasoning: str
    path: str
    free_form_violation: bool
    latency_ms: float
    input_tokens: int
    output_tokens: int
    token_source: str
    tool_used: bool = False
    tool_evidence: str = ""


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "mock": config.LYZR_MOCK,
        "agent_id": config.LYZR_AGENT_ID or None,
        "baseline_agent_id": config.LYZR_BASELINE_AGENT_ID or None,
        "confidence_threshold": config.CONFIDENCE_THRESHOLD,
        "vote_k": config.VOTE_K,
        "tools_enabled": config.TOOLS_ENABLED,
        # Exposed so this same static page can call POST /api/batch when a
        # token is configured — NOT meant as strong security, the real
        # protection against demo-link abuse is the server-side cooldown
        # (config.BATCH_MIN_INTERVAL_SECONDS), which applies regardless of
        # whether this token leaks.
        "batch_token": config.PUBLIC_BATCH_TOKEN or None,
        "categories": sorted(config.CATEGORIES),
        "root_causes": sorted(config.ROOT_CAUSES),
        "remediations": sorted(config.REMEDIATIONS),
        "cached_signatures": len(_cache),
    }


@app.get("/api/samples")
def samples(limit: int = 40):
    df = get_df()
    # One representative row per unique message, prioritizing labeled rows,
    # so the demo dropdown covers every distinct event type without
    # dumping all 455 near-duplicate rows on the client.
    df = df.copy()
    df["_labeled_first"] = (df["is_labeled"] == "yes").astype(int)
    df = df.sort_values("_labeled_first", ascending=False)
    picked = df.drop_duplicates(subset=["message"], keep="first").head(limit)
    picked = picked.sort_values("event_id")
    return picked[["event_id", "service", "severity", "message", "is_labeled", "gt_category", "gt_root_cause"]].to_dict("records")


@app.post("/api/classify", response_model=ClassifyResponse)
def classify(req: ClassifyRequest):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    service, severity, message = req.service, req.severity.upper(), req.message
    sig = normalize_message(message)

    if is_noise(severity, message):
        result = make_noise_result("adhoc", service, severity, message)
    else:
        cached = _cache.peek(sig)
        if cached is not None:
            result = make_model_result("adhoc", service, severity, message, sig, cached, path=config.PATH_CACHE, model_call=False, api_calls=0)
        else:
            client = get_client()
            resp, n_calls, ownership = classify_event(client, service, severity, message, config.VOTE_K)
            _cache.put(sig, resp)
            result = make_model_result(
                "adhoc", service, severity, message, sig, resp, path=config.PATH_MODEL, model_call=True, api_calls=n_calls,
                tool_used=ownership is not None,
                tool_evidence=(f"{ownership.resource} -> {ownership.owner} ({ownership.system})" if ownership else ""),
            )

    return ClassifyResponse(
        service=result.service, severity=result.severity, message=result.message, signature=result.signature,
        category=result.category, root_cause=result.root_cause, remediation=result.remediation,
        confidence=result.confidence, needs_human=result.needs_human, reasoning=result.reasoning,
        path=result.path, free_form_violation=result.free_form_violation, latency_ms=result.latency_ms,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens, token_source=result.token_source,
        tool_used=result.tool_used, tool_evidence=result.tool_evidence,
    )


@app.post("/api/batch")
def batch(x_batch_token: str = Header(default="")):
    # Public-demo safeguards: POST /api/batch runs a real classification
    # pass with real API cost, so a hosted deployment shouldn't leave it
    # wide open to repeated/automated hits. Both checks are no-ops locally
    # unless their env vars are set — see config.py.
    if config.PUBLIC_BATCH_TOKEN and x_batch_token != config.PUBLIC_BATCH_TOKEN:
        raise HTTPException(status_code=403, detail="missing or invalid X-Batch-Token")

    global _last_batch_call_ts
    now = time.perf_counter()
    elapsed = now - _last_batch_call_ts
    if elapsed < config.BATCH_MIN_INTERVAL_SECONDS:
        retry_in = round(config.BATCH_MIN_INTERVAL_SECONDS - elapsed, 1)
        raise HTTPException(status_code=429, detail=f"batch endpoint is cooling down, retry in {retry_in}s")
    _last_batch_call_ts = now

    df = get_df()
    client = get_client()
    t0 = time.perf_counter()
    results, sig_cache = run_optimized(df, client)
    wall = time.perf_counter() - t0

    n_total = len(results)
    n_noise = sum(1 for r in results if r.path == config.PATH_NOISE_FILTER)
    n_survivors = n_total - n_noise
    n_unique_sigs = len(sig_cache)
    n_api_calls = sum(r.api_calls for r in results)  # real API calls (= n_unique_sigs * vote_k)
    n_cache_hits = n_survivors - n_unique_sigs

    m = compute_metrics(results, df, wall, "batch")

    return {
        "funnel": {
            "ingested": n_total,
            "noise_filtered": n_noise,
            "survivors": n_survivors,
            "unique_signatures": n_unique_sigs,
            "model_calls": n_api_calls,
            "vote_k": config.VOTE_K,
            "cache_hits": n_cache_hits,
        },
        "metrics": {
            "macro_f1_category": m.macro_f1_category,
            "root_cause_accuracy": m.root_cause_accuracy,
            "n_labeled_scored": m.n_labeled_scored,
            "free_form_rate": m.free_form_rate,
            "escalation_rate": m.escalation_rate,
            "p50_latency_ms": m.p50_latency_ms,
            "p95_latency_ms": m.p95_latency_ms,
            "p95_latency_escalated_ms": m.p95_latency_escalated_ms,
            "avg_tokens_per_task": m.avg_tokens_per_task,
            "cost_per_task": m.cost_per_task,
            "total_cost": m.total_cost,
            "throughput_events_per_min": m.throughput_events_per_min,
            "llm_call_count": m.llm_call_count,
            "tool_assisted_signatures": m.tool_assisted_signatures,
        },
        "targets": {
            "macro_f1": {"target": config.TARGET_MACRO_F1, "actual": m.macro_f1_category, "pass": m.macro_f1_category >= config.TARGET_MACRO_F1},
            "root_cause_acc": {"target": config.TARGET_ROOT_CAUSE_ACC, "actual": m.root_cause_accuracy, "pass": m.root_cause_accuracy >= config.TARGET_ROOT_CAUSE_ACC},
            "free_form_rate": {"target": config.TARGET_FREE_FORM_RATE, "actual": m.free_form_rate, "pass": m.free_form_rate <= config.TARGET_FREE_FORM_RATE},
            "p95_escalated_s": {"target": config.TARGET_P95_ESCALATED_SECONDS, "actual": m.p95_latency_escalated_ms / 1000, "pass": m.p95_latency_escalated_ms <= config.TARGET_P95_ESCALATED_SECONDS * 1000},
        },
        "mock": config.LYZR_MOCK,
        "sample_decisions": [
            {
                "event_id": r.event_id, "service": r.service, "severity": r.severity, "message": r.message,
                "category": r.category, "root_cause": r.root_cause, "remediation": r.remediation,
                "confidence": r.confidence, "needs_human": r.needs_human, "reasoning": r.reasoning, "path": r.path,
                "tool_used": r.tool_used, "tool_evidence": r.tool_evidence,
            }
            for r in results[:60]
        ],
    }


_INDEX_PATH = Path(__file__).resolve().parent / "index.html"


@app.get("/")
def index():
    if not _INDEX_PATH.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(_INDEX_PATH)
