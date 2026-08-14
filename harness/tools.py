"""
A single read-only diagnostic tool: resource ownership lookup.

Every ambiguous category disagreement this harness ever measured (rate
limit, connection pool, TLS cert — see README "Sample real runs") comes
down to the same question: does the resource named in the log line belong
to *us*, or is it a third party's? Text alone under-determines that; a real
production system would answer it with a service catalog / CMDB / DNS-zone
lookup. This module simulates that lookup — clearly labeled as such, the
way lyzr_client.MockOracle is — but the MECHANISM is real: pipeline.py
decides, per event, whether the question is worth asking, calls this tool
(no LLM, no network, just a lookup), and feeds the answer back to the model
for a grounded second pass instead of a text-only guess.

The ownership facts below are handwritten from the dataset's own service
list (a real deployment would point this at an actual service registry).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Simulated CMDB / service-catalog: resource name (as it appears in a log
# line) -> who owns it. In production this is a real lookup (service mesh
# registry, DNS zone ownership, vendor-integration inventory) — this dict
# stands in for that system for demo purposes only.
_REGISTRY: dict[str, dict] = {
    "search-index": {
        "owner": "internal",
        "system": "search-index (Elasticsearch cluster)",
        "evidence": "search-index is our own cluster, owned by the search-platform team — not a third-party service.",
    },
    "postgres": {
        "owner": "internal",
        "system": "postgres (primary RDS instance)",
        "evidence": "This postgres instance is our own primary database, owned by the data-platform team.",
    },
    "cdn.lyzr.io": {
        "owner": "internal",
        "system": "cdn.lyzr.io (edge CDN)",
        "evidence": "cdn.lyzr.io sits in our own lyzr.io DNS zone, owned by the platform team — its certificate is ours to rotate, not a vendor's.",
    },
    "payments-provider": {
        "owner": "external",
        "system": "payments-provider (third-party vendor)",
        "evidence": "payments-provider is an external vendor integration, not in our infrastructure — we don't control its uptime.",
    },
    "payments.events": {
        "owner": "internal",
        "system": "payments.events (Kafka topic)",
        "evidence": "payments.events runs on our own internal Kafka cluster, owned by the platform team.",
    },
}


@dataclass
class OwnershipResult:
    resource: str
    owner: str  # "internal" | "external"
    system: str
    evidence: str


def check_resource_ownership(text: str) -> Optional[OwnershipResult]:
    """Scan `text` for any resource name in the simulated registry and
    return its ownership record, or None if nothing recognized. First match
    wins; the registry is small and deliberately non-overlapping."""
    text_lower = text.lower()
    for resource, record in _REGISTRY.items():
        if resource.lower() in text_lower:
            return OwnershipResult(resource=resource, owner=record["owner"], system=record["system"], evidence=record["evidence"])
    return None
