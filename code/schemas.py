"""Data schema models for Ghost Data Still Exists.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Set


@dataclass
class Fact:
    """Represents an atomic grounded observation from evidence at point-in-time."""
    file_id: str
    captured_at: str
    system_id: str
    topology: str
    resource_id: str
    kind: str
    fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IdentityBag:
    """Reconstructed identity attributes for a user subject up to audit_at."""
    user_id: str
    current_email: str
    emails: Set[str] = field(default_factory=set)
    names: Set[str] = field(default_factory=set)
    phones: Set[str] = field(default_factory=set)
    email_hashes: Set[str] = field(default_factory=set)
    resolve_method: str = "unresolved"  # "current", "historical", "link", "unresolved"
