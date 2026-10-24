"""Configuration module for Ghost Data Still Exists deletion-audit solution.

All repository paths are resolved dynamically relative to the repository root.
No absolute paths, secrets, or hardcoded dataset shortcuts are permitted.
"""

from pathlib import Path
import os
import sys

# Ensure flat imports within code/ directory
_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

# Repository Root & Path Resolution (Path(__file__).resolve().parents[1])
REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = REPO_ROOT / "dataset"
EVIDENCE_DIR = DATASET_DIR / "evidence"
CACHE_DIR = REPO_ROOT / "cache"
FACTS_DB_PATH = CACHE_DIR / "facts.sqlite"

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

# Model Names from Environment Variables
DEFAULT_MODEL = os.getenv("MODEL_NAME", "gemini-3.8-flash")
VLM_MODEL = os.getenv("VLM_MODEL_NAME", DEFAULT_MODEL)
