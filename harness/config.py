"""
Central configuration for the Auto-Remediation harness.

Everything that varies between a reviewer's machine and this one lives here,
driven entirely by environment variables (loaded from a local .env if present).
Nothing in this module makes a network call or reads the dataset — it only
defines constants, closed sets, and env-derived settings so every other module
can import it cheaply and deterministically.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load a .env file sitting next to this file (if any) before reading os.environ.
load_dotenv(Path(__file__).resolve().parent / ".env")

# --------------------------------------------------------------------------
# Lyzr API
# --------------------------------------------------------------------------
LYZR_API_URL = "https://agent-prod.studio.lyzr.ai/v3/inference/chat/"
LYZR_API_KEY = os.environ.get("LYZR_API_KEY", "")
LYZR_AGENT_ID = os.environ.get("LYZR_AGENT_ID", "")
# Baseline may point at a bigger/pricier model on purpose; defaults to the same agent.
LYZR_BASELINE_AGENT_ID = os.environ.get("LYZR_BASELINE_AGENT_ID") or LYZR_AGENT_ID
LYZR_USER_ID = os.environ.get("LYZR_USER_ID", "harness@lyzr.local")

# --------------------------------------------------------------------------
# Public-demo safeguards for server.py's POST /api/batch (runs the full
# optimized pipeline — real API cost). Unset by default (no restriction) for
# local dev; a hosted/public deployment should set at least the cooldown,
# and PUBLIC_BATCH_TOKEN if the link may be shared/crawled.
# --------------------------------------------------------------------------
PUBLIC_BATCH_TOKEN = os.environ.get("PUBLIC_BATCH_TOKEN", "")  # empty = no token check
BATCH_MIN_INTERVAL_SECONDS = float(os.environ.get("BATCH_MIN_INTERVAL_SECONDS", "15"))

# LYZR_MOCK=1 swaps the real HTTP client for a local oracle. This is strictly
# for exercising the pipeline plumbing offline — it is NOT a submittable
# result and every code path that uses it labels its output accordingly.
LYZR_MOCK = os.environ.get("LYZR_MOCK", "0") == "1"

# Retries: exponential backoff on 429 / 5xx / timeout.
MAX_RETRIES = 4
RETRY_BASE_DELAY_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 30

# --------------------------------------------------------------------------
# Pricing (USD per 1M tokens) — override via env for whatever model backs the
# agent_id you configured. Defaults are a reasonable mid-tier placeholder.
# --------------------------------------------------------------------------
PRICE_INPUT_PER_M = float(os.environ.get("PRICE_INPUT_PER_M", "0.25"))
PRICE_OUTPUT_PER_M = float(os.environ.get("PRICE_OUTPUT_PER_M", "1.25"))

# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------
DATA_PATH = os.environ.get("DATA_PATH") or str(Path(__file__).resolve().parent / "data" / "track_a_logs.csv")

# --------------------------------------------------------------------------
# Runs / audit
# --------------------------------------------------------------------------
RUNS_DIR = Path(__file__).resolve().parent / "runs"

# --------------------------------------------------------------------------
# Closed sets — model output outside these EXACT strings is a violation.
# --------------------------------------------------------------------------
CATEGORIES = {
    "capacity",
    "code_defect",
    "config_error",
    "dependency_failure",
    "performance",
    "resource_exhaustion",
    "noise",
    "unknown",
}

ROOT_CAUSES = {
    "consumer_lag",
    "db_connection_pool_exhausted",
    "db_deadlock",
    "disk_full",
    "expired_cert",
    "memory_leak",
    "missing_index",
    "null_pointer",
    "rate_limit_breach",
    "upstream_outage",
    "unknown",
}

REMEDIATIONS = {
    "add_backpressure_and_request_quota_increase",
    "add_index_and_review_query_plan",
    "enable_fallback_queue_and_alert_vendor",
    "increase_pool_size_and_add_timeout_retry",
    "reorder_locks_and_add_retry_with_backoff",
    "restart_pod_and_raise_heap_limit",
    "rotate_certificate_and_add_expiry_alert",
    "rotate_logs_and_expand_volume",
    "scale_consumers_and_check_poison_message",
    "ship_hotfix_null_guard",
    "none",
    "escalate_to_human",
}

# --------------------------------------------------------------------------
# Pipeline thresholds
# --------------------------------------------------------------------------
CONFIDENCE_THRESHOLD = 0.55

# Majority-vote redundancy per unique signature in the optimized pipeline
# (pipeline.classify_with_voting). 1 = original single-call behavior (the
# default everywhere, incl. every number in the README's main tables). Set
# >1 (e.g. 3) to trade back a slice of the cost win for stability against
# LLM sampling variance on ambiguous category calls — see README "Sample
# real runs" for why this exists. baseline.py never uses this; it's an
# optimized-pipeline-only lever, and ablation.py's own stages are unaffected
# unless explicitly passed a vote_k.
VOTE_K = int(os.environ.get("VOTE_K", "1"))

# Tool-calling: on the categories below, whether a fault is "ours" or a
# third party's is the deciding factor and text alone under-determines it
# (this is the exact, measured cause of every category disagreement found
# against the live agent — see README "Sample real runs"). When enabled,
# pipeline.investigate_with_tool checks the event against a simulated
# service-ownership registry (tools.py) and, if it recognizes the named
# resource, makes ONE additional grounded model call with that fact
# attached. Triggered by category, not by confidence — the diagnosed
# failure mode is the agent being *confidently* wrong, so a confidence-band
# trigger would miss it. Default on: it's cheap (fires on at most the
# handful of unique signatures that reference a cataloged resource) and
# targets a real, measured gap. Set TOOLS_ENABLED=0 to compare against the
# non-tool-assisted baseline.
TOOLS_ENABLED = os.environ.get("TOOLS_ENABLED", "1") == "1"
OWNERSHIP_SENSITIVE_CATEGORIES = {"config_error", "capacity", "resource_exhaustion", "dependency_failure"}

# Cheap-path noise filter: severity in this set AND no error-ish keyword below.
NOISE_SEVERITIES = {"INFO", "DEBUG"}
NOISE_KEYWORDS = (
    "error", "exception", "fail", "timeout", "oom", "fatal",
    "panic", "denied", "refused",
)

# --------------------------------------------------------------------------
# Decision paths (for audit / metrics)
# --------------------------------------------------------------------------
PATH_NOISE_FILTER = "noise_filter"
PATH_MODEL = "model"
PATH_CACHE = "cache"

# --------------------------------------------------------------------------
# Targets checked/flagged by run.py
# --------------------------------------------------------------------------
TARGET_MACRO_F1 = 0.85
TARGET_ROOT_CAUSE_ACC = 0.80
TARGET_FREE_FORM_RATE = 0.0
TARGET_P95_ESCALATED_SECONDS = 4.0
TARGET_COST_CUT_PCT = 0.50


def require_real_credentials() -> None:
    """Raise a clear error if a real (non-mock) run is attempted without creds."""
    if LYZR_MOCK:
        return
    missing = [name for name, val in (("LYZR_API_KEY", LYZR_API_KEY), ("LYZR_AGENT_ID", LYZR_AGENT_ID)) if not val]
    if missing:
        raise RuntimeError(
            f"Missing required env var(s): {', '.join(missing)}. "
            "Set them or pass --mock to run against the local oracle."
        )
