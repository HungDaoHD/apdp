"""Workload management — projects, their tasks, and the team calendar.

Backed by SQLite rather than the JSON files the rest of this app uses. The
usage log only ever appends, so a lock plus a JSONL file is enough there; the
workload data is edited in place by several people at once (a member flipping a
task to complete while the admin reassigns another), and the container runs two
Gunicorn workers. A read-modify-write over a JSON file across two processes
silently drops one of the two updates. sqlite3 is stdlib, and WAL mode makes
concurrent readers plus a single writer safe across processes.

Permission rules live here as pure predicates (`can_edit_tasks`); the FastAPI
dependencies in `dependencies.py` build on them, so importing that module here
would be circular — same arrangement as `authz.py`.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from services.authz import WORKLOAD_MEMBERS, is_workload_admin

# The seven tasks that make up a standard project here. Used to scaffold a new
# project so nobody has to retype them; entirely optional, and any project can
# add free-form tasks on top.
DEFAULT_TASKS: tuple[str, ...] = (
    "Script survey link",
    "Update survey link",
    "Check data logic",
    "Check base of data (hole count, handcount)",
    "Prepare data tabspec",
    "Deliver topline CE",
    "Deliver topline OE",
)

# Suggested values only — `project_type` is a free-text column, so a new type
# can be typed in and will show up in the dropdown afterwards (see
# `project_types()`).
DEFAULT_PROJECT_TYPES: tuple[str, ...] = (
    "U&A", "CLT", "HUT", "Tracking", "Ad-hoc", "Brand Health", "Concept Test",
)

STATUSES: tuple[str, ...] = ("pending", "complete", "cancel")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    client       TEXT NOT NULL DEFAULT '',
    project_type TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    start_date   TEXT,
    end_date     TEXT,
    note         TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    email      TEXT NOT NULL,
    PRIMARY KEY (project_id, email)
);

CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    assignee     TEXT NOT NULL DEFAULT '',
    due_date     TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    note         TEXT NOT NULL DEFAULT '',
    sort_order   INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_due      ON tasks(due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_project  ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
"""


class WorkloadError(Exception):
    """Validation failure with a message safe to show the user."""


# ── Connection ────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    """Resolve DATA_DIR the same way usage_log_svc does, then add workload.db."""
    from config import settings
    p = Path(settings.DATA_DIR)
    if not p.is_absolute():
        from config import _APP_DIR
        p = _APP_DIR / p
    p.mkdir(parents=True, exist_ok=True)
    return p / "workload.db"


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """A short-lived connection per operation.

    isolation_level=None puts sqlite3 in autocommit mode, so the explicit
    BEGIN IMMEDIATE in `_tx()` is the only transaction boundary — no hidden
    transaction is opened behind our back on the first INSERT.
    """
    con = sqlite3.connect(_db_path(), timeout=10.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        yield con
    finally:
        con.close()


@contextmanager
def _tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE so a concurrent writer in the other worker waits here
    (up to busy_timeout) instead of failing partway through with SQLITE_BUSY."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def init_db() -> None:
    """Create the schema if absent. Safe to call on every startup."""
    with _conn() as con:
        con.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


# ── Validation ────────────────────────────────────────────────────────────────

def _check_status(status: str) -> str:
    if status not in STATUSES:
        raise WorkloadError(f"Status {status!r} không hợp lệ — chỉ nhận: {', '.join(STATUSES)}")
    return status


def _check_date(value: str | None, field: str) -> str | None:
    """Accept None/'' as 'no date', otherwise require ISO YYYY-MM-DD."""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise WorkloadError(f"{field} phải là ngày dạng YYYY-MM-DD, nhận được {value!r}")


def _check_member(email: str | None, field: str = "assignee") -> str:
    """Empty means unassigned; anything else must be on the workload roster."""
    e = (email or "").strip().lower()
    if not e:
        return ""
    if e not in WORKLOAD_MEMBERS:
        raise WorkloadError(f"{field} {e!r} không nằm trong team workload")
    return e


# ── Projects ──────────────────────────────────────────────────────────────────

def _project_rows_to_dicts(con: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    """Attach members and per-status task counts to each project row.

    Two extra queries for the whole page rather than two per project — the
    obvious per-row version turns a 30-project list into 60 round trips.
    """
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))

    members: dict[str, list[str]] = {i: [] for i in ids}
    for m in con.execute(
        f"SELECT project_id, email FROM project_members WHERE project_id IN ({marks}) ORDER BY email",
        ids,
    ):
        members[m["project_id"]].append(m["email"])

    counts: dict[str, dict[str, int]] = {i: {s: 0 for s in STATUSES} for i in ids}
    for c in con.execute(
        f"SELECT project_id, status, COUNT(*) AS n FROM tasks "
        f"WHERE project_id IN ({marks}) GROUP BY project_id, status",
        ids,
    ):
        if c["status"] in counts[c["project_id"]]:
            counts[c["project_id"]][c["status"]] = c["n"]

    out = []
    for r in rows:
        d = dict(r)
        d["members"] = members[r["id"]]
        d["member_names"] = [e.split("@", 1)[0] for e in members[r["id"]]]
        d["task_counts"] = counts[r["id"]]
        d["task_total"] = sum(counts[r["id"]].values())
        out.append(d)
    return out


def list_projects() -> list[dict]:
    """Every project, newest first. Visible to all workload members (criterion 5)."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM projects ORDER BY COALESCE(start_date, '9999') DESC, created_at DESC"
        ).fetchall()
        return _project_rows_to_dicts(con, rows)


def get_project(project_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            return None
        project = _project_rows_to_dicts(con, [row])[0]
        project["tasks"] = [
            dict(t) for t in con.execute(
                "SELECT * FROM tasks WHERE project_id = ? "
                "ORDER BY sort_order, COALESCE(due_date, '9999'), created_at",
                (project_id,),
            )
        ]
        return project


def create_project(
    *,
    name: str,
    client: str = "",
    project_type: str = "",
    status: str = "pending",
    start_date: str | None = None,
    end_date: str | None = None,
    note: str = "",
    members: list[str] | None = None,
    scaffold_default_tasks: bool = True,
    created_by: str = "",
) -> dict:
    name = (name or "").strip()
    if not name:
        raise WorkloadError("Tên dự án là bắt buộc")
    _check_status(status)
    start = _check_date(start_date, "start_date")
    end = _check_date(end_date, "end_date")
    if start and end and end < start:
        raise WorkloadError("Ngày kết thúc không được sớm hơn ngày bắt đầu")
    emails = [_check_member(m, "member") for m in (members or [])]
    emails = sorted({e for e in emails if e})

    pid = _new_id()
    ts = _now()
    with _conn() as con, _tx(con):
        con.execute(
            "INSERT INTO projects (id, name, client, project_type, status, start_date, end_date,"
            " note, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, name, client.strip(), project_type.strip(), status, start, end,
             note.strip(), created_by, ts, ts),
        )
        con.executemany(
            "INSERT INTO project_members (project_id, email) VALUES (?,?)",
            [(pid, e) for e in emails],
        )
        if scaffold_default_tasks:
            con.executemany(
                "INSERT INTO tasks (id, project_id, title, assignee, due_date, status, note,"
                " sort_order, created_by, created_at, updated_at)"
                " VALUES (?,?,?,'',NULL,'pending','',?,?,?,?)",
                [(_new_id(), pid, title, i, created_by, ts, ts)
                 for i, title in enumerate(DEFAULT_TASKS)],
            )
    return get_project(pid)  # type: ignore[return-value]


_PROJECT_FIELDS = {"name", "client", "project_type", "status", "start_date", "end_date", "note"}


def update_project(project_id: str, fields: dict, members: list[str] | None = None) -> dict:
    """Patch a project. Only keys present in *fields* are touched.

    *members* is replace-the-whole-list rather than add/remove, matching how the
    UI edits it (a multi-select); passing None leaves the roster untouched.
    """
    patch = {k: v for k, v in fields.items() if k in _PROJECT_FIELDS}
    if "status" in patch:
        _check_status(patch["status"])
    if "start_date" in patch:
        patch["start_date"] = _check_date(patch["start_date"], "start_date")
    if "end_date" in patch:
        patch["end_date"] = _check_date(patch["end_date"], "end_date")
    if "name" in patch:
        patch["name"] = (patch["name"] or "").strip()
        if not patch["name"]:
            raise WorkloadError("Tên dự án là bắt buộc")

    emails = None
    if members is not None:
        emails = sorted({e for e in (_check_member(m, "member") for m in members) if e})

    with _conn() as con, _tx(con):
        row = con.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise WorkloadError("Không tìm thấy dự án")

        # Range check against the merged result, so patching only one end of the
        # range is still validated against the stored other end.
        start = patch.get("start_date", row["start_date"])
        end = patch.get("end_date", row["end_date"])
        if start and end and end < start:
            raise WorkloadError("Ngày kết thúc không được sớm hơn ngày bắt đầu")

        if patch:
            patch["updated_at"] = _now()
            sets = ", ".join(f"{k} = ?" for k in patch)
            con.execute(f"UPDATE projects SET {sets} WHERE id = ?",
                        [*patch.values(), project_id])
        if emails is not None:
            con.execute("DELETE FROM project_members WHERE project_id = ?", (project_id,))
            con.executemany("INSERT INTO project_members (project_id, email) VALUES (?,?)",
                            [(project_id, e) for e in emails])
    return get_project(project_id)  # type: ignore[return-value]


def delete_project(project_id: str) -> None:
    """Hard delete — ON DELETE CASCADE removes the project's tasks and members."""
    with _conn() as con, _tx(con):
        cur = con.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        if cur.rowcount == 0:
            raise WorkloadError("Không tìm thấy dự án")


def project_types() -> list[str]:
    """Suggested types plus any that have actually been typed in."""
    with _conn() as con:
        used = {
            r["project_type"] for r in
            con.execute("SELECT DISTINCT project_type FROM projects WHERE project_type <> ''")
        }
    return [*DEFAULT_PROJECT_TYPES, *sorted(used - set(DEFAULT_PROJECT_TYPES))]


# ── Permissions ───────────────────────────────────────────────────────────────

def can_edit_tasks(email: str, project_id: str) -> bool:
    """True if *email* may add/edit/delete tasks in *project_id*.

    Admins everywhere; members only inside projects they are assigned to
    (criterion 5: everyone reads every project, members write only their own).
    """
    if is_workload_admin(email):
        return True
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND email = ?",
            (project_id, (email or "").strip().lower()),
        ).fetchone()
    return row is not None


# ── Tasks ─────────────────────────────────────────────────────────────────────

def get_task(task_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def create_task(
    *,
    project_id: str,
    title: str,
    assignee: str = "",
    due_date: str | None = None,
    status: str = "pending",
    note: str = "",
    created_by: str = "",
) -> dict:
    title = (title or "").strip()
    if not title:
        raise WorkloadError("Tên task là bắt buộc")
    _check_status(status)
    due = _check_date(due_date, "due_date")
    who = _check_member(assignee)

    tid = _new_id()
    ts = _now()
    with _conn() as con, _tx(con):
        if con.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise WorkloadError("Không tìm thấy dự án")
        nxt = con.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM tasks WHERE project_id = ?",
            (project_id,),
        ).fetchone()["n"]
        con.execute(
            "INSERT INTO tasks (id, project_id, title, assignee, due_date, status, note,"
            " sort_order, completed_at, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, project_id, title, who, due, status, note.strip(), nxt,
             ts if status == "complete" else None, created_by, ts, ts),
        )
    return get_task(tid)  # type: ignore[return-value]


_TASK_FIELDS = {"title", "assignee", "due_date", "status", "note"}


def update_task(task_id: str, fields: dict) -> dict:
    patch = {k: v for k, v in fields.items() if k in _TASK_FIELDS}
    if "status" in patch:
        _check_status(patch["status"])
    if "due_date" in patch:
        patch["due_date"] = _check_date(patch["due_date"], "due_date")
    if "assignee" in patch:
        patch["assignee"] = _check_member(patch["assignee"])
    if "title" in patch:
        patch["title"] = (patch["title"] or "").strip()
        if not patch["title"]:
            raise WorkloadError("Tên task là bắt buộc")

    with _conn() as con, _tx(con):
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise WorkloadError("Không tìm thấy task")
        if "status" in patch and patch["status"] != row["status"]:
            # Stamped on the transition into 'complete' and cleared on the way
            # out, so re-opening a task doesn't leave a stale completion time.
            patch["completed_at"] = _now() if patch["status"] == "complete" else None
        if patch:
            patch["updated_at"] = _now()
            sets = ", ".join(f"{k} = ?" for k in patch)
            con.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*patch.values(), task_id])
    return get_task(task_id)  # type: ignore[return-value]


def delete_task(task_id: str) -> None:
    with _conn() as con, _tx(con):
        cur = con.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount == 0:
            raise WorkloadError("Không tìm thấy task")


def list_tasks(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    assignee: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    include_undated: bool = False,
) -> list[dict]:
    """Tasks joined with their project, for the calendar and list views.

    A date range filters on `due_date`; `include_undated` additionally keeps
    tasks with no due date (the scaffolded ones nobody has scheduled yet),
    which the UI shows in a separate 'unscheduled' bucket.
    """
    where: list[str] = []
    args: list[str] = []

    frm = _check_date(date_from, "date_from")
    to = _check_date(date_to, "date_to")
    if frm and to:
        clause = "(t.due_date BETWEEN ? AND ?)"
        args += [frm, to]
        if include_undated:
            clause = f"({clause} OR t.due_date IS NULL)"
        where.append(clause)
    elif frm:
        where.append("t.due_date >= ?"); args.append(frm)
    elif to:
        where.append("t.due_date <= ?"); args.append(to)

    if assignee:
        where.append("t.assignee = ?"); args.append(_check_member(assignee))
    if project_id:
        where.append("t.project_id = ?"); args.append(project_id)
    if status:
        where.append("t.status = ?"); args.append(_check_status(status))

    sql = (
        "SELECT t.*, p.name AS project_name, p.client AS project_client,"
        " p.project_type AS project_type, p.status AS project_status"
        " FROM tasks t JOIN projects p ON p.id = t.project_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(t.due_date, '9999'), p.name, t.sort_order"

    with _conn() as con:
        return [dict(r) for r in con.execute(sql, args)]


# ── Calendar ──────────────────────────────────────────────────────────────────

def calendar_range(view: str, anchor: str) -> tuple[str, str]:
    """Inclusive [from, to] covering *view* around the *anchor* date.

    The month view returns whole weeks (Monday-start) so the grid it renders is
    always a complete 6x7 block with no ragged first and last rows.
    """
    try:
        d = date.fromisoformat(anchor)
    except ValueError:
        raise WorkloadError(f"anchor phải là ngày dạng YYYY-MM-DD, nhận được {anchor!r}")

    if view == "day":
        return d.isoformat(), d.isoformat()
    if view == "week":
        start = d - timedelta(days=d.weekday())
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    if view == "month":
        first = d.replace(day=1)
        grid_start = first - timedelta(days=first.weekday())
        return grid_start.isoformat(), (grid_start + timedelta(days=41)).isoformat()
    raise WorkloadError(f"View {view!r} không hợp lệ — chỉ nhận: day, week, month")


def calendar(view: str, anchor: str, **filters) -> dict:
    """Everything the calendar page needs for one screen, in one round trip."""
    frm, to = calendar_range(view, anchor)
    tasks = list_tasks(date_from=frm, date_to=to, include_undated=True, **filters)
    dated = [t for t in tasks if t["due_date"]]
    undated = [t for t in tasks if not t["due_date"]]
    return {
        "view": view,
        "anchor": anchor,
        "from": frm,
        "to": to,
        "tasks": dated,
        "unscheduled": undated,
        "projects": list_projects(),
    }
