"""Tests for facts store and identity resolution (Milestone 2).
"""

import sys
from pathlib import Path
import pytest

# Ensure flat imports for code/
repo_root = Path(__file__).resolve().parents[1]
code_dir = repo_root / "code"
if str(code_dir) not in sys.path:
    sys.path.insert(0, str(code_dir))

from config import FACTS_DB_PATH
from facts import ensure_facts_store, mentions, snapshot_state, covering_snapshots
from identity import resolve


@pytest.fixture(scope="session")
def facts_db():
    con = ensure_facts_store()
    return con


def test_identity_resolution_current_email(facts_db):
    """Resolve a subject using their current email address."""
    # user_05 current email is leila.haddad@yahoo.com
    bag = resolve("leila.haddad@yahoo.com", "2026-09-20T00:00:00Z")
    assert bag.resolve_method == "current"
    assert bag.user_id == "user_05"
    assert "leila.haddad@yahoo.com" in bag.emails
    assert len(bag.names) > 0
    assert len(bag.email_hashes) > 0


def test_identity_resolution_historical_email(facts_db):
    """Resolve a subject using a historical email recorded in user_audit_log."""
    # user_38 had old email choi.jihoon94@gmail.com changed at 2026-06-03T02:15:52Z
    bag_after = resolve("choi.jihoon94@gmail.com", "2026-09-20T00:00:00Z")
    assert bag_after.resolve_method == "historical"
    assert bag_after.user_id == "user_38"
    assert "choi.jihoon94@gmail.com" in bag_after.emails
    assert bag_after.current_email != ""

    # Before the audit log entry, the historical change should not be known
    bag_before = resolve("choi.jihoon94@gmail.com", "2026-01-01T00:00:00Z")
    assert bag_before.resolve_method == "unresolved"
    assert bag_before.user_id == ""


def test_identity_resolution_uppercase_email(facts_db):
    """Resolve a subject whose request provided uppercase email characters."""
    # LEILA.HADDAD@YAHOO.COM should resolve to user_05 via case-insensitive current match
    bag = resolve("LEILA.HADDAD@YAHOO.COM", "2026-09-20T00:00:00Z")
    assert bag.resolve_method == "current"
    assert bag.user_id == "user_05"
    assert "leila.haddad@yahoo.com" in bag.emails


def test_identity_resolution_unknown_email(facts_db):
    """Unknown email returns unresolved bag with empty user_id."""
    bag = resolve("nonexistent_user_xyz_999@example.com", "2026-09-20T00:00:00Z")
    assert bag.resolve_method == "unresolved"
    assert bag.user_id == ""


def test_snapshot_state_across_time(facts_db):
    """snapshot_state returns fact if captured_at <= audit_at, None before."""
    # account_005 is captured in evidence_54 at 2026-09-02T23:00:00Z
    fact_before = snapshot_state("account_005", "2026-09-01T00:00:00Z")
    assert fact_before is None

    fact_after = snapshot_state("account_005", "2026-09-03T00:00:00Z")
    assert fact_after is not None
    assert fact_after.resource_id == "account_005"
    assert fact_after.file_id == "evidence_54"
    assert fact_after.system_id == "account_service"


def test_mentions_count_and_temporal_cutoff(facts_db):
    """mentions returns all matching facts up to audit_at and ignores later ones."""
    # account_005 is in evidence_54 (2026-09-02T23:00:00Z)
    m_before = mentions("account_005", "2026-09-01T00:00:00Z")
    assert len(m_before) == 0

    m_after = mentions("account_005", "2026-09-03T00:00:00Z")
    assert len(m_after) >= 1
    assert m_after[0].resource_id == "account_005"


def test_png_registration_pending(facts_db):
    """All 11 PNG evidence files are registered as kind='PNG_PENDING'."""
    cur = facts_db.cursor()
    cur.execute("SELECT COUNT(*) FROM facts WHERE kind = 'PNG_PENDING'")
    count = cur.fetchone()[0]
    assert count == 11

    cur.execute("SELECT file_id, system_id, fields_json FROM facts WHERE kind = 'PNG_PENDING'")
    rows = cur.fetchall()
    assert len(rows) == 11
    for r in rows:
        assert r["fields_json"] == "{}"
        assert r["system_id"] != ""


def test_strict_audit_at_boundary(facts_db):
    """Cutoff is inclusive of exact captured_at timestamp, but excludes 1s prior."""
    # evidence_54 captured_at is '2026-09-02T23:00:00Z'
    exact_time = "2026-09-02T23:00:00Z"
    before_1s = "2026-09-02T22:59:59Z"

    at_exact = mentions("account_005", exact_time)
    assert any(f.file_id == "evidence_54" for f in at_exact)

    at_before = mentions("account_005", before_1s)
    assert not any(f.file_id == "evidence_54" for f in at_before)


def test_no_hardcoded_literals_in_solution_logic():
    """Ensure no case-number branches or literal resource/evidence IDs exist in code/*.py."""
    import re
    from pathlib import Path

    code_path = Path(__file__).resolve().parents[1]
    prohibited_pattern = re.compile(r"c_num|case_\d+|evidence_\d+|[a-z]+_\d{3,}|[a-z]+_\{")

    for py_file in code_path.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        matches = prohibited_pattern.findall(text)
        assert not matches, f"Prohibited hardcoded literal(s) found in {py_file.name}: {matches}"

