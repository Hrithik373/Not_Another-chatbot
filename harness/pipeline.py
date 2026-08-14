"""
Shared decision logic: noise filtering, closed-set validation, and confidence
gating. This module holds the building blocks; baseline.py and optimized.py
compose them into two different pipelines so the delta between them is real
and reproducible (not two code paths pretending to differ).

    baseline.py  = model(every raw event)                         + validate + gate
    optimized.py = noise_filter -> dedup -> model(unique sigs only) + validate + gate
"""
from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import config
import tools
from lyzr_client import LyzrResponse
from normalize import normalize_message, signature_hash

logger = logging.getLogger("pipeline")


@dataclass
class EventResult:
    event_id: str
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
    path: str  # noise_filter | model | cache
    free_form_violation: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    token_source: str = ""
    latency_ms: float = 0.0
    model_call: bool = False  # True only for the one call that produced this signature's decision
    api_calls: int = 0  # real LLM API calls behind this decision (0 noise/cache, 1 normal, k if vote_k>1)
    tool_used: bool = False  # True if investigate_with_tool fired a grounded follow-up call
    tool_evidence: str = ""  # the ownership fact fed back to the model, if any
    raw_text: str = ""


def is_noise(severity: str, message: str) -> bool:
    """Cheap-path filter, no LLM involved: INFO/DEBUG severity with no
    error-ish keyword is treated as pure noise."""
    if severity not in config.NOISE_SEVERITIES:
        return False
    msg_lower = message.lower()
    return not any(kw in msg_lower for kw in config.NOISE_KEYWORDS)


def make_noise_result(event_id: str, service: str, severity: str, message: str) -> EventResult:
    sig = normalize_message(message)
    return EventResult(
        event_id=event_id, service=service, severity=severity, message=message,
        signature=sig, category="noise", root_cause="unknown", remediation="none",
        confidence=1.0, needs_human=False, reasoning="cheap-path noise filter: low severity, no error keyword",
        path=config.PATH_NOISE_FILTER,
    )


def validate_and_gate(resp: LyzrResponse) -> tuple[str, str, str, float, bool, str, bool]:
    """Apply closed-set validation then the confidence gate to a raw model
    response. Returns (category, root_cause, remediation, confidence,
    needs_human, reasoning, free_form_violation)."""
    category, root_cause, remediation = resp.category, resp.root_cause, resp.remediation
    confidence, needs_human, reasoning = resp.confidence, resp.needs_human, resp.reasoning

    violation = (
        category not in config.CATEGORIES
        or root_cause not in config.ROOT_CAUSES
        or remediation not in config.REMEDIATIONS
    )
    if violation:
        logger.warning(
            "Free-form violation: category=%r root_cause=%r remediation=%r -> forcing escalate_to_human",
            category, root_cause, remediation,
        )
        reasoning = f"[closed-set violation, forced escalation] raw=({category!r}, {root_cause!r}, {remediation!r}); {reasoning}"
        category, root_cause, remediation = "unknown", "unknown", "escalate_to_human"
        needs_human = True
        confidence = min(confidence, 0.0)

    if not violation and (category == "unknown" or confidence < config.CONFIDENCE_THRESHOLD):
        needs_human = True
        if remediation not in ("escalate_to_human", "none"):
            reasoning = f"[low confidence {confidence:.2f} < {config.CONFIDENCE_THRESHOLD}, forced escalation] {reasoning}"
            remediation = "escalate_to_human"

    return category, root_cause, remediation, confidence, needs_human, reasoning, violation


def make_model_result(
    event_id: str, service: str, severity: str, message: str, signature: str,
    resp: LyzrResponse, path: str, model_call: bool, api_calls: Optional[int] = None,
    tool_used: bool = False, tool_evidence: str = "",
) -> EventResult:
    category, root_cause, remediation, confidence, needs_human, reasoning, violation = validate_and_gate(resp)
    return EventResult(
        event_id=event_id, service=service, severity=severity, message=message,
        signature=signature, category=category, root_cause=root_cause, remediation=remediation,
        confidence=confidence, needs_human=needs_human, reasoning=reasoning, path=path,
        free_form_violation=violation, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
        token_source=resp.token_source, latency_ms=resp.latency_ms, model_call=model_call,
        api_calls=api_calls if api_calls is not None else (1 if model_call else 0),
        tool_used=tool_used, tool_evidence=tool_evidence,
        raw_text=resp.raw_text,
    )


def make_raw_result(
    event_id: str, service: str, severity: str, message: str, signature: str,
    resp: LyzrResponse, path: str, model_call: bool, api_calls: Optional[int] = None,
    tool_used: bool = False, tool_evidence: str = "",
) -> EventResult:
    """Build an EventResult straight from the model response with NO
    closed-set validation and NO confidence gating applied. Used only by
    ablation.py to measure the marginal value the gating lever adds on top
    of noise-filter/dedup — never used for the real optimized pipeline."""
    violation = (
        resp.category not in config.CATEGORIES
        or resp.root_cause not in config.ROOT_CAUSES
        or resp.remediation not in config.REMEDIATIONS
    )
    return EventResult(
        event_id=event_id, service=service, severity=severity, message=message,
        signature=signature, category=resp.category, root_cause=resp.root_cause,
        remediation=resp.remediation, confidence=resp.confidence, needs_human=resp.needs_human,
        reasoning=resp.reasoning, path=path, free_form_violation=violation,
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
        token_source=resp.token_source, latency_ms=resp.latency_ms, model_call=model_call,
        api_calls=api_calls if api_calls is not None else (1 if model_call else 0),
        tool_used=tool_used, tool_evidence=tool_evidence,
        raw_text=resp.raw_text,
    )


def majority_vote(responses: list[LyzrResponse]) -> LyzrResponse:
    """Reduce k independent classifications of the same signature to one.

    Votes on `category` (the field that showed real run-to-run LLM sampling
    variance in practice); root_cause/remediation are taken from whichever
    response cast the winning vote, since they're already highly correlated
    with category and near-perfectly stable on their own. Confidence is
    dampened by the agreement fraction (3/3 -> unchanged, 2/3 -> x0.67, no
    majority -> x(1/k)) so a split vote is *itself* a signal that naturally
    pushes borderline cases through the existing confidence gate instead of
    silently picking a coin-flip winner.
    """
    if len(responses) == 1:
        return responses[0]

    counts = Counter(r.category for r in responses)
    top_category, top_n = counts.most_common(1)[0]
    agreement = top_n / len(responses)
    candidates = [r for r in responses if r.category == top_category]
    winner = max(candidates, key=lambda r: r.confidence)

    return LyzrResponse(
        category=winner.category,
        root_cause=winner.root_cause,
        remediation=winner.remediation,
        confidence=winner.confidence * agreement,
        needs_human=winner.needs_human,
        reasoning=f"[majority vote {top_n}/{len(responses)} -> {top_category!r}] {winner.reasoning}",
        input_tokens=sum(r.input_tokens for r in responses),
        output_tokens=sum(r.output_tokens for r in responses),
        token_source=winner.token_source,
        latency_ms=max(r.latency_ms for r in responses),  # calls run concurrently
        raw_text=winner.raw_text,
        parse_error=winner.parse_error,
        attempts=max(r.attempts for r in responses),
        error=winner.error,
    )


def classify_with_voting(client, service: str, severity: str, message: str, vote_k: int = 1) -> tuple[LyzrResponse, int]:
    """Call the model `vote_k` times concurrently for the same event and
    majority-vote the result. vote_k=1 (the default everywhere) is exactly
    the original single-call behavior. Returns (response, real_api_calls_made)
    so callers can report accurate cost/call-count metrics."""
    if vote_k <= 1:
        return client.classify(service, severity, message), 1
    with ThreadPoolExecutor(max_workers=vote_k) as pool:
        futures = [pool.submit(client.classify, service, severity, message) for _ in range(vote_k)]
        responses = [f.result() for f in futures]
    return majority_vote(responses), vote_k


def investigate_with_tool(
    client, service: str, severity: str, message: str, resp: LyzrResponse,
) -> tuple[LyzrResponse, Optional[tools.OwnershipResult], int]:
    """Agentic tool-use step: if the first-pass category is one where "is
    this fault ours or a third party's" is the deciding factor (see
    config.OWNERSHIP_SENSITIVE_CATEGORIES), check the event against the
    simulated service-ownership registry (tools.py — no LLM call, a plain
    lookup). If it recognizes a resource named in the message, make ONE
    additional grounded call with that fact attached and let the model
    re-decide with real (simulated) evidence instead of guessing from text
    alone. Triggered by category, not confidence: the disagreements this
    targets are cases where the agent is *confidently* wrong, so a
    confidence-band trigger would miss them.

    Returns (final_response, ownership_result_or_None, extra_api_calls).
    """
    if not config.TOOLS_ENABLED:
        return resp, None, 0
    if resp.category not in config.OWNERSHIP_SENSITIVE_CATEGORIES:
        return resp, None, 0

    ownership = tools.check_resource_ownership(message)
    if ownership is None:
        return resp, None, 0

    extra_context = (
        f"resource ownership lookup for {ownership.resource!r} -> owner={ownership.owner} "
        f"({ownership.system}). {ownership.evidence} Use this alongside the fault type, not "
        f"ownership alone — these four are distinct concepts, not interchangeable: "
        f"(1) capacity = a rate limit, quota, or consumer-lag threshold was breached; "
        f"(2) resource_exhaustion = the service's OWN standalone resource (its own memory or "
        f"disk) literally ran out — never used for rate/quota/lag, that's always capacity; "
        f"(3) dependency_failure = the service failed to successfully complete a call to ANOTHER "
        f"system in its request path (a connection timeout, a non-2xx response), regardless of "
        f"whether that system is ours or a vendor's — the fault is in the interaction, not the "
        f"resource; (4) config_error = a static configuration issue (an expired certificate, a "
        f"bad setting) rather than a live resource, call, or threshold."
    )
    second_resp = client.classify(service, severity, message, extra_context=extra_context)
    second_resp.reasoning = f"[tool: resource_ownership={ownership.owner}] {second_resp.reasoning}"
    return second_resp, ownership, 1


def classify_event(client, service: str, severity: str, message: str, vote_k: int = 1) -> tuple[LyzrResponse, int, Optional[tools.OwnershipResult]]:
    """Full model-classification orchestration for one event: majority-vote
    across vote_k calls, then an optional tool-grounded investigation pass
    on top. This is the single entry point optimized.py and server.py use.
    Returns (final_response, total_real_api_calls, ownership_result_or_None).
    """
    resp, n_calls = classify_with_voting(client, service, severity, message, vote_k)
    resp, ownership, extra_calls = investigate_with_tool(client, service, severity, message, resp)
    return resp, n_calls + extra_calls, ownership


class SignatureCache:
    """signature -> LyzrResponse. Same signature never calls the model twice
    (idempotency requirement from the API contract), and is the mechanism the
    optimized pipeline uses to fan one decision back out to a whole cluster."""

    def __init__(self):
        self._store: dict[str, LyzrResponse] = {}
        self.hits = 0
        self.misses = 0

    def get(self, signature: str) -> Optional[LyzrResponse]:
        resp = self._store.get(signature)
        if resp is not None:
            self.hits += 1
        return resp

    def put(self, signature: str, resp: LyzrResponse) -> None:
        self._store[signature] = resp
        self.misses += 1

    def peek(self, signature: str) -> Optional[LyzrResponse]:
        """Lookup without affecting hit/miss counters — used when the caller
        already knows the cache state and just needs the stored value."""
        return self._store.get(signature)

    def __len__(self) -> int:
        return len(self._store)
