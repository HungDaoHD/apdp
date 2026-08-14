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
    "thang.la@asia-plus.net",
    "thuydo@kadence.com.vn",
})

# Administrators: usage log, and download of the raw (PII-bearing) data files.
ADMIN_EMAILS: frozenset[str] = frozenset({
    "hung.dao@asia-plus.net",
})

# ── Workload module ───────────────────────────────────────────────────────────
# A narrower roster than ALLOWED_USERS: signing in to SurveyFlow does not by
# itself grant access to the team's workload calendar. Kept as an ordered tuple
# (not a set) because the week view renders one row per member and that order
# should be stable across page loads.
WORKLOAD_MEMBERS: tuple[str, ...] = (
    "hung.dao@asia-plus.net",
    "tam.nguyen@asia-plus.net",
    "vananh.do@asia-plus.net",
    "thang.la@asia-plus.net",
)

_WORKLOAD_MEMBER_SET: frozenset[str] = frozenset(WORKLOAD_MEMBERS)

# Workload administrators: create/edit/delete projects and assign members.
WORKLOAD_ADMINS: frozenset[str] = frozenset({
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


def is_workload_user(email: str | None) -> bool:
    """True if *email* may open the workload calendar at all."""
    return _norm(email) in _WORKLOAD_MEMBER_SET


def is_workload_admin(email: str | None) -> bool:
    """True if *email* may create projects and assign them to members."""
    return _norm(email) in WORKLOAD_ADMINS


def display_name(email: str | None) -> str:
    """Short label for *email* — the local part, e.g. 'hung.dao'.

    Calendar cells are narrow; a full address would wrap or be truncated in
    every chip, and within one company domain the local part is unambiguous.
    """
    return _norm(email).split("@", 1)[0]
