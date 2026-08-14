"""Workload API — team projects, their tasks, and the calendar views.

Read routes are open to every workload member (criterion 5: everyone sees every
project). Write routes split two ways: projects are admin-only, tasks are
editable by the admin or by any member assigned to that project.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dependencies import require_csrf, require_workload, require_workload_admin
from services import workload_svc
from services.authz import WORKLOAD_ADMINS, WORKLOAD_MEMBERS, display_name
from services.workload_svc import WorkloadError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workload", tags=["workload"])

Status = Literal["pending", "complete", "cancel"]
View = Literal["month", "week", "day"]


# ── Request bodies ────────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name:         str = Field(min_length=1, max_length=200)
    client:       str = Field(default="", max_length=200)
    project_type: str = Field(default="", max_length=100)
    status:       Status = "pending"
    start_date:   str | None = None
    end_date:     str | None = None
    note:         str = Field(default="", max_length=2000)
    members:      list[str] = Field(default_factory=list, max_length=50)
    scaffold_default_tasks: bool = True


class ProjectUpdate(BaseModel):
    """Every field optional — only what is sent gets patched.

    `members=None` means "leave the roster alone"; `members=[]` clears it.
    """
    name:         str | None = Field(default=None, min_length=1, max_length=200)
    client:       str | None = Field(default=None, max_length=200)
    project_type: str | None = Field(default=None, max_length=100)
    status:       Status | None = None
    start_date:   str | None = None
    end_date:     str | None = None
    note:         str | None = Field(default=None, max_length=2000)
    members:      list[str] | None = Field(default=None, max_length=50)


class TaskCreate(BaseModel):
    title:    str = Field(min_length=1, max_length=200)
    assignee: str = Field(default="", max_length=120)
    due_date: str | None = None
    status:   Status = "pending"
    note:     str = Field(default="", max_length=2000)


class TaskUpdate(BaseModel):
    title:    str | None = Field(default=None, min_length=1, max_length=200)
    assignee: str | None = Field(default=None, max_length=120)
    due_date: str | None = None
    status:   Status | None = None
    note:     str | None = Field(default=None, max_length=2000)


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


def _require_task_editor(email: str, project_id: str) -> None:
    if not workload_svc.can_edit_tasks(email, project_id):
        raise HTTPException(403, "You can only edit tasks in projects assigned to you")


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
        "project_types": workload_svc.project_types(),
        "default_tasks": list(workload_svc.DEFAULT_TASKS),
        "statuses":      list(workload_svc.STATUSES),
    }


# ── Projects ──────────────────────────────────────────────────────────────────

@router.get("/projects")
async def list_projects(email: str = Depends(require_workload)):
    return workload_svc.list_projects()


@router.get("/projects/{project_id}")
async def get_project(project_id: str, email: str = Depends(require_workload)):
    project = workload_svc.get_project(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    project["can_edit_tasks"] = workload_svc.can_edit_tasks(email, project_id)
    return project


@router.post("/projects", dependencies=[Depends(require_csrf)])
async def create_project(body: ProjectCreate, email: str = Depends(require_workload_admin)):
    try:
        return workload_svc.create_project(**body.model_dump(), created_by=email)
    except WorkloadError as exc:
        raise _bad(exc)


@router.patch("/projects/{project_id}", dependencies=[Depends(require_csrf)])
async def update_project(
    project_id: str, body: ProjectUpdate, email: str = Depends(require_workload_admin)
):
    sent = _patch(body)
    try:
        return workload_svc.update_project(
            project_id, sent, members=sent.get("members")
        )
    except WorkloadError as exc:
        raise _bad(exc)


@router.delete("/projects/{project_id}", dependencies=[Depends(require_csrf)])
async def delete_project(project_id: str, email: str = Depends(require_workload_admin)):
    try:
        workload_svc.delete_project(project_id)
    except WorkloadError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


# ── Tasks ─────────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/tasks", dependencies=[Depends(require_csrf)])
async def create_task(
    project_id: str, body: TaskCreate, email: str = Depends(require_workload)
):
    _require_task_editor(email, project_id)
    try:
        return workload_svc.create_task(
            project_id=project_id, created_by=email, **body.model_dump()
        )
    except WorkloadError as exc:
        raise _bad(exc)


@router.patch("/tasks/{task_id}", dependencies=[Depends(require_csrf)])
async def update_task(task_id: str, body: TaskUpdate, email: str = Depends(require_workload)):
    task = workload_svc.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    _require_task_editor(email, task["project_id"])
    try:
        return workload_svc.update_task(task_id, _patch(body))
    except WorkloadError as exc:
        raise _bad(exc)


@router.delete("/tasks/{task_id}", dependencies=[Depends(require_csrf)])
async def delete_task(task_id: str, email: str = Depends(require_workload)):
    task = workload_svc.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    _require_task_editor(email, task["project_id"])
    workload_svc.delete_task(task_id)
    return {"ok": True}


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
        return workload_svc.list_tasks(
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
        return workload_svc.calendar(view, anchor, **filters)
    except WorkloadError as exc:
        raise _bad(exc)
