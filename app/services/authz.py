"""Access control — who may use the app, and who administers it.

QMe authenticates *that* an account is genuine; it says nothing about whether
that account belongs to this team. Without the list below, any QMe account
holder who can reach the public URL is a fully privileged user of SurveyFlow.

Pure data + predicates on purpose: `dependencies.py` builds the FastAPI
dependencies on top of this, so importing it here would be circular.
"""
from __future__ import annotations

# Accounts allowed to sign in at all. Doubles as the roster shown on the usage
# log page (imported there as FW_USERS).
ALLOWED_USERS: frozenset[str] = frozenset({
    "hung.dao@asia-plus.net",
    "bichthao.duong@asia-plus.net",
    "huyen.nguyen94@asia-plus.net",
    "pha.dang@asia-plus.net",
    "thuthao.nguyen@asia-plus.net",
    "diem.nguyen@asia-plus.net",
    "huy.le@asia-plus.net",
    "lieu.tran@asia-plus.net",
    "tananh.nguyen@asia-plus.net",
    "tan.nguyen@asia-plus.net",
    "tam.nguyen@asia-plus.net",
    "vananh.do@asia-plus.net",
    "thuydo@kadence.com.vn",
})

# Administrators: usage log, and download of the raw (PII-bearing) data files.
ADMIN_EMAILS: frozenset[str] = frozenset({
    "hung.dao@asia-plus.net",
})


def _norm(email: str | None) -> str:
    """Normalise an email for comparison — QMe casing is not guaranteed."""
    return (email or "").strip().lower()


def is_allowed(email: str | None) -> bool:
    """True if *email* may use the application at all."""
    return _norm(email) in ALLOWED_USERS


def is_admin(email: str | None) -> bool:
    """True if *email* may reach administrator-only data."""
    return _norm(email) in ADMIN_EMAILS
