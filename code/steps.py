"""Step compilation and serialization module.

Compiles baseline findings, remediation, and verification steps into
strictly sorted, sequentially numbered, validated step sequences and JSON.
"""

import json
from typing import Dict, Any, List, Tuple

from config import PHASE_ORDER, OPERATION_ORDER


def sort_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort steps strictly across four canonical tiers per C03:
    1. phase_rank (FINDING < REMEDIATION < VERIFICATION)
    2. operation_rank (canonical order within phase)
    3. system_id (alphabetical ascending)
    4. resource_id (alphabetical ascending)
    """
    def step_sort_key(s: Dict[str, Any]) -> Tuple[int, int, str, str]:
        p_rank = PHASE_ORDER.get(s["phase"], 99)
        op_rank = OPERATION_ORDER.get(s["phase"], {}).get(s["operation"], 99)
        return (p_rank, op_rank, s["system_id"], s["resource_id"])

    sorted_steps = sorted(steps, key=step_sort_key)
    # Renumber consecutively from 1
    for idx, st in enumerate(sorted_steps, 1):
        st["step_no"] = idx
    return sorted_steps


def serialize_steps(steps: List[Dict[str, Any]]) -> str:
    """Serialize step list into standard compact JSON without unneeded whitespace."""
    clean_steps = []
    for s in steps:
        clean_steps.append({
            "step_no": s["step_no"],
            "phase": s["phase"],
            "system_id": s["system_id"],
            "resource_id": s["resource_id"],
            "operation": s["operation"],
            "state": s["state"],
            "evidence_id": s.get("evidence_id", ""),
        })
    return json.dumps(clean_steps, separators=(",", ":"))
