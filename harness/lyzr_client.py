"""
Thin wrapper around the Lyzr `/v3/inference/chat/` endpoint.

Responsibilities (and nothing more — the agent itself is a stateless
classifier, all intelligence about *when* to call it lives in pipeline.py):
  - build the per-call payload with a FRESH session_id (no cross-event memory)
  - retry with exponential backoff on 429 / 5xx / timeout, max 4 tries
  - time each call
  - extract token usage from the response if present, else fall back to
    tiktoken (o200k_base) on input+output text, and record which source was
    used so run.py can print it
  - parse the strict-JSON agent reply into a plain dict; parsing failures are
    surfaced (not swallowed) so pipeline.py can route them to escalation

LYZR_MOCK=1 swaps in `MockOracle`, a local, deterministic, keyword-based
stand-in used ONLY to exercise the pipeline end-to-end without network
access. It is clearly not a real model call and every result it produces is
tagged accordingly.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

import config

logger = logging.getLogger("lyzr_client")

try:
    import tiktoken

    _ENCODING = tiktoken.get_encoding("o200k_base")
except Exception:  # pragma: no cover - tiktoken should always be installed
    _ENCODING = None


# The Lyzr agent (agent_id in .env) is configured server-side in Lyzr Studio
# with the full classification system prompt — closed sets, output schema,
# and noise/unknown handling all live there, confirmed by live testing
# (see README "Reviewer notes"). Per the design thesis the agent is a thin,
# stateless classifier: the harness's job is just to hand it the raw event
# facts, not to re-teach it the schema on every call. pipeline.py's
# closed-set validation stays authoritative regardless of what the agent
# already enforces — this is defense in depth, not the only line of defense.
PROMPT_TEMPLATE = "service={service} severity={severity} message={message}"

# Appended only when pipeline.investigate_with_tool fires a grounded
# follow-up call (see tools.py). Kept separate from PROMPT_TEMPLATE so the
# common, no-tool path stays exactly the compact single line above.
TOOL_CONTEXT_TEMPLATE = "\n\nAdditional verified context (from an internal tool call, not the log line itself): {extra_context}"


def build_prompt(service: str, severity: str, message: str, extra_context: str = "") -> str:
    prompt = PROMPT_TEMPLATE.format(service=service, severity=severity, message=message)
    if extra_context:
        prompt += TOOL_CONTEXT_TEMPLATE.format(extra_context=extra_context)
    return prompt


@dataclass
class LyzrResponse:
    category: str
    root_cause: str
    remediation: str
    confidence: float
    needs_human: bool
    reasoning: str
    input_tokens: int
    output_tokens: int
    token_source: str  # "api" | "tiktoken"
    latency_ms: float
    raw_text: str
    parse_error: bool = False
    attempts: int = 1
    error: Optional[str] = None


def _count_tokens_fallback(text: str) -> int:
    if _ENCODING is None:
        # Very rough fallback if tiktoken is somehow unavailable.
        return max(1, len(text) // 4)
    return len(_ENCODING.encode(text))


def _extract_text(payload: dict) -> str:
    """The Lyzr chat endpoint's reply text can show up under a few possible
    keys depending on API version; check the documented one first."""
    for key in ("response", "message", "output", "text", "content"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    # Some deployments nest it, e.g. {"data": {"response": "..."}}
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_text(data)
    return ""


def _extract_usage(payload: dict) -> Optional[tuple[int, int]]:
    """Look for token usage in a handful of common shapes. Returns
    (input_tokens, output_tokens) or None if not present."""
    usage = payload.get("usage") or payload.get("token_usage")
    if isinstance(payload.get("metadata"), dict) and usage is None:
        usage = payload["metadata"].get("usage")
    if not isinstance(usage, dict):
        return None
    for in_key, out_key in (
        ("input_tokens", "output_tokens"),
        ("prompt_tokens", "completion_tokens"),
    ):
        if in_key in usage and out_key in usage:
            try:
                return int(usage[in_key]), int(usage[out_key])
            except (TypeError, ValueError):
                return None
    return None


def _regex_repair_parse(text: str) -> Optional[dict]:
    """Last-resort field-by-field extraction for a real, observed failure
    mode: the live agent sometimes quotes a fragment of the log message
    inside `reasoning` without escaping the inner quotes, e.g.
        "reasoning":"The event says "rate limit exceeded..." which..."
    That's invalid JSON (the unescaped quotes prematurely close the string),
    so json.loads correctly rejects it. The five scalar fields never contain
    quotes themselves, so they're extracted independently by name; reasoning
    is free text and is pulled from after its key to the object's final
    closing quote+brace, tolerating whatever quoting mess is in between.
    """
    def scalar(key: str) -> Optional[str]:
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', text)
        return m.group(1) if m else None

    category, root_cause, remediation = scalar("category"), scalar("root_cause"), scalar("remediation")
    if category is None or root_cause is None or remediation is None:
        return None  # not the shape we know how to repair

    conf_m = re.search(r'"confidence"\s*:\s*([0-9.]+)', text)
    human_m = re.search(r'"needs_human"\s*:\s*(true|false)', text, re.IGNORECASE)
    reason_m = re.search(r'"reasoning"\s*:\s*"(.*)"\s*\}\s*$', text, re.DOTALL)

    return {
        "category": category,
        "root_cause": root_cause,
        "remediation": remediation,
        "confidence": float(conf_m.group(1)) if conf_m else 0.5,
        "needs_human": (human_m.group(1).lower() == "true") if human_m else False,
        "reasoning": reason_m.group(1) if reason_m else "[reasoning unparseable due to malformed quoting in raw reply]",
    }


def _parse_json_reply(raw_text: str) -> Optional[dict]:
    text = raw_text.strip()
    # Strip ```json ... ``` fences if the model wrapped its output.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Grab the first {...} blob and retry strict parsing.
    start, end = text.find("{"), text.rfind("}")
    blob = text[start : end + 1] if (start != -1 and end != -1 and end > start) else text
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        pass
    # Field-by-field regex repair for the known unescaped-inner-quote failure mode.
    return _regex_repair_parse(blob)


class LyzrClient:
    """Real HTTP client for the Lyzr inference endpoint."""

    def __init__(self, agent_id: str, api_key: str = "", user_id: str = ""):
        self.agent_id = agent_id
        self.api_key = api_key or config.LYZR_API_KEY
        self.user_id = user_id or config.LYZR_USER_ID
        self._session = requests.Session()

    def classify(self, service: str, severity: str, message: str, extra_context: str = "") -> LyzrResponse:
        prompt = build_prompt(service, severity, message, extra_context)
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}

        last_error = None
        start = time.perf_counter()
        for attempt in range(1, config.MAX_RETRIES + 1):
            body = {
                "user_id": self.user_id,
                "agent_id": self.agent_id,
                "session_id": str(uuid.uuid4()),  # fresh per call, neutralizes memory
                "message": prompt,
            }
            try:
                resp = self._session.post(
                    config.LYZR_API_URL,
                    headers=headers,
                    json=body,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    raise requests.exceptions.RequestException(last_error)
                resp.raise_for_status()
                payload = resp.json()
                break
            except (requests.exceptions.RequestException, ValueError) as exc:
                last_error = str(exc)
                logger.warning("Lyzr call attempt %d/%d failed: %s", attempt, config.MAX_RETRIES, last_error)
                if attempt < config.MAX_RETRIES:
                    delay = config.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                    delay += random.uniform(0, delay * 0.25)
                    time.sleep(delay)
                else:
                    latency_ms = (time.perf_counter() - start) * 1000
                    return LyzrResponse(
                        category="unknown", root_cause="unknown", remediation="escalate_to_human",
                        confidence=0.0, needs_human=True,
                        reasoning=f"API call failed after {config.MAX_RETRIES} attempts: {last_error}",
                        input_tokens=_count_tokens_fallback(prompt), output_tokens=0,
                        token_source="tiktoken", latency_ms=latency_ms, raw_text="",
                        parse_error=True, attempts=attempt, error=last_error,
                    )
        latency_ms = (time.perf_counter() - start) * 1000

        raw_text = _extract_text(payload)
        parsed = _parse_json_reply(raw_text) if raw_text else None

        usage = _extract_usage(payload)
        if usage is not None:
            input_tokens, output_tokens = usage
            token_source = "api"
        else:
            input_tokens = _count_tokens_fallback(prompt)
            output_tokens = _count_tokens_fallback(raw_text)
            token_source = "tiktoken"

        if parsed is None:
            return LyzrResponse(
                category="unknown", root_cause="unknown", remediation="escalate_to_human",
                confidence=0.0, needs_human=True,
                reasoning=f"Could not parse model reply as JSON: {raw_text[:200]!r}",
                input_tokens=input_tokens, output_tokens=output_tokens,
                token_source=token_source, latency_ms=latency_ms, raw_text=raw_text,
                parse_error=True, attempts=attempt,
            )

        return LyzrResponse(
            category=str(parsed.get("category", "unknown")),
            root_cause=str(parsed.get("root_cause", "unknown")),
            remediation=str(parsed.get("remediation", "escalate_to_human")),
            confidence=float(parsed.get("confidence", 0.0) or 0.0),
            needs_human=bool(parsed.get("needs_human", False)),
            reasoning=str(parsed.get("reasoning", "")),
            input_tokens=input_tokens, output_tokens=output_tokens,
            token_source=token_source, latency_ms=latency_ms, raw_text=raw_text,
            parse_error=False, attempts=attempt,
        )


class MockOracle:
    """Local, deterministic, keyword-based stand-in for the Lyzr agent.

    NOT SUBMITTABLE — exists purely so `python run.py --mock` can exercise
    the full pipeline (noise filter, dedup, gating, audit, metrics) offline.
    Every LyzrResponse it returns is unambiguously produced without a real
    model call; run.py labels mock runs in its output.
    """

    # signature substrings -> (category, root_cause, remediation, confidence)
    _RULES: list[tuple[str, tuple[str, str, str, float]]] = [
        ("rate limit exceeded", ("capacity", "rate_limit_breach", "add_backpressure_and_request_quota_increase", 0.93)),
        ("connection timeout to postgres", ("dependency_failure", "db_connection_pool_exhausted", "increase_pool_size_and_add_timeout_retry", 0.91)),
        ("disk usage", ("resource_exhaustion", "disk_full", "rotate_logs_and_expand_volume", 0.92)),
        ("kafka consumer lag", ("capacity", "consumer_lag", "scale_consumers_and_check_poison_message", 0.9)),
        ("circuit breaker open", ("dependency_failure", "upstream_outage", "enable_fallback_queue_and_alert_vendor", 0.9)),
        ("deadlock detected", ("code_defect", "db_deadlock", "reorder_locks_and_add_retry_with_backoff", 0.9)),
        ("nullpointerexception", ("code_defect", "null_pointer", "ship_hotfix_null_guard", 0.94)),
        ("slow query", ("performance", "missing_index", "add_index_and_review_query_plan", 0.9)),
        ("certificate expired", ("config_error", "expired_cert", "rotate_certificate_and_add_expiry_alert", 0.93)),
        ("outofmemoryerror", ("resource_exhaustion", "memory_leak", "restart_pod_and_raise_heap_limit", 0.92)),
    ]

    def classify(self, service: str, severity: str, message: str, extra_context: str = "") -> LyzrResponse:
        start = time.perf_counter()
        time.sleep(random.uniform(0.01, 0.03))  # simulate small latency
        msg_lower = message.lower()
        for needle, (cat, rc, rem, conf) in self._RULES:
            if needle in msg_lower:
                latency_ms = (time.perf_counter() - start) * 1000
                reasoning = f"[MOCK] matched pattern '{needle}'"
                if extra_context:
                    reasoning += f" (tool context received: {extra_context[:60]}...)"
                raw = json.dumps({
                    "category": cat, "root_cause": rc, "remediation": rem,
                    "confidence": conf, "needs_human": False, "reasoning": reasoning,
                })
                return LyzrResponse(
                    category=cat, root_cause=rc, remediation=rem, confidence=conf,
                    needs_human=False, reasoning=reasoning,
                    input_tokens=_count_tokens_fallback(message), output_tokens=_count_tokens_fallback(raw),
                    token_source="tiktoken", latency_ms=latency_ms, raw_text=raw,
                )
        latency_ms = (time.perf_counter() - start) * 1000
        reasoning = "[MOCK] no known pattern matched"
        raw = json.dumps({
            "category": "unknown", "root_cause": "unknown", "remediation": "escalate_to_human",
            "confidence": 0.3, "needs_human": True, "reasoning": reasoning,
        })
        return LyzrResponse(
            category="unknown", root_cause="unknown", remediation="escalate_to_human",
            confidence=0.3, needs_human=True, reasoning=reasoning,
            input_tokens=_count_tokens_fallback(message), output_tokens=_count_tokens_fallback(raw),
            token_source="tiktoken", latency_ms=latency_ms, raw_text=raw,
        )


def make_client(agent_id: str) -> Any:
    """Factory: returns a MockOracle if LYZR_MOCK=1, else a real LyzrClient."""
    if config.LYZR_MOCK:
        logger.info("LYZR_MOCK=1 -> using local MockOracle (NOT submittable results)")
        return MockOracle()
    config.require_real_credentials()
    return LyzrClient(agent_id=agent_id)
