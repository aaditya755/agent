"""Tool suite bound to a specific case context for the deletion-audit agent.

Tools:
- get_case_dossier(): Retrieve dossier of discovered resources, activities, and identifiers.
- inspect_resource(resource_id): Retrieve records and facts for a given resource <= audit_at.
- read_evidence_file(file_id, max_chars): Safely inspect evidence content <= audit_at capped at 4000 chars.
- submit_verdicts(verdicts): Submit final audit judgments (intent, support_judgments, notes).
"""

import csv
import json
import logging
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

import config
import baseline
import facts

logger = logging.getLogger(__name__)


class VerdictValidationError(Exception):
    """Raised when submitted verdicts fail schema validation."""
    pass


class CaseToolEnvironment:
    """Encapsulates tools bound strictly to a specific request case and its audit_at cutoff."""

    def __init__(self, request_row: Dict[str, Any], dossier_data: Optional[Dict[str, Any]] = None):
        self.request_row = request_row
        self.case_id = request_row.get("case_id", "")
        self.email = request_row.get("email", "")
        self.audit_at = request_row.get("audit_at", "")
        self.request_text = request_row.get("request_text", "")
        self.dossier_data = dossier_data or baseline.dossier(self.email, self.audit_at)

        self.verdicts: Optional[Dict[str, Any]] = None
        self.tool_call_count = 0

        # Load evidence manifest for audit_at validation
        self._evidence_manifest: Dict[str, Dict[str, str]] = {}
        if config.EVIDENCE_FILES_PATH.exists():
            with open(config.EVIDENCE_FILES_PATH, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    self._evidence_manifest[row["file_id"]] = row

    def get_case_dossier(self) -> Dict[str, Any]:
        """Return the case dossier containing subject identifiers, activities, and discovered resources."""
        self.tool_call_count += 1
        resources_summary = {}
        for rid, flist in self.dossier_data.get("resources", {}).items():
            resources_summary[rid] = [
                {
                    "file_id": f.get("file_id"),
                    "system_id": f.get("system_id"),
                    "topology": f.get("topology"),
                    "captured_at": f.get("captured_at"),
                }
                for f in flist
            ]

        return {
            "case_id": self.case_id,
            "subject_identity": {
                "user_id": self.dossier_data.get("identity", {}).get("user_id"),
                "email": self.dossier_data.get("identity", {}).get("email"),
                "all_identifiers": list(self.dossier_data.get("all_identifiers", [])),
            },
            "discovered_resources": resources_summary,
            "activities_count": len(self.dossier_data.get("activities", [])),
            "claims": self.dossier_data.get("claims", {}),
        }

    def inspect_resource(self, resource_id: str) -> Dict[str, Any]:
        """Return point-in-time facts and field values for a specific resource captured <= audit_at."""
        self.tool_call_count += 1
        rid = resource_id.strip()
        flist = self.dossier_data.get("resources", {}).get(rid, [])
        if not flist:
            return {"resource_id": rid, "status": "NOT_FOUND_IN_DOSSIER", "facts": []}

        facts_out = []
        for f in flist:
            fj = f.get("fields_json", "{}")
            try:
                fields_dict = json.loads(fj) if isinstance(fj, str) else fj
            except Exception:
                fields_dict = {"raw": fj}

            facts_out.append({
                "file_id": f.get("file_id"),
                "captured_at": f.get("captured_at"),
                "system_id": f.get("system_id"),
                "topology": f.get("topology"),
                "fields": fields_dict,
            })

        return {
            "resource_id": rid,
            "status": "PRESENT",
            "evidence_count": len(facts_out),
            "facts": facts_out,
        }

    def read_evidence_file(self, file_id: str, max_chars: int = 4000) -> Dict[str, Any]:
        """Read evidence file contents <= audit_at, capped strictly at max_chars <= 4000."""
        self.tool_call_count += 1
        fid = file_id.strip()
        manifest_entry = self._evidence_manifest.get(fid)
        if not manifest_entry:
            return {"file_id": fid, "error": f"Evidence file {fid} not found in manifest."}

        captured_at = manifest_entry.get("captured_at", "")
        if captured_at > self.audit_at:
            return {
                "file_id": fid,
                "error": f"REFUSAL: Evidence {fid} was captured at {captured_at} after audit_at {self.audit_at} and cannot be used.",
            }

        rel_path = manifest_entry.get("file_path", "")
        full_path = config.DATASET_DIR / rel_path
        if not full_path.exists():
            return {"file_id": fid, "error": f"Evidence file on disk not found: {rel_path}"}

        cap = max(100, min(int(max_chars), 4000))
        ext = full_path.suffix.lower()

        # Handle text files (.log, .json, .txt, .sql)
        if ext in [".log", ".json", ".txt", ".sql"]:
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")[:cap]
                return {
                    "file_id": fid,
                    "captured_at": captured_at,
                    "content_snippet": content,
                    "chars_returned": len(content),
                }
            except Exception as e:
                return {"file_id": fid, "error": f"Failed reading file: {e}"}

        # For images and PDFs, query SQLite facts or cached summary
        con = sqlite3.connect(config.FACTS_DB_PATH)
        cur = con.cursor()
        cur.execute("SELECT resource_id, kind, fields_json FROM facts WHERE file_id = ?", (fid,))
        rows = cur.fetchall()
        con.close()

        records_summary = [f"{r[0]} ({r[1]}): {r[2]}" for r in rows][:10]
        snippet = "\n".join(records_summary)[:cap]

        return {
            "file_id": fid,
            "captured_at": captured_at,
            "type": ext,
            "extracted_facts_snippet": snippet or "Binary or visual evidence file. See extracted facts.",
        }

    def submit_verdicts(self, verdicts: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and record final judgments. Rejects schema-invalid submissions with descriptive error."""
        self.tool_call_count += 1

        if not isinstance(verdicts, dict):
            raise VerdictValidationError("Verdicts must be a JSON object.")

        intent = verdicts.get("intent")
        if intent not in ["DELETION", "NON_DELETION"]:
            raise VerdictValidationError(
                f"Invalid 'intent': must be either 'DELETION' or 'NON_DELETION', got {repr(intent)}"
            )

        support_judgments = verdicts.get("support_judgments", [])
        if not isinstance(support_judgments, list):
            raise VerdictValidationError("'support_judgments' must be a list of objects.")

        validated_sj = []
        for idx, sj in enumerate(support_judgments):
            if not isinstance(sj, dict):
                raise VerdictValidationError(f"support_judgments[{idx}] must be a dict.")
            rid = sj.get("resource_id")
            if not rid or not isinstance(rid, str):
                raise VerdictValidationError(f"support_judgments[{idx}] missing valid 'resource_id'.")
            exposes = sj.get("exposes_direct_identifier")
            if not isinstance(exposes, bool):
                raise VerdictValidationError(
                    f"support_judgments[{idx}] 'exposes_direct_identifier' must be boolean, got {type(exposes)}"
                )
            quote = str(sj.get("evidence_quote", ""))[:200]
            validated_sj.append({
                "resource_id": rid,
                "exposes_direct_identifier": exposes,
                "evidence_quote": quote,
            })

        notes = str(verdicts.get("notes", ""))[:300]

        self.verdicts = {
            "intent": intent,
            "support_judgments": validated_sj,
            "notes": notes,
        }
        return {"status": "SUCCESS", "message": "Verdicts submitted and accepted."}

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Return tool definitions conforming to the OpenAI function calling schema."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_case_dossier",
                    "description": "Retrieve dossier of subject identity, discovered resources, and claims.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "inspect_resource",
                    "description": "Inspect point-in-time facts and field records for a specific resource ID <= audit_at.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "resource_id": {"type": "string", "description": "Resource identifier e.g. support_001, account_005"}
                        },
                        "required": ["resource_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_evidence_file",
                    "description": "Read evidence file content <= audit_at up to max_chars <= 4000.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_id": {"type": "string", "description": "Evidence file ID e.g. evidence_01"},
                            "max_chars": {"type": "integer", "description": "Max characters to read (capped at 4000)"},
                        },
                        "required": ["file_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_verdicts",
                    "description": "Submit final audit judgments (intent, support_judgments, notes).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "verdicts": {
                                "type": "object",
                                "properties": {
                                    "intent": {
                                        "type": "string",
                                        "enum": ["DELETION", "NON_DELETION"],
                                        "description": "Classification of request intent",
                                    },
                                    "support_judgments": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "resource_id": {"type": "string"},
                                                "exposes_direct_identifier": {"type": "boolean"},
                                                "evidence_quote": {"type": "string"},
                                            },
                                            "required": ["resource_id", "exposes_direct_identifier"],
                                        },
                                        "description": "Judgments on whether support tickets expose direct PII",
                                    },
                                    "notes": {"type": "string", "description": "Investigator notes <= 300 chars"},
                                },
                                "required": ["intent"],
                            }
                        },
                        "required": ["verdicts"],
                    },
                },
            },
        ]
