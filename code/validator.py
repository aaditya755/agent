"""Output and step sequence validation module.

Strictly verifies output.csv format, row count, sorting, step schemas,
phase/operation/state tuples, sequential numbering, and temporal point-in-time validity.
"""

import csv
import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from config import (
    PHASE_ORDER,
    OPERATION_ORDER,
    EVIDENCE_FILES_PATH,
    DELETION_REQUESTS_PATH,
    SAMPLE_DELETION_REQUESTS_PATH,
)

VALID_STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}

VALID_OPERATIONS_STATES = {
    "FINDING": {
        ("REPORT_REMAINING_RECORD", "PRESENT"),
        ("REPORT_EXPOSED_PII", "PRESENT"),
        ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
        ("REPORT_RESTORE_RISK", "ACTIVE"),
    },
    "REMEDIATION": {
        ("DISABLE_DATA_FLOW", "DISABLED"),
        ("BLOCK_RESTORE", "BLOCKED"),
        ("DELETE_RECORD", "ABSENT"),
        ("REDACT_RECORD", "REDACTED"),
        ("ANONYMIZE_RECORD", "ANONYMIZED"),
    },
    "VERIFICATION": {
        ("VERIFY_RECORD_ABSENT", "ABSENT"),
        ("VERIFY_RECORD_PRESENT", "PRESENT"),
        ("VERIFY_RECORD_REDACTED", "REDACTED"),
        ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
        ("VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
        ("VERIFY_RESTORE_BLOCKED", "BLOCKED"),
    },
}

REQUIRED_STEP_FIELDS = {
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
}


def load_evidence_manifest() -> Dict[str, str]:
    """Load map of file_id -> captured_at from evidence_files.csv."""
    manifest = {}
    if EVIDENCE_FILES_PATH.exists():
        with open(EVIDENCE_FILES_PATH, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                manifest[row["file_id"]] = row["captured_at"]
    return manifest


def load_audit_cutoffs(sample: bool = False) -> Dict[str, str]:
    """Load map of case_id -> audit_at cutoff."""
    path = SAMPLE_DELETION_REQUESTS_PATH if sample else DELETION_REQUESTS_PATH
    cutoffs = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cutoffs[row["case_id"]] = row["audit_at"]
    return cutoffs


def validate_steps(
    case_id: str,
    status: str,
    steps: List[Dict[str, Any]],
    audit_at: Optional[str] = None,
    evidence_manifest: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Validate a single step sequence, returning a list of error strings."""
    errors = []

    if status == "NOT_APPLICABLE":
        if len(steps) != 0:
            errors.append(f"{case_id}: NOT_APPLICABLE case must have empty steps_json, got {len(steps)} steps")
        return errors

    if len(steps) == 0:
        errors.append(f"{case_id}: {status} case cannot have empty steps_json")
        return errors

    prev_sort_key: Optional[Tuple[int, int, str, str]] = None

    for idx, st in enumerate(steps, 1):
        # 1. Required keys
        keys = set(st.keys())
        if keys != REQUIRED_STEP_FIELDS:
            errors.append(f"{case_id} step {idx}: keys mismatch. Expected {REQUIRED_STEP_FIELDS}, got {keys}")

        # 2. Sequential step_no
        s_no = st.get("step_no")
        if s_no != idx:
            errors.append(f"{case_id} step {idx}: non-sequential step_no {s_no} (expected {idx})")

        phase = st.get("phase", "")
        op = st.get("operation", "")
        state = st.get("state", "")
        ev_id = st.get("evidence_id", "")
        sys_id = st.get("system_id", "")
        res_id = st.get("resource_id", "")

        # 3. Phase, Operation, State tuple validity
        if phase not in VALID_OPERATIONS_STATES:
            errors.append(f"{case_id} step {idx}: invalid phase {phase}")
        elif (op, state) not in VALID_OPERATIONS_STATES[phase]:
            errors.append(f"{case_id} step {idx}: invalid op/state pair ({op}, {state}) for phase {phase}")

        # 4. Canonical sorting order
        if phase in PHASE_ORDER and op in OPERATION_ORDER.get(phase, {}):
            sort_key = (PHASE_ORDER[phase], OPERATION_ORDER[phase][op], sys_id, res_id)
            if prev_sort_key is not None and sort_key < prev_sort_key:
                errors.append(f"{case_id} step {idx}: sort order violation ({op}, {sys_id}, {res_id}) < previous")
            prev_sort_key = sort_key

        # 5. Evidence citation rules
        if status == "INCOMPLETE":
            if phase in ("REMEDIATION", "VERIFICATION") and ev_id != "":
                errors.append(f"{case_id} step {idx}: future step in INCOMPLETE case must have evidence_id='', got '{ev_id}'")
            elif phase == "FINDING" and not ev_id:
                errors.append(f"{case_id} step {idx}: FINDING step must have non-empty evidence_id")
        elif status == "COMPLETE":
            if phase != "VERIFICATION":
                errors.append(f"{case_id} step {idx}: COMPLETE case must contain only VERIFICATION steps, got {phase}")

        # 6. Point-in-time temporal cutoff
        if ev_id and audit_at and evidence_manifest:
            ev_cap = evidence_manifest.get(ev_id)
            if ev_cap and ev_cap > audit_at:
                errors.append(f"{case_id} step {idx}: cited evidence {ev_id} (captured {ev_cap}) is after audit_at {audit_at}")

    return errors


def validate_output_file(
    output_path: Path,
    expected_count: int = 518,
    sample: bool = False,
) -> Tuple[bool, List[str]]:
    """Validate an output.csv file against all challenge schema and contract requirements."""
    errors = []
    if not output_path.exists():
        return False, [f"Output file does not exist: {output_path}"]

    manifest = load_evidence_manifest()
    cutoffs = load_audit_cutoffs(sample=sample)

    with open(output_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != ["case_id", "deletion_status", "steps_json"]:
            errors.append(f"Header mismatch: expected ['case_id', 'deletion_status', 'steps_json'], got {header}")

        rows = list(reader)

    if len(rows) != expected_count:
        errors.append(f"Row count mismatch: expected {expected_count}, got {len(rows)}")

    prev_case_num = -1
    for row_idx, row in enumerate(rows, 1):
        if len(row) != 3:
            errors.append(f"Row {row_idx}: expected 3 columns, got {len(row)}")
            continue

        cid, st, steps_raw = row

        # Case numerical ordering
        m = re.match(r"^case_(\d+)$", cid)
        if m:
            case_index = int(m.group(1))
            if case_index <= prev_case_num:
                errors.append(f"Row {row_idx}: case_id {cid} out of sort order (prev case index {prev_case_num})")
            prev_case_num = case_index
        else:
            errors.append(f"Row {row_idx}: invalid case_id format '{cid}'")

        if st not in VALID_STATUSES:
            errors.append(f"Row {row_idx} ({cid}): invalid deletion_status '{st}'")

        try:
            steps = json.loads(steps_raw)
            if not isinstance(steps, list):
                errors.append(f"Row {row_idx} ({cid}): steps_json is not an array")
                continue
        except Exception as e:
            errors.append(f"Row {row_idx} ({cid}): JSON parse error in steps_json: {e}")
            continue

        audit_at = cutoffs.get(cid)
        step_errors = validate_steps(cid, st, steps, audit_at, manifest)
        errors.extend(step_errors)

    return (len(errors) == 0, errors)
