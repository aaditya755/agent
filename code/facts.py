"""Facts store backed by SQLite cache.

Extracts point-in-time facts from evidence (.sql, .log, .json, .txt, .pdf, .png)
and dataset CSVs, strictly enforcing the audit_at cutoff on all queries.
"""

import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from config import (
    DATASET_DIR,
    CACHE_DIR,
    FACTS_DB_PATH,
    USER_IDENTITIES_PATH,
    USER_AUDIT_LOG_PATH,
    SYSTEM_ACTIVITY_PATH,
    DELETION_RULES_PATH,
    EVIDENCE_FILES_PATH,
)
from schemas import Fact

SYSTEM_ORIGIN_MAP = {
    "account_admin_portal": "account_service",
    "customer_account_service": "account_service",
    "customer_account_failover_replica_capture": "account_service",
    "account_history_service": "account_service",
    "search_index_admin": "search_service",
    "search_index_service": "search_service",
    "customer_search_primary_capture": "search_service",
    "integration_admin": "integration_service",
    "integration_management_service": "integration_service",
    "downstream_delivery_service": "integration_service",
    "customer_export_service": "export_service",
    "export_service": "export_service",
    "legal_retention_service": "legal_service",
    "records_assurance_snapshot_bundle": "records_assurance",
    "customer_data_inventory_capture": "data_inventory",
    "customer_data_read_replica_capture": "account_service",
    "customer_data_failover_replica_capture": "account_service",
    "customer_data_regional_replicas_capture": "account_service",
    "customer_data_reporting_replica_capture": "account_service",
    "central_observability_export": "observability",
    "service_readback_logs": "service_readback",
    "service_resource_export": "service_export",
    "backup_service": "backup_service",
    "object_storage": "object_storage",
    "identity_service": "identity_service",
    "security_service": "security_service",
    "analytics_platform": "analytics_platform",
    "support_platform": "support_platform",
    "marketing_platform": "marketing_platform",
}

PREFIX_SYSTEM_MAP = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "export": "export_service",
    "tombstone": "identity_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
}


def normalize_system_id(sys_id: str, resource_id: str = "") -> str:
    """Normalize system_id using canonical resource prefix mapping."""
    if resource_id:
        clean_res = resource_id.strip('"\'')
        m = re.match(r"^([a-zA-Z_]+)_\d+", clean_res)
        pfx = m.group(1) if m else clean_res.split("_")[0]
        if pfx in PREFIX_SYSTEM_MAP:
            return PREFIX_SYSTEM_MAP[pfx]
    return SYSTEM_ORIGIN_MAP.get(sys_id, sys_id)

TABLE_SYSTEM_MAP = {
    "customer_profiles": "account_service",
    "customer_profiles_primary": "account_service",
    "customer_profiles_failover_replica": "account_service",
    "customer_data_primary": "account_service",
    "customer_data_read_replica": "account_service",
    "customer_data_failover_replica": "account_service",
    "customer_data_replica_ap_south": "account_service",
    "customer_data_replica_eu_west": "account_service",
    "customer_data_reporting_replica": "account_service",
    "customer_search_documents": "search_service",
    "search_documents_primary": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def detect_topology(origin: str, context_name: str = "") -> str:
    """Infer infrastructure topology role from origin and file/table name."""
    name = f"{origin}_{context_name}".lower()
    if "failover" in name:
        return "failover_replica"
    if "read_replica" in name:
        return "read_replica"
    if "regional" in name or "replica_ap" in name or "replica_eu" in name:
        return "regional_replica"
    if "reporting" in name:
        return "reporting_replica"
    if "primary" in name:
        return "primary"
    if "inventory" in name:
        return "inventory"
    if "bundle" in name:
        return "snapshot_bundle"
    if "log" in name:
        return "log"
    if "console" in name or "portal" in name or "view" in name or "inspector" in name:
        return "console"
    if "document" in name or "register" in name or "report" in name:
        return "document"
    return "snapshot"


def compute_dataset_hash(dataset_dir: Path) -> str:
    """Deterministic hash of participant-facing dataset files to detect changes."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(dataset_dir):
        dirs.sort()
        for f in sorted(files):
            fp = Path(root) / f
            rel = fp.relative_to(dataset_dir).as_posix()
            stat = fp.stat()
            h.update(f"{rel}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
    return h.hexdigest()


def _init_db_schema(con: sqlite3.Connection):
    """Create fact store and dataset query tables with necessary indices."""
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT,
        captured_at TEXT,
        system_id TEXT,
        topology TEXT,
        resource_id TEXT,
        kind TEXT,
        fields_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_facts_res_time ON facts(resource_id, captured_at);
    CREATE INDEX IF NOT EXISTS idx_facts_file ON facts(file_id);
    CREATE INDEX IF NOT EXISTS idx_facts_system_time ON facts(system_id, captured_at);

    CREATE TABLE IF NOT EXISTS snapshot_coverage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT,
        captured_at TEXT,
        system_id TEXT,
        topology TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_cov_sys_time ON snapshot_coverage(system_id, captured_at);
    CREATE INDEX IF NOT EXISTS idx_cov_file ON snapshot_coverage(file_id);

    CREATE TABLE IF NOT EXISTS user_identities (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        email TEXT,
        phone TEXT,
        email_hash TEXT,
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_identities_email ON user_identities(email);

    CREATE TABLE IF NOT EXISTS user_audit_log (
        audit_id TEXT PRIMARY KEY,
        user_id TEXT,
        event_time TEXT,
        field_name TEXT,
        old_value TEXT,
        new_value TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_audit_user_time ON user_audit_log(user_id, event_time);
    CREATE INDEX IF NOT EXISTS idx_audit_field_old ON user_audit_log(field_name, old_value);
    CREATE INDEX IF NOT EXISTS idx_audit_field_new ON user_audit_log(field_name, new_value);

    CREATE TABLE IF NOT EXISTS system_activity (
        activity_id TEXT PRIMARY KEY,
        event_time TEXT,
        event_type TEXT,
        source_system TEXT,
        target_system TEXT,
        resource_id TEXT,
        subject_reference TEXT,
        status TEXT,
        details_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_act_res_time ON system_activity(resource_id, event_time);
    CREATE INDEX IF NOT EXISTS idx_act_subj_time ON system_activity(subject_reference, event_time);

    CREATE TABLE IF NOT EXISTS deletion_rules (
        rule_id TEXT PRIMARY KEY,
        data_category TEXT,
        condition TEXT,
        required_action TEXT
    );

    CREATE TABLE IF NOT EXISTS evidence_files (
        file_id TEXT PRIMARY KEY,
        file_path TEXT,
        origin TEXT,
        captured_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ev_files_file ON evidence_files(file_id);
    """)
    con.commit()


def _populate_csv_tables(con: sqlite3.Connection):
    """Ingest participant CSVs into queryable database tables."""
    cur = con.cursor()

    with open(USER_IDENTITIES_PATH, encoding="utf-8") as f:
        cur.executemany(
            "INSERT INTO user_identities VALUES (:user_id, :name, :email, :phone, :email_hash, :updated_at)",
            csv.DictReader(f),
        )

    with open(USER_AUDIT_LOG_PATH, encoding="utf-8") as f:
        cur.executemany(
            "INSERT INTO user_audit_log VALUES (:audit_id, :user_id, :event_time, :field_name, :old_value, :new_value)",
            csv.DictReader(f),
        )

    with open(DELETION_RULES_PATH, encoding="utf-8") as f:
        cur.executemany(
            "INSERT INTO deletion_rules VALUES (:rule_id, :data_category, :condition, :required_action)",
            csv.DictReader(f),
        )

    with open(EVIDENCE_FILES_PATH, encoding="utf-8") as f:
        cur.executemany(
            "INSERT INTO evidence_files VALUES (:file_id, :file_path, :origin, :captured_at)",
            csv.DictReader(f),
        )

    with open(SYSTEM_ACTIVITY_PATH, encoding="utf-8") as f:
        acts = []
        for row in csv.DictReader(f):
            acts.append((
                row["activity_id"],
                row["event_time"],
                row["event_type"],
                row["source_system"],
                row["target_system"],
                row.get("resource_id") or "",
                row.get("subject_reference") or "",
                row.get("status") or "",
                row.get("details") or "{}",
            ))
        cur.executemany("INSERT INTO system_activity VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", acts)

    con.commit()


def _populate_evidence_facts(con: sqlite3.Connection):
    """Extract point-in-time facts from all evidence files."""
    import pdfplumber

    cur = con.cursor()
    cur.execute("SELECT file_id, file_path, origin, captured_at FROM evidence_files")
    ev_files = cur.fetchall()

    facts = []
    coverage = []

    def add_fact(f_id, c_at, s_id, t_topo, r_id, kind_name, f_json):
        clean_res = r_id.strip('"\'') if r_id else ""
        norm_sys = normalize_system_id(s_id, clean_res)
        facts.append((f_id, c_at, norm_sys, t_topo, clean_res, kind_name, f_json))

    kv_re = re.compile(r'(\w+)=(?:\"([^\"]*)\"|(\S+))')

    for fid, rel_path, origin, captured_at in ev_files:
        fpath = DATASET_DIR / rel_path
        ext = fpath.suffix.lower()

        if ext == ".png":
            sys_id = normalize_system_id(origin)
            topo = detect_topology(origin, fpath.name)
            add_fact(fid, captured_at, sys_id, topo, "", "PNG_PENDING", "{}")
            coverage.append((fid, captured_at, sys_id, topo))

        elif ext == ".sql":
            mem_con = sqlite3.connect(":memory:")
            with open(fpath, "r", encoding="utf-8") as f:
                mem_con.executescript(f.read())
            tables = [r[0] for r in mem_con.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall()]
            for t in tables:
                if t == "snapshot_scope":
                    for s_row in mem_con.execute("SELECT origin_system, captured_at FROM snapshot_scope").fetchall():
                        s_sys, s_time = s_row
                        coverage.append((fid, s_time or captured_at, normalize_system_id(s_sys), "inventory"))
                    continue
                t_sys = TABLE_SYSTEM_MAP.get(t, normalize_system_id(origin))
                t_topo = detect_topology(origin, t)
                coverage.append((fid, captured_at, t_sys, t_topo))
                cur_t = mem_con.execute(f"SELECT * FROM {t}")
                cols = [d[0] for d in cur_t.description]
                for row in cur_t.fetchall():
                    row_dict = dict(zip(cols, row))
                    res_id = row_dict.get("resource_id") or ""
                    if res_id:
                        add_fact(fid, captured_at, t_sys, t_topo, res_id, "RECORD", json.dumps(row_dict, default=str))

        elif ext == ".json":
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            topo = detect_topology(origin, fpath.name)
            if "snapshots" in data and isinstance(data["snapshots"], list):
                for sn in data["snapshots"]:
                    col = sn.get("collection") or sn.get("resource_type") or ""
                    sn_sys = TABLE_SYSTEM_MAP.get(col, normalize_system_id(origin))
                    coverage.append((fid, captured_at, sn_sys, topo))
                    for rec in sn.get("records", []):
                        res_id = rec.get("resource_id") or ""
                        if res_id:
                            add_fact(fid, captured_at, sn_sys, topo, res_id, "RECORD", json.dumps(rec, default=str))
            elif "service_exports" in data and isinstance(data["service_exports"], dict):
                for s_name, s_val in data["service_exports"].items():
                    norm_s_name = normalize_system_id(s_name)
                    coverage.append((fid, captured_at, norm_s_name, topo))
                    for col_name, rec_list in s_val.items():
                        if isinstance(rec_list, list):
                            for rec in rec_list:
                                res_id = rec.get("resource_id") or ""
                                if res_id:
                                    add_fact(fid, captured_at, norm_s_name, topo, res_id, "RECORD", json.dumps(rec, default=str))
            elif "records" in data and isinstance(data["records"], list):
                sys_id = normalize_system_id(origin)
                coverage.append((fid, captured_at, sys_id, topo))
                for rec in data["records"]:
                    res_id = rec.get("resource_id") or ""
                    if res_id:
                        rec_sys = rec.get("system") or sys_id
                        add_fact(fid, captured_at, rec_sys, topo, res_id, "RECORD", json.dumps(rec, default=str))

        elif ext == ".txt":
            sys_id = normalize_system_id(origin)
            topo = detect_topology(origin, fpath.name)
            coverage.append((fid, captured_at, sys_id, topo))
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("- "):
                        parts = [p.strip() for p in line[2:].split("|")]
                        if len(parts) >= 5:
                            ts, s_id, r_id, rec_type, result = parts[:5]
                            subj = parts[5] if len(parts) > 5 else ""
                            rec_dict = {
                                "timestamp": ts,
                                "system_id": normalize_system_id(s_id, r_id),
                                "resource_id": r_id.strip('"\''),
                                "record_type": rec_type,
                                "result": result,
                                "subject_reference": subj,
                            }
                            add_fact(fid, ts or captured_at, s_id, topo, r_id, "HANDOVER_RECORD", json.dumps(rec_dict))

        elif ext == ".log":
            sys_id = normalize_system_id(origin)
            topo = "log"
            coverage.append((fid, captured_at, sys_id, topo))
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # 1. Check structured JSON log lines
                    m_json = re.match(r"^(\S+)\s+(\{.*\})$", line)
                    if m_json:
                        ts, jstr = m_json.groups()
                        try:
                            jd = json.loads(jstr)
                            res_id = jd.get("resource_id") or ""
                            log_sys = jd.get("service") or jd.get("logger") or sys_id
                            if res_id:
                                add_fact(fid, ts, log_sys, topo, res_id, "LOG_EVENT", json.dumps(jd))
                            continue
                        except Exception:
                            pass

                    # 2. Check bracketed log lines
                    m_br = re.match(r"^(\S+)\s+\[(\w+)\]\s+\[([\w\-]+)\]\s+\[([\w\-]+)\]\s+([\w\-]+):\s*(.*)$", line)
                    if m_br:
                        ts, level, log_sys, trace_id, event_name, rest = m_br.groups()
                        kvs = {}
                        for k, v1, v2 in kv_re.findall(rest):
                            kvs[k] = v1 if v1 != "" else v2
                        res_id = kvs.get("resource_id") or ""
                        if res_id:
                            kvs["log_level"] = level
                            kvs["log_system"] = log_sys
                            kvs["trace_id"] = trace_id
                            kvs["event_name"] = event_name
                            add_fact(fid, ts, log_sys, topo, res_id, "LOG_EVENT", json.dumps(kvs))
                        continue

                    # 3. Check unbracketed key=value log lines
                    m_kv = re.match(r"^(\S+)\s+(.*)$", line)
                    if m_kv:
                        ts, rest = m_kv.groups()
                        kvs = {}
                        for k, v1, v2 in kv_re.findall(rest):
                            kvs[k] = v1 if v1 != "" else v2
                        res_id = kvs.get("resource_id") or ""
                        log_sys = kvs.get("service") or sys_id
                        if res_id:
                            add_fact(fid, ts, log_sys, topo, res_id, "LOG_EVENT", json.dumps(kvs))

        elif ext == ".pdf":
            sys_id = normalize_system_id(origin)
            topo = "document"
            coverage.append((fid, captured_at, sys_id, topo))
            with pdfplumber.open(fpath) as doc:
                full_text = "\n".join([page.extract_text() or "" for page in doc.pages])

            # Extract structured order entries in legal retention register (evidence_27)
            order_rows = re.findall(
                r"(ORD-\d+)\s*\|\s*(user_\d+)\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*Retain until\s*([^|]+)\|\s*(\w+)",
                full_text,
            )
            for ord_id, u_id, reason, dt, amt, retain_dt, initial in order_rows:
                ord_dict = {
                    "order_id": ord_id,
                    "user_id": u_id,
                    "reason": reason.strip(),
                    "retain_until": retain_dt.strip(),
                    "amount": amt.strip(),
                }
                add_fact(fid, captured_at, "legal_service", topo, ord_id, "LEGAL_HOLD_RECORD", json.dumps(ord_dict))

            # Extract account statement & export references (evidence_18)
            nst_matches = set(re.findall(r"Statement\s+(NST-\d+-\d+-\d+)", full_text, re.IGNORECASE))
            for nst in nst_matches:
                m_num = re.search(r"NST-\d+-(\d+)-(\d+)", nst)
                export_id = f"export_{m_num.group(2)}" if m_num else nst
                stmt_dict = {"statement_id": nst, "export_id": export_id, "user_id": "user_18", "text": full_text[:400]}
                add_fact(fid, captured_at, "export_service", topo, export_id, "DOCUMENT_RECORD", json.dumps(stmt_dict))
                add_fact(fid, captured_at, "account_service", topo, nst, "DOCUMENT_RECORD", json.dumps(stmt_dict))

            ord_matches = set(re.findall(r"(ORD-\d+)", full_text))
            for om in ord_matches:
                if not any(om == r[0] for r in order_rows):
                    add_fact(fid, captured_at, sys_id, topo, om, "DOCUMENT_RECORD", json.dumps({"order_id": om, "text": full_text[:400]}))

            # Extract security audit records in extracts (evidence_55)
            audit_matches = set(re.findall(r"(audit_record_\d+)", full_text))
            for arm in audit_matches:
                add_fact(fid, captured_at, "security_service", topo, arm, "PDF_RECORD", json.dumps({"text_extract": full_text[:500]}))

            # Extract support message references in attachments (evidence_58)
            supp_matches = set(re.findall(r"(support_\d+)", full_text))
            for sm in supp_matches:
                add_fact(fid, captured_at, "support_platform", topo, sm, "PDF_RECORD", json.dumps({"text_extract": full_text[:500]}))

    cur.executemany(
        "INSERT INTO facts (file_id, captured_at, system_id, topology, resource_id, kind, fields_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        facts,
    )
    cur.executemany(
        "INSERT INTO snapshot_coverage (file_id, captured_at, system_id, topology) VALUES (?, ?, ?, ?)",
        coverage,
    )
    con.commit()


def ensure_facts_store(db_path: Optional[Path] = None, force: bool = False) -> sqlite3.Connection:
    """Ensure facts database exists and matches dataset hash; build or rebuild if needed."""
    if db_path is None:
        db_path = FACTS_DB_PATH

    current_hash = compute_dataset_hash(DATASET_DIR)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if db_path.exists() and not force:
        try:
            con = sqlite3.connect(db_path)
            cur = con.cursor()
            cur.execute("SELECT value FROM meta WHERE key = 'dataset_hash'")
            row = cur.fetchone()
            if row and row[0] == current_hash:
                con.row_factory = sqlite3.Row
                return con
            con.close()
        except sqlite3.Error:
            pass

    # Rebuild database
    if db_path.exists():
        db_path.unlink()

    con = sqlite3.connect(db_path)
    _init_db_schema(con)
    _populate_csv_tables(con)
    _populate_evidence_facts(con)

    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dataset_hash', ?)", (current_hash,))
    con.commit()
    con.row_factory = sqlite3.Row
    return con


def mentions(resource_id: str, audit_at: str, db_path: Optional[Path] = None) -> List[Fact]:
    """Retrieve all facts mentioning resource_id captured at or before audit_at."""
    con = ensure_facts_store(db_path)
    cur = con.cursor()
    cur.execute(
        """
        SELECT file_id, captured_at, system_id, topology, resource_id, kind, fields_json
        FROM facts
        WHERE resource_id = ? AND captured_at <= ?
        ORDER BY captured_at DESC, file_id DESC
        """,
        (resource_id, audit_at),
    )
    results = []
    for r in cur.fetchall():
        results.append(Fact(
            file_id=r["file_id"],
            captured_at=r["captured_at"],
            system_id=r["system_id"],
            topology=r["topology"],
            resource_id=r["resource_id"],
            kind=r["kind"],
            fields=json.loads(r["fields_json"]),
        ))
    return results


def snapshot_state(resource_id: str, audit_at: str, db_path: Optional[Path] = None) -> Optional[Fact]:
    """Retrieve latest fact for resource_id captured at or before audit_at."""
    con = ensure_facts_store(db_path)
    cur = con.cursor()
    cur.execute(
        """
        SELECT file_id, captured_at, system_id, topology, resource_id, kind, fields_json
        FROM facts
        WHERE resource_id = ? AND captured_at <= ?
        ORDER BY captured_at DESC, file_id DESC
        LIMIT 1
        """,
        (resource_id, audit_at),
    )
    r = cur.fetchone()
    if not r:
        return None
    return Fact(
        file_id=r["file_id"],
        captured_at=r["captured_at"],
        system_id=r["system_id"],
        topology=r["topology"],
        resource_id=r["resource_id"],
        kind=r["kind"],
        fields=json.loads(r["fields_json"]),
    )


def covering_snapshots(system_id: str, audit_at: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Retrieve all snapshot coverage records for system_id captured at or before audit_at."""
    con = ensure_facts_store(db_path)
    cur = con.cursor()
    cur.execute(
        """
        SELECT DISTINCT file_id, captured_at, system_id, topology
        FROM snapshot_coverage
        WHERE system_id = ? AND captured_at <= ?
        ORDER BY captured_at DESC, file_id DESC
        """,
        (system_id, audit_at),
    )
    return [dict(r) for r in cur.fetchall()]


def get_evidence_file(file_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Retrieve evidence file metadata by file_id."""
    con = ensure_facts_store(db_path)
    cur = con.cursor()
    cur.execute("SELECT file_id, file_path, origin, captured_at FROM evidence_files WHERE file_id = ?", (file_id,))
    r = cur.fetchone()
    return dict(r) if r else None
