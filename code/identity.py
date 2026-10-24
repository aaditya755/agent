"""Identity resolution module for Ghost Data Still Exists.

Resolves request subjects to stable user_id, recovering current and historical
identifiers up to audit_at cutoff.
"""

import hashlib
import sys
from pathlib import Path
from typing import Optional

_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from config import FACTS_DB_PATH
from schemas import IdentityBag
from facts import ensure_facts_store


def resolve(email: str, audit_at: str, db_path: Optional[Path] = None) -> IdentityBag:
    """Resolve an incoming request email to an IdentityBag at point-in-time audit_at.

    Args:
        email: Provided contact/request email address.
        audit_at: Temporal cutoff ISO timestamp. Evidence or changes after this are ignored.
        db_path: Optional path to facts sqlite database.

    Returns:
        IdentityBag with user_id, current_email, emails, names, phones, email_hashes, resolve_method.
    """
    con = ensure_facts_store(db_path)
    cur = con.cursor()

    email_norm = email.strip().lower()

    user_id = ""
    resolve_method = "unresolved"

    # Step 1: Check current identities snapshot
    cur.execute(
        "SELECT user_id, name, email, phone, email_hash FROM user_identities WHERE LOWER(email) = ?",
        (email_norm,),
    )
    row = cur.fetchone()
    if row:
        user_id = row["user_id"]
        resolve_method = "current"
    else:
        # Step 2: Check historical audit log for email changes at or before audit_at
        cur.execute(
            """
            SELECT user_id, event_time, field_name, old_value, new_value
            FROM user_audit_log
            WHERE field_name = 'email'
              AND event_time <= ?
              AND (LOWER(old_value) = ? OR LOWER(new_value) = ?)
            ORDER BY event_time DESC
            LIMIT 1
            """,
            (audit_at, email_norm, email_norm),
        )
        audit_row = cur.fetchone()
        if audit_row:
            user_id = audit_row["user_id"]
            resolve_method = "historical"
        else:
            # Step 3: Check LINK_RESOURCE_IDENTITY in system_activity
            cur.execute(
                """
                SELECT subject_reference
                FROM system_activity
                WHERE event_type = 'LINK_RESOURCE_IDENTITY'
                  AND event_time <= ?
                  AND LOWER(subject_reference) = ?
                ORDER BY event_time DESC
                LIMIT 1
                """,
                (audit_at, email_norm),
            )
            link_row = cur.fetchone()
            if link_row:
                user_id = link_row["subject_reference"]
                resolve_method = "link"

    if not user_id:
        return IdentityBag(
            user_id="",
            current_email=email_norm,
            emails={email_norm},
            resolve_method="unresolved",
        )

    # Reconstruct all known identifiers up to audit_at
    emails = {email_norm}
    names = set()
    phones = set()
    email_hashes = {hashlib.sha256(email_norm.encode("utf-8")).hexdigest()}

    # Fetch current snapshot attributes for user_id
    cur.execute(
        "SELECT user_id, name, email, phone, email_hash FROM user_identities WHERE user_id = ?",
        (user_id,),
    )
    user_row = cur.fetchone()
    current_email = email_norm
    if user_row:
        if user_row["email"]:
            c_email = user_row["email"].strip().lower()
            current_email = c_email
            emails.add(c_email)
            email_hashes.add(hashlib.sha256(c_email.encode("utf-8")).hexdigest())
        if user_row["name"]:
            names.add(user_row["name"].strip())
        if user_row["phone"]:
            phones.add(user_row["phone"].strip())
        if user_row["email_hash"]:
            email_hashes.add(user_row["email_hash"].strip())

    # Fetch historical changes up to audit_at
    cur.execute(
        """
        SELECT field_name, old_value, new_value
        FROM user_audit_log
        WHERE user_id = ? AND event_time <= ?
        ORDER BY event_time ASC
        """,
        (user_id, audit_at),
    )
    for h in cur.fetchall():
        fname = h["field_name"]
        for val in [h["old_value"], h["new_value"]]:
            if not val or not val.strip():
                continue
            val_clean = val.strip()
            if fname == "email":
                v_lower = val_clean.lower()
                emails.add(v_lower)
                email_hashes.add(hashlib.sha256(v_lower.encode("utf-8")).hexdigest())
            elif fname == "name":
                names.add(val_clean)
            elif fname == "phone":
                phones.add(val_clean)

    return IdentityBag(
        user_id=user_id,
        current_email=current_email,
        emails=emails,
        names=names,
        phones=phones,
        email_hashes=email_hashes,
        resolve_method=resolve_method,
    )
