"""Baseline deletion-audit engine.

Deterministic rules engine that builds a dossier for a case, applies deletion rules,
evaluates compliance at audit_at cutoff, and emits findings, remediation, and verification steps.
"""

import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Tuple

_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from config import (
    PHASE_ORDER,
    OPERATION_ORDER,
    VERIFY_COMPLIANT_IN_INCOMPLETE,
    LEGAL_HOLD_REGISTER_STEPS,
    SECONDARY_IDENTIFIER_STEPS,
)
from schemas import IdentityBag, Fact
import facts
from facts import normalize_system_id, PREFIX_SYSTEM_MAP, ensure_facts_store
import identity

NON_DELETION_PATTERNS = [
    r"leave the account active",
    r"not asking for deletion",
    r"keep the account",
    r"do not close the account",
    r"without removing any of my records",
    r"marketing lists only",
    r"not a request to erase",
    r"leave my profile",
    r"continue using the service",
    r"don't delete or change anything",
    r"\bnot an erasure\b",
    r"\bdo not (close|delete|erase|remove)\b",
    r"\bleave (the account|my profile)\b",
    r"\bkeep the account\b",
    r"\b(update|change|correct)\b.*?\b(profile|address|phone|email|details)\b",
    r"\b(invoice|receipt|statement|copy of my bill)\b",
    r"\b(opt[- ]?out|unsubscribe|marketing|newsletter)\b",
    r"\b(login|log in|password|reset|access)\b.*?\b(help|issue|assist|trouble)\b",
    r"\b(requesting an invoice|billing statement)\b",
]


def is_non_deletion_request(request_text: str, activities: List[Dict[str, Any]] = None) -> Tuple[bool, bool, bool]:
    """Determine whether a request is a non-deletion action (NOT_APPLICABLE).

    Returns:
        (is_non_del, has_event, has_pattern)
        Rule: has_event AND has_pattern => NOT_APPLICABLE.
    """
    has_event = False
    if activities:
        for a in activities:
            if a.get("event_type") == "CLASSIFY_REQUEST":
                has_event = True
                break

    text_lower = request_text.lower()
    has_pattern = any(re.search(pat, text_lower) for pat in NON_DELETION_PATTERNS)

    is_non_del = has_event and has_pattern
    return is_non_del, has_event, has_pattern


def _search_json_identifiers(obj: Any, id_strings: Set[str]) -> bool:
    """Recursively search for exact identifier matches in structured objects."""
    if isinstance(obj, str):
        return obj in id_strings
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                if v in id_strings:
                    return True
                if (v.startswith("{") and v.endswith("}")) or (v.startswith("[") and v.endswith("]")):
                    try:
                        if _search_json_identifiers(json.loads(v), id_strings):
                            return True
                    except Exception:
                        pass
            elif _search_json_identifiers(v, id_strings):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _search_json_identifiers(item, id_strings):
                return True
    return False


def dossier(email: str, audit_at: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Reconstruct all identity identifiers, linked resources, and evidence for a case at audit_at."""
    con = ensure_facts_store(db_path)
    cur = con.cursor()

    bag = identity.resolve(email, audit_at, db_path)
    uid = bag.user_id

    id_strings = {uid} | set(bag.emails) | set(bag.phones) | set(bag.email_hashes)
    id_strings.discard("")
    id_strings.discard(None)

    # 1. Gather all facts captured <= audit_at matching id_strings
    cur.execute("SELECT file_id, captured_at, system_id, topology, resource_id, kind, fields_json FROM facts WHERE captured_at <= ?", (audit_at,))
    all_facts = cur.fetchall()

    case_facts: Dict[str, List[Dict[str, Any]]] = {}
    for f in all_facts:
        fj = f["fields_json"]
        if any(ident in fj for ident in id_strings):
            try:
                jd = json.loads(fj)
                if _search_json_identifiers(jd, id_strings):
                    rid = f["resource_id"]
                    if rid and not rid.startswith("ORD-") and not rid.startswith("NST-"):
                        if rid not in case_facts:
                            case_facts[rid] = []
                        case_facts[rid].append(dict(f))
            except Exception:
                pass

    # 2. Reconstruct primary account ID from evidence files or activity
    acc_id = ""
    cur.execute("SELECT resource_id, fields_json FROM facts WHERE resource_id LIKE 'account_%'")
    for r in cur.fetchall():
        fj = r["fields_json"]
        if any(ident in fj for ident in id_strings):
            try:
                jd = json.loads(fj)
                if _search_json_identifiers(jd, id_strings):
                    acc_id = r["resource_id"]
                    break
            except Exception:
                pass

    if not acc_id and uid:
        cur.execute(
            """
            SELECT resource_id FROM system_activity
            WHERE source_system = 'deletion_worker' AND target_system = 'account_service'
              AND event_time <= ?
            ORDER BY event_time DESC LIMIT 1
            """,
            (audit_at,),
        )
        row = cur.fetchone()
        if row:
            acc_id = row[0]
        else:
            m_u = re.match(r"^user_0*(\d+)$", uid)
            if m_u:
                acc_id = f"account_{int(m_u.group(1)):03d}"
            else:
                num_part = uid.split("_")[1] if "_" in uid else "001"
                acc_id = f"account_{num_part}"

    # 3. Check LINK_RESOURCE_IDENTITY activities <= audit_at
    cur.execute(
        "SELECT activity_id, event_time, target_system, resource_id, details_json FROM system_activity WHERE event_type = 'LINK_RESOURCE_IDENTITY' AND event_time <= ?",
        (audit_at,),
    )
    for a in cur.fetchall():
        dt = a["details_json"]
        if any(ident in dt for ident in id_strings):
            rid = a["resource_id"]
            if rid and rid not in case_facts:
                case_facts[rid] = []

    # 4. Check activities for this user <= audit_at
    cur.execute(
        """
        SELECT activity_id, event_time, event_type, source_system, target_system, resource_id, status, details_json
        FROM system_activity
        WHERE event_time <= ? AND (subject_reference = ? OR details_json LIKE ? OR resource_id = ?)
        """,
        (audit_at, uid, f"%{bag.current_email}%", acc_id),
    )
    user_activities = [dict(r) for r in cur.fetchall()]

    return {
        "subject_ref": uid,
        "bag": bag,
        "account_id": acc_id,
        "resources": case_facts,
        "activities": user_activities,
    }


def cite(resource_id: str, audit_at: str, system_id: str, is_future: bool = False, db_path: Optional[Path] = None) -> str:
    """Return the participant-visible evidence_id proving the state at audit_at."""
    if is_future:
        return ""
    con = ensure_facts_store(db_path)
    cur = con.cursor()

    # Rule C04: latest evidence <= audit_at that mentions the resource
    if resource_id:
        cur.execute(
            """
            SELECT file_id FROM facts
            WHERE resource_id = ? AND captured_at <= ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (resource_id, audit_at),
        )
        row = cur.fetchone()
        if row:
            return row[0]

    # For account absent verification without explicit resource mention: covering snapshot
    if system_id == "account_service":
        cur.execute(
            """
            SELECT sc.file_id FROM snapshot_coverage sc
            WHERE sc.system_id = 'account_service' AND sc.captured_at <= ?
              AND EXISTS (SELECT 1 FROM facts f WHERE f.file_id = sc.file_id)
            ORDER BY sc.captured_at DESC LIMIT 1
            """,
            (audit_at,),
        )
        row = cur.fetchone()
        return row[0] if row else ""

    return ""


def classify_case(req: Dict[str, Any], dossier_data: Dict[str, Any], db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Apply deletion rules to case dossier and formulate findings, remediations, and verifications."""
    con = ensure_facts_store(db_path)
    cur = con.cursor()

    cid = req.get("case_id", "")
    audit_at = req.get("audit_at", "")
    req_text = req.get("request_text", "")
    activities = dossier_data.get("activities", [])

    # 1. Check NOT_APPLICABLE
    is_non_del, has_event, has_pattern = is_non_deletion_request(req_text, activities)
    review_flags: List[str] = []
    if is_non_del:
        return {
            "case_id": cid,
            "deletion_status": "NOT_APPLICABLE",
            "findings": [],
            "remediations": [],
            "verifications": [],
            "all_steps": [],
            "review_flags": review_flags,
        }
    elif has_event != has_pattern:
        flag = f"NOT_APPLICABLE mismatch: has_event={has_event}, has_pattern={has_pattern}"
        review_flags.append(flag)

    bag = dossier_data["bag"]
    id_strings = {bag.user_id} | set(bag.emails) | set(bag.phones) | set(bag.email_hashes)
    id_strings.discard("")
    id_strings.discard(None)

    direct_id_strings = set(bag.emails) | set(bag.phones) | set(bag.email_hashes)
    direct_id_strings.discard("")
    direct_id_strings.discard(None)

    acc_id = dossier_data.get("account_id") or ""
    resources = dossier_data.get("resources", {})

    findings: List[Dict[str, Any]] = []
    remediations: List[Dict[str, Any]] = []
    verifications: List[Dict[str, Any]] = []

    # Check primary account state in replica <= audit_at
    acc_is_active = False
    if acc_id:
        cur.execute(
            """
            SELECT file_id, fields_json FROM facts
            WHERE resource_id = ? AND captured_at <= ? AND kind = 'RECORD'
            ORDER BY captured_at DESC LIMIT 1
            """,
            (acc_id, audit_at),
        )
        row = cur.fetchone()
        if row:
            try:
                jd = json.loads(row[1])
                st = jd.get("account_status") or jd.get("status")
                sc = jd.get("status_code")
                prof = jd.get("profile")
                if sc == 404 or prof is None and sc is not None:
                    acc_is_active = False
                elif st == "ACTIVE" or (not st and sc == 200):
                    acc_is_active = True
                elif st in ("DELETED", "INACTIVE"):
                    acc_is_active = False
            except Exception:
                acc_is_active = False

    if acc_is_active and acc_id:
        ev_id = cite(acc_id, audit_at, "account_service", False, db_path)
        findings.append({
            "step_no": 0,
            "phase": "FINDING",
            "system_id": "account_service",
            "resource_id": acc_id,
            "operation": "REPORT_REMAINING_RECORD",
            "state": "PRESENT",
            "evidence_id": ev_id,
        })
        remediations.append({
            "step_no": 0,
            "phase": "REMEDIATION",
            "system_id": "account_service",
            "resource_id": acc_id,
            "operation": "DELETE_RECORD",
            "state": "ABSENT",
            "evidence_id": "",
        })

    # Track resource states
    resource_states: Dict[str, str] = {}
    for rid, f_list in resources.items():
        if not f_list:
            resource_states[rid] = "UNKNOWN"

    # Evaluate each downstream resource
    for rid, f_list in resources.items():
        if not f_list:
            continue
        # Get latest fact <= audit_at
        f = max(f_list, key=lambda x: x["captured_at"])
        sys_id = normalize_system_id(f["system_id"], rid)
        fj = f["fields_json"]
        jd = json.loads(fj) if fj else {}

        if rid.startswith("search_"):
            ev_id = cite(rid, audit_at, sys_id, False, db_path)
            findings.append({
                "step_no": 0,
                "phase": "FINDING",
                "system_id": sys_id,
                "resource_id": rid,
                "operation": "REPORT_REMAINING_RECORD",
                "state": "PRESENT",
                "evidence_id": ev_id,
            })
            remediations.append({
                "step_no": 0,
                "phase": "REMEDIATION",
                "system_id": sys_id,
                "resource_id": rid,
                "operation": "DELETE_RECORD",
                "state": "ABSENT",
                "evidence_id": "",
            })

        elif rid.startswith("export_"):
            ev_id = cite(rid, audit_at, sys_id, False, db_path)
            findings.append({
                "step_no": 0,
                "phase": "FINDING",
                "system_id": sys_id,
                "resource_id": rid,
                "operation": "REPORT_REMAINING_RECORD",
                "state": "PRESENT",
                "evidence_id": ev_id,
            })
            remediations.append({
                "step_no": 0,
                "phase": "REMEDIATION",
                "system_id": sys_id,
                "resource_id": rid,
                "operation": "DELETE_RECORD",
                "state": "ABSENT",
                "evidence_id": "",
            })

        elif rid.startswith("support_"):
            txt = jd.get("message_text") or jd.get("text_extract") or jd.get("message_excerpt") or ""
            is_att = (
                "attachment" in f["kind"].lower()
                or jd.get("record_type") == "SUPPORT_ATTACHMENT"
                or jd.get("collection_name") == "support_attachments"
                or jd.get("marker_found") == "false"
            )
            if is_att:
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                findings.append({
                    "step_no": 0,
                    "phase": "FINDING",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "REPORT_REMAINING_RECORD",
                    "state": "PRESENT",
                    "evidence_id": ev_id,
                })
                remediations.append({
                    "step_no": 0,
                    "phase": "REMEDIATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "DELETE_RECORD",
                    "state": "ABSENT",
                    "evidence_id": "",
                })
            else:
                has_pii = (
                    any(ident in str(jd) for ident in direct_id_strings)
                    or (any(ident in txt for ident in id_strings) and "[REDACTED]" not in txt)
                )
                if has_pii:
                    ev_id = cite(rid, audit_at, sys_id, False, db_path)
                    findings.append({
                        "step_no": 0,
                        "phase": "FINDING",
                        "system_id": sys_id,
                        "resource_id": rid,
                        "operation": "REPORT_EXPOSED_PII",
                        "state": "PRESENT",
                        "evidence_id": ev_id,
                    })
                    remediations.append({
                        "step_no": 0,
                        "phase": "REMEDIATION",
                        "system_id": sys_id,
                        "resource_id": rid,
                        "operation": "REDACT_RECORD",
                        "state": "REDACTED",
                        "evidence_id": "",
                    })
                else:
                    ev_id = cite(rid, audit_at, sys_id, False, db_path)
                    verifications.append({
                        "step_no": 0,
                        "phase": "VERIFICATION",
                        "system_id": sys_id,
                        "resource_id": rid,
                        "operation": "VERIFY_RECORD_REDACTED",
                        "state": "REDACTED",
                        "evidence_id": ev_id,
                    })

        elif rid.startswith("analytics_"):
            is_marketing = ("email_opt_in" in str(jd) or "marketing" in str(jd))
            has_pii = any(ident in str(jd) for ident in id_strings)
            if is_marketing and has_pii:
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                findings.append({
                    "step_no": 0,
                    "phase": "FINDING",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "REPORT_REMAINING_RECORD",
                    "state": "PRESENT",
                    "evidence_id": ev_id,
                })
                remediations.append({
                    "step_no": 0,
                    "phase": "REMEDIATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "DELETE_RECORD",
                    "state": "ABSENT",
                    "evidence_id": "",
                })
            elif has_pii:
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                findings.append({
                    "step_no": 0,
                    "phase": "FINDING",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "REPORT_EXPOSED_PII",
                    "state": "PRESENT",
                    "evidence_id": ev_id,
                })
                remediations.append({
                    "step_no": 0,
                    "phase": "REMEDIATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "ANONYMIZE_RECORD",
                    "state": "ANONYMIZED",
                    "evidence_id": "",
                })
            else:
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                verifications.append({
                    "step_no": 0,
                    "phase": "VERIFICATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "VERIFY_RECORD_ANONYMIZED",
                    "state": "ANONYMIZED",
                    "evidence_id": ev_id,
                })

        elif rid.startswith("data_flow_"):
            enabled = jd.get("enabled")
            if enabled in (True, 1, "1") or str(enabled).lower() in ("true", "1"):
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                findings.append({
                    "step_no": 0,
                    "phase": "FINDING",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "REPORT_ACTIVE_DATA_FLOW",
                    "state": "ACTIVE",
                    "evidence_id": ev_id,
                })
                remediations.append({
                    "step_no": 0,
                    "phase": "REMEDIATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "DISABLE_DATA_FLOW",
                    "state": "DISABLED",
                    "evidence_id": "",
                })
            else:
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                verifications.append({
                    "step_no": 0,
                    "phase": "VERIFICATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "VERIFY_DATA_FLOW_DISABLED",
                    "state": "DISABLED",
                    "evidence_id": ev_id,
                })

        elif rid.startswith("backup_"):
            res_allowed = jd.get("restore_allowed")
            is_active_risk = (
                res_allowed in (True, 1, "1")
                or str(res_allowed).lower() in ("true", "1")
                or jd.get("result") == "ACTIVE"
                or jd.get("eligible") in (True, "true")
                or (jd.get("deletion_block") in (False, "false") and jd.get("eligible") is not None)
            )
            if is_active_risk:
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                findings.append({
                    "step_no": 0,
                    "phase": "FINDING",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "REPORT_RESTORE_RISK",
                    "state": "ACTIVE",
                    "evidence_id": ev_id,
                })
                remediations.append({
                    "step_no": 0,
                    "phase": "REMEDIATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "BLOCK_RESTORE",
                    "state": "BLOCKED",
                    "evidence_id": "",
                })
            else:
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                verifications.append({
                    "step_no": 0,
                    "phase": "VERIFICATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "VERIFY_RESTORE_BLOCKED",
                    "state": "BLOCKED",
                    "evidence_id": ev_id,
                })

        elif rid.startswith("tombstone_"):
            has_pii = any(jd.get(k) is not None for k in ["name", "email", "phone", "email_hash"])
            if has_pii:
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                findings.append({
                    "step_no": 0,
                    "phase": "FINDING",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "REPORT_EXPOSED_PII",
                    "state": "PRESENT",
                    "evidence_id": ev_id,
                })
                remediations.append({
                    "step_no": 0,
                    "phase": "REMEDIATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "REDACT_RECORD",
                    "state": "REDACTED",
                    "evidence_id": "",
                })
            else:
                ev_id = cite(rid, audit_at, sys_id, False, db_path)
                verifications.append({
                    "step_no": 0,
                    "phase": "VERIFICATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "VERIFY_RECORD_REDACTED",
                    "state": "REDACTED",
                    "evidence_id": ev_id,
                })

        elif rid.startswith("audit_record_"):
            ev_id = cite(rid, audit_at, sys_id, False, db_path)
            verifications.append({
                "step_no": 0,
                "phase": "VERIFICATION",
                "system_id": sys_id,
                "resource_id": rid,
                "operation": "VERIFY_RECORD_PRESENT",
                "state": "PRESENT",
                "evidence_id": ev_id,
            })

    is_incomplete = len(findings) > 0
    status = "INCOMPLETE" if is_incomplete else "COMPLETE"

    # Assemble final verification steps
    final_verifs: List[Dict[str, Any]] = []
    seen_verifs: Set[Tuple[str, str, str]] = set()

    def add_verif(step: Dict[str, Any]):
        key = (step["system_id"], step["resource_id"], step["operation"])
        if key not in seen_verifs:
            seen_verifs.add(key)
            final_verifs.append(step)

    if is_incomplete:
        # C09: In incomplete cases, primary account is always verified absent (future: evidence_id="")
        if acc_id:
            add_verif({
                "step_no": 0,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": acc_id,
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT",
                "evidence_id": "",
            })
        # Remediated resources
        for rem in remediations:
            op = rem["operation"]
            rid = rem["resource_id"]
            sys_id = rem["system_id"]
            if op == "DELETE_RECORD":
                add_verif({
                    "step_no": 0,
                    "phase": "VERIFICATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "VERIFY_RECORD_ABSENT",
                    "state": "ABSENT",
                    "evidence_id": "",
                })
            elif op == "REDACT_RECORD":
                add_verif({
                    "step_no": 0,
                    "phase": "VERIFICATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "VERIFY_RECORD_REDACTED",
                    "state": "REDACTED",
                    "evidence_id": "",
                })
            elif op == "ANONYMIZE_RECORD":
                add_verif({
                    "step_no": 0,
                    "phase": "VERIFICATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "VERIFY_RECORD_ANONYMIZED",
                    "state": "ANONYMIZED",
                    "evidence_id": "",
                })
            elif op == "DISABLE_DATA_FLOW":
                add_verif({
                    "step_no": 0,
                    "phase": "VERIFICATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "VERIFY_DATA_FLOW_DISABLED",
                    "state": "DISABLED",
                    "evidence_id": "",
                })
            elif op == "BLOCK_RESTORE":
                add_verif({
                    "step_no": 0,
                    "phase": "VERIFICATION",
                    "system_id": sys_id,
                    "resource_id": rid,
                    "operation": "VERIFY_RESTORE_BLOCKED",
                    "state": "BLOCKED",
                    "evidence_id": "",
                })
        # Retained security audit records
        for v in verifications:
            if v["operation"] == "VERIFY_RECORD_PRESENT":
                v_copy = dict(v)
                v_copy["evidence_id"] = ""
                add_verif(v_copy)
    else:
        # COMPLETE case: evidence-backed verifications only
        if acc_id:
            ev_id = cite(acc_id, audit_at, "account_service", False, db_path)
            add_verif({
                "step_no": 0,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": acc_id,
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT",
                "evidence_id": ev_id,
            })
        for v in verifications:
            add_verif(v)

    # Sort steps strictly across 4 tiers per C03
    def step_sort_key(s: Dict[str, Any]) -> Tuple[int, int, str, str]:
        p_rank = PHASE_ORDER[s["phase"]]
        op_rank = OPERATION_ORDER[s["phase"]][s["operation"]]
        return (p_rank, op_rank, s["system_id"], s["resource_id"])

    findings.sort(key=step_sort_key)
    remediations.sort(key=step_sort_key)
    final_verifs.sort(key=step_sort_key)

    all_steps = findings + remediations + final_verifs
    for idx, st in enumerate(all_steps, 1):
        st["step_no"] = idx

    return {
        "case_id": cid,
        "deletion_status": status,
        "findings": findings,
        "remediations": remediations,
        "verifications": final_verifs,
        "all_steps": all_steps,
        "resource_states": resource_states,
        "review_flags": review_flags,
    }
