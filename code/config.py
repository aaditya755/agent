"""Configuration module for Ghost Data Still Exists deletion-audit solution.

All repository paths are resolved dynamically relative to the repository root.
No absolute paths, secrets, or hardcoded dataset shortcuts are permitted.
"""

from pathlib import Path
import os
import sys
from dotenv import load_dotenv

# Ensure flat imports within code/ directory
_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

# Repository Root & Path Resolution
REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = REPO_ROOT / "dataset"
EVIDENCE_DIR = DATASET_DIR / "evidence"
CACHE_DIR = REPO_ROOT / "cache"
CACHE_LLM_DIR = CACHE_DIR / "llm"
USAGE_LEDGER_PATH = CACHE_DIR / "usage.jsonl"
FACTS_DB_PATH = CACHE_DIR / "facts.sqlite"

# Load environment variables from .env
load_dotenv(REPO_ROOT / ".env")

# Primary Dataset File Paths
DELETION_REQUESTS_PATH = DATASET_DIR / "deletion_requests.csv"
SAMPLE_DELETION_REQUESTS_PATH = DATASET_DIR / "sample_deletion_requests.csv"
SAMPLE_OUTPUT_PATH = DATASET_DIR / "sample_output.csv"
USER_IDENTITIES_PATH = DATASET_DIR / "user_identities.csv"
USER_AUDIT_LOG_PATH = DATASET_DIR / "user_audit_log.csv"
SYSTEM_ACTIVITY_PATH = DATASET_DIR / "system_activity.csv"
DELETION_RULES_PATH = DATASET_DIR / "deletion_rules.csv"
EVIDENCE_FILES_PATH = DATASET_DIR / "evidence_files.csv"
OUTPUT_PATH = REPO_ROOT / "output.csv"

# Canonical Phasing Hierarchy (C03)
PHASE_ORDER = {
    "FINDING": 0,
    "REMEDIATION": 1,
    "VERIFICATION": 2,
}

# Canonical Operation Hierarchy (C01, C03)
OPERATION_ORDER = {
    "FINDING": {
        "REPORT_REMAINING_RECORD": 0,
        "REPORT_EXPOSED_PII": 1,
        "REPORT_ACTIVE_DATA_FLOW": 2,
        "REPORT_RESTORE_RISK": 3,
    },
    "REMEDIATION": {
        "DISABLE_DATA_FLOW": 0,
        "BLOCK_RESTORE": 1,
        "DELETE_RECORD": 2,
        "REDACT_RECORD": 3,
        "ANONYMIZE_RECORD": 4,
    },
    "VERIFICATION": {
        "VERIFY_RECORD_ABSENT": 0,
        "VERIFY_RECORD_PRESENT": 1,
        "VERIFY_RECORD_REDACTED": 2,
        "VERIFY_RECORD_ANONYMIZED": 3,
        "VERIFY_DATA_FLOW_DISABLED": 4,
        "VERIFY_RESTORE_BLOCKED": 5,
    },
}

# Config-Flag Defaults (C11)
VERIFY_COMPLIANT_IN_INCOMPLETE = "account+retained+remediated only"
LEGAL_HOLD_REGISTER_STEPS = False
SECONDARY_IDENTIFIER_STEPS = False


def _get_required_env(var_name: str) -> str:
    val = os.getenv(var_name)
    if not val or not val.strip():
        raise ValueError(
            f"Environment variable '{var_name}' is unset. Please configure it in .env or your shell environment."
        )
    return val.strip()


def get_gemini_api_key() -> str:
    """Retrieve GEMINI_API_KEY from environment. Fails clearly if unset."""
    return _get_required_env("GEMINI_API_KEY")


def get_model_agent() -> str:
    """Retrieve GEMINI_MODEL_AGENT from environment. Fails clearly if unset."""
    return _get_required_env("GEMINI_MODEL_AGENT")


def get_model_escalation() -> str:
    """Retrieve GEMINI_MODEL_ESCALATION from environment. Fails clearly if unset."""
    return _get_required_env("GEMINI_MODEL_ESCALATION")


def get_model_vision() -> str:
    """Retrieve GEMINI_MODEL_VISION from environment. Fails clearly if unset."""
    return _get_required_env("GEMINI_MODEL_VISION")


def __getattr__(name: str):
    if name == "GEMINI_API_KEY":
        return get_gemini_api_key()
    elif name == "GEMINI_MODEL_AGENT":
        return get_model_agent()
    elif name == "GEMINI_MODEL_ESCALATION":
        return get_model_escalation()
    elif name == "GEMINI_MODEL_VISION":
        return get_model_vision()
    elif name == "DEFAULT_MODEL":
        return os.getenv("GEMINI_MODEL_AGENT", "gemini-3.8-flash")
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
