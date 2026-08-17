"""Workload API — team projects, their tasks, and the calendar views.

Read routes are open to every workload member (criterion 5: everyone sees every
project). Write routes split two ways: a project record is editable by whoever
created it (plus the admin), while its tasks are editable by anyone on the
project — creator, admin, or assigned member — so a member can schedule and
self-assign their own work without going through the admin.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dependencies import require_csrf, require_workload
from services import workload_svc
from services.authz import WORKLOAD_ADMINS, WORKLOAD_MEMBERS, display_name
from services.workload_svc import WorkloadError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workload", tags=["workload"])

Status = Literal["pending", "ongoing", "complete", "cancel"]          # tasks
ProjectStatus = Literal["pending", "ongoing", "complete", "cancel"]    # projects — a distinct domain
View = Literal["month", "week", "day"]


# ── Request bodies ────────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    project_code: str = Field(min_length=1, max_length=20, description="VNxxxx (VN + 4 digits)")
    name:         str = Field(min_length=1, max_length=200)
    client:       str = Field(default="", max_length=200)
    project_type: str = Field(default="", max_length=100)
    status:       ProjectStatus = "ongoing"
    note:         str = Field(default="", max_length=2000)
    members:      list[str] = Field(default_factory=list, max_length=50)
    tasks:        list[str] = Field(default_factory=lambda: list(workload_svc.DEFAULT_TASKS), max_length=20)


class ProjectUpdate(BaseModel):
    """Every field optional — only what is sent gets patched.

    `members=None` means "leave the roster alone"; `members=[]` clears it.
    """
    project_code: str | None = Field(default=None, min_length=1, max_length=20)
    name:         str | None = Field(default=None, min_length=1, max_length=200)
    client:       str | None = Field(default=None, max_length=200)
    project_type: str | None = Field(default=None, max_length=100)
    status:       ProjectStatus | None = None
    note:         str | None = Field(default=None, max_length=2000)
    members:      list[str] | None = Field(default=None, max_length=50)


class TaskCreate(BaseModel):
    title:      str = Field(min_length=1, max_length=200)
    assignees:  list[str] = Field(default_factory=list, max_length=10)
    due_date:   str | None = None
    start_date: str | None = None
    status:     Status = "ongoing"
    note:       str = Field(default="", max_length=2000)


class TaskUpdate(BaseModel):
    title:      str | None = Field(default=None, min_length=1, max_length=200)
    assignees:  list[str] | None = Field(default=None, max_length=10)
    due_date:   str | None = None
    start_date: str | None = None
    status:     Status | None = None
    note:       str | None = Field(default=None, max_length=2000)


class BulkAssignee(BaseModel):
    email: str = Field(min_length=1, max_length=120)
    add:   bool


def _patch(body: BaseModel, *, drop: set[str] = frozenset()) -> dict:
    """Only the fields the client actually sent.

    exclude_unset matters here: `due_date: None` sent explicitly means "clear
    the date", while an absent `due_date` means "don't touch it" — collapsing
    the two would wipe dates on every unrelated edit.
    """
    return {k: v for k, v in body.model_dump(exclude_unset=True).items() if k not in drop}


def _bad(exc: WorkloadError) -> HTTPException:
    """WorkloadError messages are written for the user, so pass them through."""
    return HTTPException(422, str(exc))


async def _require_task_editor(email: str, project_id: str) -> None:
    if not await workload_svc.can_edit_tasks(email, project_id):
        raise HTTPException(403, "You can only edit tasks in projects assigned to you")


async def _require_project_editor(email: str, project_id: str) -> None:
    if not await workload_svc.can_edit_project(email, project_id):
        raise HTTPException(403, "You can only edit projects you created")


# ── Meta ──────────────────────────────────────────────────────────────────────

@router.get("/meta")
async def meta(email: str = Depends(require_workload)):
    """Roster, dropdown options, and what this caller is allowed to do."""
    from services.authz import is_workload_admin
    return {
        "me": {
            "email":    email,
            "name":     display_name(email),
            "is_admin": is_workload_admin(email),
        },
        "members": [
            {"email": m, "name": display_name(m), "is_admin": m in WORKLOAD_ADMINS}
            for m in WORKLOAD_MEMBERS
        ],
        "project_types": await workload_svc.project_types(),
        "clients":       await workload_svc.clients(),
        "default_tasks": list(workload_svc.DEFAULT_TASKS),
        "statuses":      list(workload_svc.STATUSES),
    }


# ── Projects ──────────────────────────────────────────────────────────────────

@router.get("/projects")
async def list_projects(email: str = Depends(require_workload)):
    return await workload_svc.list_projects()


@router.get("/projects/{project_id}")
async def get_project(project_id: str, email: str = Depends(require_workload)):
    project = await workload_svc.get_project(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    project["can_edit_tasks"]   = await workload_svc.can_edit_tasks(email, project_id)
    project["can_edit_project"] = await workload_svc.can_edit_project(email, project_id)
    return project


@router.post("/projects", dependencies=[Depends(require_csrf)])
async def create_project(body: ProjectCreate, email: str = Depends(require_workload)):
    """Any workload member may start a project — they become its owner, which
    is what `can_edit_project` keys the later edit/delete checks on."""
    try:
        return await workload_svc.create_project(**body.model_dump(), created_by=email)
    except WorkloadError as exc:
        raise _bad(exc)


@router.patch("/projects/{project_id}", dependencies=[Depends(require_csrf)])
async def update_project(
    project_id: str, body: ProjectUpdate, email: str = Depends(require_workload)
):
    await _require_project_editor(email, project_id)
    sent = _patch(body)
    try:
        return await workload_svc.update_project(
            project_id, sent, members=sent.get("members")
        )
    except WorkloadError as exc:
        raise _bad(exc)


@router.delete("/projects/{project_id}", dependencies=[Depends(require_csrf)])
async def delete_project(project_id: str, email: str = Depends(require_workload)):
    await _require_project_editor(email, project_id)
    try:
        await workload_svc.delete_project(project_id)
    except WorkloadError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


# ── Tasks ─────────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/tasks", dependencies=[Depends(require_csrf)])
async def create_task(
    project_id: str, body: TaskCreate, email: str = Depends(require_workload)
):
    await _require_task_editor(email, project_id)
    try:
        return await workload_svc.create_task(
            project_id=project_id, created_by=email, **body.model_dump()
        )
    except WorkloadError as exc:
        raise _bad(exc)


@router.patch("/tasks/{task_id}", dependencies=[Depends(require_csrf)])
async def update_task(task_id: str, body: TaskUpdate, email: str = Depends(require_workload)):
    task = await workload_svc.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    await _require_task_editor(email, task["project_id"])
    try:
        return await workload_svc.update_task(task_id, _patch(body))
    except WorkloadError as exc:
        raise _bad(exc)


@router.delete("/tasks/{task_id}", dependencies=[Depends(require_csrf)])
async def delete_task(task_id: str, email: str = Depends(require_workload)):
    task = await workload_svc.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    await _require_task_editor(email, task["project_id"])
    await workload_svc.delete_task(task_id)
    return {"ok": True}


@router.post("/projects/{project_id}/tasks/bulk-assignee", dependencies=[Depends(require_csrf)])
async def bulk_assignee(project_id: str, body: BulkAssignee, email: str = Depends(require_workload)):
    """Add or remove one person across every task in the project at once."""
    await _require_task_editor(email, project_id)
    try:
        n = await workload_svc.bulk_set_task_assignee(project_id, body.email, body.add)
    except WorkloadError as exc:
        raise _bad(exc)
    return {"ok": True, "updated": n}


@router.get("/tasks")
async def list_tasks(
    email: str = Depends(require_workload),
    date_from:  str | None = Query(default=None),
    date_to:    str | None = Query(default=None),
    assignee:   str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status:     Status | None = Query(default=None),
):
    try:
        return await workload_svc.list_tasks(
            date_from=date_from, date_to=date_to, assignee=assignee,
            project_id=project_id, status=status,
        )
    except WorkloadError as exc:
        raise _bad(exc)


# ── Calendar ──────────────────────────────────────────────────────────────────

@router.get("/calendar")
async def calendar(
    email: str = Depends(require_workload),
    view:   View = Query(default="month"),
    anchor: str = Query(..., description="Any date inside the period, YYYY-MM-DD"),
    assignee:   str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status:     Status | None = Query(default=None),
):
    """One screen's worth of calendar data: tasks in range, plus every project."""
    filters = {k: v for k, v in
               {"assignee": assignee, "project_id": project_id, "status": status}.items()
               if v}
    try:
        return await workload_svc.calendar(view, anchor, **filters)
    except WorkloadError as exc:
        raise _bad(exc)
