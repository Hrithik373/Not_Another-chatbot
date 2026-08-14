"""
Per-event audit trail: every event's final decision, reasoning, and the path
that produced it (noise_filter | model | cache) gets appended to
runs/audit_<timestamp>.jsonl. This is what a reviewer/operator would use to
answer "why did the system decide X for event Y".
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import config
from pipeline import EventResult


class AuditWriter:
    def __init__(self, run_name: str, out_dir: Optional[Path] = None):
        out_dir = out_dir or config.RUNS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.path = out_dir / f"audit_{run_name}_{ts}.jsonl"
        self._fh = open(self.path, "w", encoding="utf-8")

    def write(self, result: EventResult) -> None:
        record = {
            "event_id": result.event_id,
            "service": result.service,
            "severity": result.severity,
            "message": result.message,
            "signature": result.signature,
            "category": result.category,
            "root_cause": result.root_cause,
            "remediation": result.remediation,
            "confidence": result.confidence,
            "needs_human": result.needs_human,
            "reasoning": result.reasoning,
            "path": result.path,
            "free_form_violation": result.free_form_violation,
            "model_call": result.model_call,
            "tool_used": result.tool_used,
            "tool_evidence": result.tool_evidence,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "token_source": result.token_source,
            "latency_ms": result.latency_ms,
        }
        self._fh.write(json.dumps(record) + "\n")

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "AuditWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
