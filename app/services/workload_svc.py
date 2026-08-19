"""Workload management — projects, their tasks, and the team calendar.

Backed by MongoDB (via Motor, the async driver) rather than the JSON files
the rest of this app uses. The usage log only ever appends, so a lock plus a
JSONL file is enough there; the workload data is edited in place by several
people at once (a member flipping a task to complete while the admin
reassigns another), and the container runs two Gunicorn workers. A
read-modify-write over a JSON file across two processes silently drops one of
the two updates — Mongo's server handles concurrent writers itself.

Every document write here is a single `update_one`/`insert_one` call, which
Mongo guarantees is atomic on its own; there is no multi-document transaction
wrapping e.g. "insert a project, then insert its seed tasks". A standalone
MongoDB instance (no replica set) can't do multi-document transactions at
all, and setting one up is more operational overhead than a 4-person tool
warrants. Where that gap matters (create_project can leave an orphan project
if the task insert that follows it fails) a compensating delete cleans up
after itself — see the comment there.

Permission rules live here as pure predicates (`can_edit_tasks`); the FastAPI
dependencies in `dependencies.py` build on them, so importing that module here
would be circular — same arrangement as `authz.py`.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import UpdateOne

from services.authz import WORKLOAD_MEMBERS, is_workload_admin

# VNxxxx (VN + 4 digits) — the human-facing project identifier, distinct
# from the internal `id` primary key used in URLs and joins.
_PROJECT_CODE_RE = re.compile(r"^VN\d{4}$")

# The seven tasks that make up a standard project here. Used to scaffold a new
# project so nobody has to retype them; entirely optional, and any project can
# add free-form tasks on top.
DEFAULT_TASKS: tuple[str, ...] = (
    "Script survey link",
    "Update survey link",
    "Final survey link",
    "Check data logic",
    "Check data base",
    "Prepare data tabspec",
    "Deliver topline CE",
    "Deliver topline OE",
)

# Suggested values only — `project_type` is a free-text field, so a new type
# can be typed in and will show up in the dropdown afterwards (see
# `project_types()`).
DEFAULT_PROJECT_TYPES: tuple[str, ...] = (
    "U&A", "CLT", "HUT", "D2D", "Retail Audit", "Tracking", "Ad-hoc",
    "Brand Health", "Concept Test",
)

# Task status. Kept as its own tuple, independent from PROJECT_STATUSES below
# — the two domains happen to share the same four words today, but that's
# coincidence, not a shared type; one can change without silently affecting
# the other.
STATUSES: tuple[str, ...] = ("pending", "ongoing", "complete", "cancel")

# Project status — a separate domain from task status.
PROJECT_STATUSES: tuple[str, ...] = ("pending", "ongoing", "complete", "cancel")


class WorkloadError(Exception):
    """Validation failure with a message safe to show the user."""


# ── Connection ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _client() -> AsyncIOMotorClient:
    """A single long-lived client, reused for the app's lifetime.

    Unlike the short-lived-connection-per-call pattern used for SQLite
    elsewhere in this app, Motor's client owns its own connection pool and is
    explicitly meant to be created once and shared — creating one per request
    would defeat that pooling.
    """
    from config import settings
    return AsyncIOMotorClient(settings.MONGODB_URI)


def _db() -> AsyncIOMotorDatabase:
    from config import settings
    return _client()[settings.MONGODB_DB]


def _projects():
    return _db()["projects"]


def _tasks():
    return _db()["tasks"]


def _activity():
    return _db()["activity"]


async def init_db() -> None:
    """Create indexes if absent. Safe to call on every startup — Mongo's
    create_index is a no-op when an equivalent index already exists, so no
    schema-migration bookkeeping is needed the way SQLite required."""
    await _projects().create_index("project_code", unique=True)
    await _projects().create_index("created_at")
    await _tasks().create_index("project_id")
    await _tasks().create_index("due_date")
    await _tasks().create_index("start_date")
    await _tasks().create_index("assignees")
    # The log is only ever read newest-first, optionally narrowed to one person.
    await _activity().create_index([("at", -1)])
    await _activity().create_index("actor")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _out(doc: dict | None) -> dict | None:
    """Mongo's `_id` -> our public `id`, keeping the API response shape
    identical to the SQLite version so the frontend needed no changes."""
    if doc is None:
        return None
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    return doc


def _out_task(doc: dict | None) -> dict | None:
    """`_out()`, plus migrating documents written before multi-assignee
    support: a lone `assignee` string becomes a one-item `assignees` list
    (or an empty one) instead of leaving `assignees` missing entirely, which
    would otherwise crash frontend code that expects an array."""
    d = _out(doc)
    if d is None:
        return None
    if "assignees" not in d:
        old = d.pop("assignee", "")
        d["assignees"] = [old] if old else []
    else:
        d.pop("assignee", None)
    if "start_date" not in d:
        # Documents written before start/end spans existed are single-day —
        # their start is their due date.
        d["start_date"] = d.get("due_date")
    return d


# ── Validation ────────────────────────────────────────────────────────────────

def _split_csv(value: str | None) -> list[str]:
    """Multi-select filters arrive as one comma-joined query param (e.g.
    'a@x.com,b@x.com') rather than repeated params — simpler on the JS side,
    and this is the one place that has to know about the join format."""
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _check_status(status: str, allowed: tuple[str, ...] = STATUSES) -> str:
    if status not in allowed:
        raise WorkloadError(f"Status {status!r} is invalid — expected one of {', '.join(allowed)}")
    return status


def _check_date(value: str | None, field: str) -> str | None:
    """Accept None/'' as 'no date', otherwise require ISO YYYY-MM-DD."""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise WorkloadError(f"{field} must be a YYYY-MM-DD date, got {value!r}")


def _check_span(start: str | None, due: str | None) -> tuple[str | None, str | None]:
    """Normalise a task's [start_date, due_date] range, returning both.

    The two ends default to each other, so supplying either one alone
    schedules a single-day task — setting just a start date is the common
    way to put work on the calendar, and leaving it unscheduled because the
    due date was blank would be surprising. Only when both are empty is the
    task genuinely unscheduled.
    """
    if not start and not due:
        return None, None
    if not due:
        return start, start
    if not start:
        return due, due
    if start > due:
        raise WorkloadError("start_date must be on or before due_date")
    return start, due


def _check_member(email: str | None, field: str = "assignee") -> str:
    """Empty means unassigned; anything else must be on the workload roster."""
    e = (email or "").strip().lower()
    if not e:
        return ""
    if e not in WORKLOAD_MEMBERS:
        raise WorkloadError(f"{field} {e!r} is not a workload member")
    return e


def _check_members(emails: list[str] | None, field: str = "assignee") -> list[str]:
    """A task can have several assignees — validate each, dedupe, sorted."""
    return sorted({e for e in (_check_member(m, field) for m in (emails or [])) if e})


def _check_task_assignees(emails: list[str] | None, project_members: list[str]) -> list[str]:
    """Task PIC must always be a subset of project PIC: validate roster
    membership (via `_check_members`) and then that each is already on the
    project's own member list."""
    checked = _check_members(emails)
    invalid = [e for e in checked if e not in project_members]
    if invalid:
        raise WorkloadError(
            f"{', '.join(invalid)} must be added to the project before being assigned to its tasks"
        )
    return checked


def _check_project_code(value: str) -> str:
    """Normalise to uppercase, then require the VNxxxx pattern (VN + 4 digits)."""
    v = (value or "").strip().upper()
    if not _PROJECT_CODE_RE.match(v):
        raise WorkloadError(f"Project ID must match VNxxxx (VN + 4 digits), got {value!r}")
    return v


# ── Projects ──────────────────────────────────────────────────────────────────

_UNDATED_SORT_KEY = "9999"  # sorts after any real ISO date, matching the old SQLite COALESCE trick


async def _attach_task_counts(projects: list[dict]) -> list[dict]:
    """Per-status task counts for each project, in one aggregation query
    rather than one query per project — the obvious per-row version turns a
    30-project list into 30 round trips."""
    ids = [p["_id"] for p in projects]
    if not ids:
        return []
    counts: dict[str, dict[str, int]] = {i: {s: 0 for s in STATUSES} for i in ids}
    cursor = _tasks().aggregate([
        {"$match": {"project_id": {"$in": ids}}},
        {"$group": {"_id": {"project_id": "$project_id", "status": "$status"}, "n": {"$sum": 1}}},
    ])
    async for row in cursor:
        pid, status = row["_id"]["project_id"], row["_id"]["status"]
        if pid in counts and status in counts[pid]:
            counts[pid][status] = row["n"]

    out = []
    for p in projects:
        d = _out(p)
        d["task_counts"] = counts[p["_id"]]
        d["task_total"] = sum(counts[p["_id"]].values())
        out.append(d)
    return out


async def list_projects() -> list[dict]:
    """Every project, newest first. Visible to all workload members (criterion 5)."""
    rows = await _projects().find().sort("created_at", -1).to_list(length=None)
    return await _attach_task_counts(rows)


async def get_project(project_id: str) -> dict | None:
    row = await _projects().find_one({"_id": project_id})
    if row is None:
        return None
    project = (await _attach_task_counts([row]))[0]
    task_rows = await _tasks().find({"project_id": project_id}).to_list(length=None)
    task_rows.sort(key=lambda t: (t["sort_order"], t["due_date"] or _UNDATED_SORT_KEY, t["created_at"]))
    project["tasks"] = [_out_task(t) for t in task_rows]
    return project


async def create_project(
    *,
    project_code: str,
    name: str,
    client: str = "",
    project_type: str = "",
    status: str = "ongoing",
    note: str = "",
    members: list[str] | None = None,
    tasks: list[str] | None = None,
    created_by: str = "",
) -> dict:
    """Create a project. *tasks* is the exact list of task titles to seed —
    the UI offers the 7 standard ones as pre-checked boxes so the creator can
    drop any that don't apply; omitting the argument entirely (rather than
    passing []) falls back to all 7, for callers other than that dialog."""
    name = (name or "").strip()
    if not name:
        raise WorkloadError("Project name is required")
    code = _check_project_code(project_code)
    _check_status(status, PROJECT_STATUSES)
    emails = [_check_member(m, "member") for m in (members or [])]
    emails = sorted({e for e in emails if e})
    # Normalised on the way in — `can_edit_project` compares against it, and
    # a casing mismatch there would silently lock a creator out of their own
    # project.
    created_by = (created_by or "").strip().lower()
    task_titles = [t.strip() for t in (DEFAULT_TASKS if tasks is None else tasks) if t and t.strip()]

    pid = _new_id()
    ts = _now()
    try:
        await _projects().insert_one({
            "_id": pid, "project_code": code, "name": name, "client": client.strip(),
            "project_type": project_type.strip(), "status": status, "note": note.strip(),
            "members": emails, "created_by": created_by, "created_at": ts, "updated_at": ts,
        })
    except Exception as exc:
        # The unique index on project_code is the actual race-proof guard;
        # this duplicate-key error is what it looks like when it fires.
        if "E11000" in str(exc):
            raise WorkloadError(f"Project ID {code} is already in use")
        raise

    if task_titles:
        # Every project member is auto-assigned to every seed task — tasks
        # can carry multiple assignees, so there's no ambiguity to resolve
        # the way there was with a single-assignee field.
        try:
            await _tasks().insert_many([
                {
                    "_id": _new_id(), "project_id": pid, "title": title, "assignees": list(emails),
                    "due_date": None, "status": "ongoing", "note": "", "sort_order": i,
                    "completed_at": None, "created_by": created_by, "created_at": ts, "updated_at": ts,
                }
                for i, title in enumerate(task_titles)
            ])
        except Exception:
            # No multi-document transaction here (see module docstring) — if
            # seeding tasks fails partway through, remove the project rather
            # than leave an orphan with an inconsistent task list.
            await _projects().delete_one({"_id": pid})
            await _tasks().delete_many({"project_id": pid})
            raise
    return await get_project(pid)  # type: ignore[return-value]


_PROJECT_FIELDS = {"project_code", "name", "client", "project_type", "status", "note"}


async def update_project(project_id: str, fields: dict, members: list[str] | None = None) -> dict:
    """Patch a project. Only keys present in *fields* are touched.

    *members* is replace-the-whole-list rather than add/remove, matching how the
    UI edits it (a multi-select); passing None leaves the roster untouched.
    """
    patch = {k: v for k, v in fields.items() if k in _PROJECT_FIELDS}
    if "status" in patch:
        _check_status(patch["status"], PROJECT_STATUSES)
    if "project_code" in patch:
        patch["project_code"] = _check_project_code(patch["project_code"])
    if "name" in patch:
        patch["name"] = (patch["name"] or "").strip()
        if not patch["name"]:
            raise WorkloadError("Project name is required")

    if members is not None:
        patch["members"] = sorted({e for e in (_check_member(m, "member") for m in members) if e})

    if not patch:
        if await _projects().find_one({"_id": project_id}, {"_id": 1}) is None:
            raise WorkloadError("Project not found")
        return await get_project(project_id)  # type: ignore[return-value]

    patch["updated_at"] = _now()
    try:
        result = await _projects().update_one({"_id": project_id}, {"$set": patch})
    except Exception as exc:
        if "E11000" in str(exc):
            raise WorkloadError(f"Project ID {patch['project_code']} is already in use")
        raise
    if result.matched_count == 0:
        raise WorkloadError("Project not found")

    if "members" in patch:
        # Task PIC must stay a subset of project PIC — dropping someone from
        # the project also drops them from that project's tasks.
        await _prune_task_assignees(project_id, patch["members"])

    return await get_project(project_id)  # type: ignore[return-value]


async def _prune_task_assignees(project_id: str, allowed_members: list[str]) -> None:
    """Trim every task's assignees down to *allowed_members* — called after a
    project's own member roster shrinks, to keep task PIC ⊆ project PIC."""
    allowed = set(allowed_members)
    task_rows = await _tasks().find({"project_id": project_id}, {"assignees": 1}).to_list(length=None)
    ts = _now()
    ops = []
    for t in task_rows:
        current = t.get("assignees") or []
        kept = sorted(set(current) & allowed)
        if kept != sorted(current):
            ops.append(UpdateOne({"_id": t["_id"]}, {"$set": {"assignees": kept, "updated_at": ts}}))
    if ops:
        await _tasks().bulk_write(ops)


async def delete_project(project_id: str) -> None:
    """Hard delete, including the project's tasks.

    Two separate deletes, not one atomic operation (see module docstring) —
    the project is removed first, so a crash between the two calls can leave
    orphan tasks but never a project that outlives its own deletion.
    """
    result = await _projects().delete_one({"_id": project_id})
    if result.deleted_count == 0:
        raise WorkloadError("Project not found")
    await _tasks().delete_many({"project_id": project_id})


async def project_label(project_id: str) -> tuple[str, str]:
    """(project_code, name) in one projected query.

    `get_project` would do for this, but it also runs a task-count aggregation
    and loads every task — needless work on a path (logging a task edit) that
    runs on every drag-and-drop of a calendar chip.
    """
    row = await _projects().find_one({"_id": project_id}, {"project_code": 1, "name": 1})
    if not row:
        return "", ""
    return row.get("project_code", ""), row.get("name", "")


async def project_types() -> list[str]:
    """Suggested types plus any that have actually been typed in."""
    used = set(await _projects().distinct("project_type", {"project_type": {"$ne": ""}}))
    return [*DEFAULT_PROJECT_TYPES, *sorted(used - set(DEFAULT_PROJECT_TYPES))]


async def clients() -> list[str]:
    """Every client/corporate name used so far, for the combobox — no fixed
    suggestions here since, unlike project types, there's no sensible default list."""
    used = await _projects().distinct("client", {"client": {"$ne": ""}})
    return sorted(used)


# ── Permissions ───────────────────────────────────────────────────────────────

async def can_edit_tasks(email: str, project_id: str) -> bool:
    """True if *email* may add/edit/delete tasks in *project_id*.

    Three ways in: the workload admin (everywhere), whoever created the
    project, or anyone on that project's member list. The member case is what
    lets someone self-assign to a task on a project they handle without
    needing the admin to do it for them.
    """
    who = (email or "").strip().lower()
    if is_workload_admin(who):
        return True
    row = await _projects().find_one(
        {"_id": project_id, "$or": [{"members": who}, {"created_by": who}]}, {"_id": 1}
    )
    return row is not None


async def can_edit_project(email: str, project_id: str) -> bool:
    """True if *email* may rename/re-scope/delete the project itself.

    Narrower than `can_edit_tasks`: being on a project lets you work its
    tasks, but only its creator (or the admin) can change or delete the
    project record — otherwise any member could delete a project out from
    under everyone else working it.
    """
    who = (email or "").strip().lower()
    if is_workload_admin(who):
        return True
    row = await _projects().find_one({"_id": project_id, "created_by": who}, {"_id": 1})
    return row is not None


# ── Tasks ─────────────────────────────────────────────────────────────────────

async def get_task(task_id: str) -> dict | None:
    return _out_task(await _tasks().find_one({"_id": task_id}))


async def create_task(
    *,
    project_id: str,
    title: str,
    assignees: list[str] | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
    status: str = "ongoing",
    note: str = "",
    created_by: str = "",
) -> dict:
    title = (title or "").strip()
    if not title:
        raise WorkloadError("Task title is required")
    _check_status(status)
    start, due = _check_span(
        _check_date(start_date, "start_date"), _check_date(due_date, "due_date")
    )

    project = await _projects().find_one({"_id": project_id})
    if project is None:
        raise WorkloadError("Project not found")
    who = _check_task_assignees(assignees, project.get("members", []))
    if not who:
        # Nobody named on creation — default to the project owner so a new
        # task never starts out with no PIC at all; the owner can hand it off
        # afterwards like any other assignee. Only when the owner is actually
        # on the project's own member list, since task PIC must stay a subset
        # of project PIC — an owner who isn't a member gets no default here,
        # same as before.
        owner = (project.get("created_by") or "").strip().lower()
        if owner in project.get("members", []):
            who = [owner]

    last = await _tasks().find({"project_id": project_id}).sort("sort_order", -1).limit(1).to_list(length=1)
    nxt = (last[0]["sort_order"] + 1) if last else 0

    tid = _new_id()
    ts = _now()
    await _tasks().insert_one({
        "_id": tid, "project_id": project_id, "title": title, "assignees": who,
        "due_date": due, "start_date": start,
        "status": status, "note": note.strip(), "sort_order": nxt,
        "completed_at": ts if status == "complete" else None,
        "created_by": created_by, "created_at": ts, "updated_at": ts,
    })
    return await get_task(tid)  # type: ignore[return-value]


_TASK_FIELDS = {"title", "assignees", "due_date", "start_date", "status", "note"}


async def update_task(task_id: str, fields: dict) -> dict:
    patch = {k: v for k, v in fields.items() if k in _TASK_FIELDS}
    if "status" in patch:
        _check_status(patch["status"])
    if "due_date" in patch:
        patch["due_date"] = _check_date(patch["due_date"], "due_date")
    if "start_date" in patch:
        patch["start_date"] = _check_date(patch["start_date"], "start_date")
    if "title" in patch:
        patch["title"] = (patch["title"] or "").strip()
        if not patch["title"]:
            raise WorkloadError("Task title is required")

    row = await _tasks().find_one({"_id": task_id})
    if row is None:
        raise WorkloadError("Task not found")

    if "due_date" in patch or "start_date" in patch:
        # Re-normalise the pair together, not each field alone — moving
        # due_date earlier than an unchanged start_date (or vice versa) has
        # to be caught even though only one of the two was actually sent, and
        # setting one end on a previously unscheduled task fills in the other.
        eff_due = patch["due_date"] if "due_date" in patch else row.get("due_date")
        eff_start = patch["start_date"] if "start_date" in patch else row.get("start_date")
        patch["start_date"], patch["due_date"] = _check_span(eff_start, eff_due)

    if "assignees" in patch:
        # Task PIC must be a subset of project PIC — look up the parent
        # project's own member list to validate against.
        project = await _projects().find_one({"_id": row["project_id"]}, {"members": 1})
        patch["assignees"] = _check_task_assignees(patch["assignees"], (project or {}).get("members", []))

    if "status" in patch and patch["status"] != row["status"]:
        # Stamped on the transition into 'complete' and cleared on the way
        # out, so re-opening a task doesn't leave a stale completion time.
        patch["completed_at"] = _now() if patch["status"] == "complete" else None

    if patch:
        patch["updated_at"] = _now()
        await _tasks().update_one({"_id": task_id}, {"$set": patch})
    return await get_task(task_id)  # type: ignore[return-value]


async def delete_task(task_id: str) -> None:
    result = await _tasks().delete_one({"_id": task_id})
    if result.deleted_count == 0:
        raise WorkloadError("Task not found")


async def bulk_set_task_assignee(project_id: str, email: str, add: bool) -> int:
    """Add or remove *email* across every task in the project in one action.

    *email* must already be a project member — enforced here the same way as
    a single-task edit, so this can't be used to smuggle in a non-member
    assignee project-wide. Returns how many tasks actually changed.
    """
    project = await _projects().find_one({"_id": project_id})
    if project is None:
        raise WorkloadError("Project not found")
    who = _check_member(email, "member")
    if not who or who not in project.get("members", []):
        raise WorkloadError(f"{email!r} is not a member of this project")

    task_rows = await _tasks().find({"project_id": project_id}, {"assignees": 1}).to_list(length=None)
    ts = _now()
    ops = []
    for t in task_rows:
        current = set(t.get("assignees") or [])
        updated = (current | {who}) if add else (current - {who})
        if updated != current:
            ops.append(UpdateOne({"_id": t["_id"]}, {"$set": {"assignees": sorted(updated), "updated_at": ts}}))
    if ops:
        await _tasks().bulk_write(ops)
    return len(ops)


async def list_tasks(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    assignee: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    include_undated: bool = False,
) -> list[dict]:
    """Tasks joined with their project, for the calendar and list views.

    A [date_from, date_to] window matches on *overlap* with each task's own
    [start_date, due_date] span, not on `due_date` alone — a task can run
    across several days now, and it belongs on every one of them, not just
    the day it ends. `include_undated` additionally keeps tasks with no due
    date at all (the scaffolded ones nobody has scheduled yet), which the UI
    shows in a separate 'unscheduled' bucket.
    """
    query: dict = {}

    frm = _check_date(date_from, "date_from")
    to = _check_date(date_to, "date_to")
    if frm and to:
        # $expr (not a plain field match) because a task's *effective* start
        # is start_date if set, else due_date — and older documents written
        # before start_date existed have no such field to compare against at
        # all, which a plain {"start_date": {"$lte": ...}} would silently
        # (and wrongly) exclude.
        overlap = {"$expr": {"$and": [
            {"$lte": [{"$ifNull": ["$start_date", "$due_date"]}, to]},
            {"$gte": ["$due_date", frm]},
        ]}}
        if include_undated:
            query["$or"] = [overlap, {"due_date": None}]
        else:
            query.update(overlap)
    elif frm:
        query["due_date"] = {"$gte": frm}
    elif to:
        query["due_date"] = {"$lte": to}

    if assignee:
        # $in against an array field matches "assignees contains any of
        # these" — the multi-select filter's whole point, and it degrades to
        # a plain single-value containment check when there's only one.
        emails = _check_members(_split_csv(assignee))
        if emails:
            query["assignees"] = {"$in": emails}
    if project_id:
        ids = _split_csv(project_id)
        if ids:
            query["project_id"] = {"$in": ids}
    if status:
        statuses = [_check_status(s) for s in _split_csv(status)]
        if statuses:
            query["status"] = {"$in": statuses}

    task_rows = await _tasks().find(query).to_list(length=None)
    if not task_rows:
        return []

    project_ids = {t["project_id"] for t in task_rows}
    project_rows = await _projects().find({"_id": {"$in": list(project_ids)}}).to_list(length=None)
    projects_by_id = {p["_id"]: p for p in project_rows}

    out = []
    for t in task_rows:
        p = projects_by_id.get(t["project_id"])
        d = _out_task(t)
        d["project_name"] = p["name"] if p else ""
        d["project_code"] = p.get("project_code", "") if p else ""
        d["project_client"] = p["client"] if p else ""
        d["project_type"] = p["project_type"] if p else ""
        d["project_status"] = p["status"] if p else ""
        d["project_members"] = p.get("members", []) if p else []
        d["project_created_by"] = p.get("created_by", "") if p else ""
        out.append(d)

    out.sort(key=lambda t: (t["due_date"] or _UNDATED_SORT_KEY, t["project_name"], t["sort_order"]))
    return out


# ── Calendar ──────────────────────────────────────────────────────────────────

def calendar_range(view: str, anchor: str) -> tuple[str, str]:
    """Inclusive [from, to] covering *view* around the *anchor* date.

    The month view returns whole weeks (Monday-start) so the grid it renders is
    always a complete 6x7 block with no ragged first and last rows.
    """
    try:
        d = date.fromisoformat(anchor)
    except ValueError:
        raise WorkloadError(f"anchor must be a YYYY-MM-DD date, got {anchor!r}")

    if view == "day":
        return d.isoformat(), d.isoformat()
    if view == "week":
        start = d - timedelta(days=d.weekday())
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    if view == "month":
        first = d.replace(day=1)
        grid_start = first - timedelta(days=first.weekday())
        return grid_start.isoformat(), (grid_start + timedelta(days=41)).isoformat()
    raise WorkloadError(f"Invalid view {view!r} — expected day, week or month")


async def calendar(view: str, anchor: str, **filters) -> dict:
    """Everything the calendar page needs for one screen, in one round trip."""
    frm, to = calendar_range(view, anchor)
    tasks = await list_tasks(date_from=frm, date_to=to, include_undated=True, **filters)
    dated = [t for t in tasks if t["due_date"]]
    undated = [t for t in tasks if not t["due_date"]]
    return {
        "view": view,
        "anchor": anchor,
        "from": frm,
        "to": to,
        "tasks": dated,
        "unscheduled": undated,
        "projects": await list_projects(),
    }


# ── Activity log ──────────────────────────────────────────────────────────────
# Append-only: who did what, to which project/task, and what changed. Written
# from the router layer, which is where the acting account is actually known —
# the functions above are called by tests and scripts with no session attached.
#
# Never deleted or expired. A team of four generates a few hundred entries a
# month at a few hundred bytes each, so unbounded growth is not a practical
# concern here, and silently expiring an audit trail would be a worse surprise
# than the disk it costs.

ACTIVITY_ACTIONS: tuple[str, ...] = (
    "project.create", "project.update", "project.delete",
    "task.create", "task.update", "task.delete", "task.bulk_assignee",
)

# Fields whose before/after is worth recording. Anything not listed (e.g.
# updated_at, which changes on literally every edit) would be noise.
_LOGGED_PROJECT_FIELDS = ("project_code", "name", "client", "project_type", "status", "note", "members")
_LOGGED_TASK_FIELDS = ("title", "assignees", "due_date", "start_date", "status", "note")


def diff_fields(before: dict | None, after: dict | None, fields: tuple[str, ...]) -> list[dict]:
    """Which of *fields* actually changed, as [{field, from, to}].

    Compared against the stored documents rather than the request body: a
    PATCH that re-sends a task's existing status shouldn't read as a change,
    and the server normalises some values (a start date filled in from a due
    date) that the request never mentioned but the log should show.
    """
    b, a = before or {}, after or {}
    return [
        {"field": f, "from": b.get(f), "to": a.get(f)}
        for f in fields
        if b.get(f) != a.get(f)
    ]


def diff_project(before: dict | None, after: dict | None) -> list[dict]:
    return diff_fields(before, after, _LOGGED_PROJECT_FIELDS)


def diff_task(before: dict | None, after: dict | None) -> list[dict]:
    return diff_fields(before, after, _LOGGED_TASK_FIELDS)


async def log_activity(
    *,
    actor: str,
    action: str,
    target: str = "",
    project_id: str | None = None,
    project_code: str = "",
    project_name: str = "",
    task_id: str | None = None,
    changes: list[dict] | None = None,
) -> None:
    """Append one entry. Callers are not expected to handle failures — see
    `routers.workload._log`, which swallows them: losing a log line must never
    turn a successful edit into an error the user sees."""
    await _activity().insert_one({
        "_id": _new_id(),
        "at": _now(),
        "actor": (actor or "").strip().lower(),
        "action": action,
        "target": target,
        "project_id": project_id,
        "project_code": project_code,
        "project_name": project_name,
        "task_id": task_id,
        "changes": changes or [],
    })


async def list_activity(
    *,
    limit: int = 300,
    actor: str | None = None,
    action: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """The most recent entries, newest first.

    *after* / *before* are full ISO-8601 instants, not calendar dates: the
    caller's day boundaries are in their own timezone, and converting them
    here would need a timezone the server doesn't know. Every stored `at` is
    UTC ISO-8601, a format whose lexical order matches chronological order,
    so a plain string range compare is exact.
    """
    query: dict = {}
    if actor:
        actors = [_check_member(a, "actor") for a in _split_csv(actor)]
        if actors:
            query["actor"] = {"$in": actors}
    if action:
        actions = _split_csv(action)
        for a in actions:
            if a not in ACTIVITY_ACTIONS:
                raise WorkloadError(f"Unknown action {a!r}")
        if actions:
            query["action"] = {"$in": actions}
    if after or before:
        at: dict = {}
        if after:
            at["$gte"] = after
        if before:
            at["$lte"] = before
        query["at"] = at

    rows = await _activity().find(query).sort("at", -1).limit(max(1, min(limit, 2000))).to_list(length=None)
    return [_out(r) for r in rows]
