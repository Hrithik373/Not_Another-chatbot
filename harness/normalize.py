"""
message -> signature normalization.

Two log lines that differ only in a timestamp, a hex pointer, a UUID, or a
numeric value (queue depth, error code, latency, %) are the *same event type*
for classification purposes. We strip those volatile bits out so that the
dedup/clustering step in pipeline.py can group them under one signature and
call the model exactly once per distinct signature.
"""
from __future__ import annotations

import hashlib
import re

# Order matters: replace the more specific patterns (UUID, hex) before the
# generic digit-run pattern would otherwise chew through them.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
# ISO-ish timestamps, e.g. 2026-08-14T19:22:01Z or 2026-08-14 19:22:01.123
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_DIGIT_RUN_RE = re.compile(r"\d+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_message(message: str) -> str:
    """Collapse a raw log message into a stable signature string.

    Deterministic and order-sensitive: timestamps and UUIDs are replaced
    before the catch-all digit-run pass so we don't leave fragments behind.
    """
    text = message.strip().lower()
    text = _TIMESTAMP_RE.sub("<ts>", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _DIGIT_RUN_RE.sub("<num>", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def signature_hash(signature: str) -> str:
    """Short stable hash of a signature, used as a compact cache/audit key."""
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
