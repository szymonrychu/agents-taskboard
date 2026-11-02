"""Tasks routes, mounted on both the web and the service app. Only the actor dependency
differs: a user on the web app, the forwarded agent identity on the service app."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from psycopg_pool import AsyncConnectionPool

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.errors import ApiError
from agents_taskboard.api.schemas import CommentCreate, ListStatus, TaskCreate, TaskPatch
from agents_taskboard.db import tasks as repo
from agents_taskboard.labels import normalize_labels
from agents_taskboard.settings import Settings

MAX_LIMIT = 200


def task_id_of(key: str) -> int:
    task_id = repo.parse_key(key)
    if task_id is None:
        raise ApiError(404, "not_found", f"no task {key}")
    return task_id


def pool_of(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


def public_url(request: Request) -> str:
    settings: Settings = request.app.state.settings
    return settings.public_base_url


async def load_one(request: Request, task_id: int) -> dict[str, Any]:
    """The full task JSON, or 404."""
    async with pool_of(request).connection() as conn:
        found = await repo.load_tasks(conn, [task_id], public_url(request))
    if not found:
        raise ApiError(404, "not_found", f"no task T-{task_id}")
    return found[0]


def _resolve_assignee(body: TaskCreate | TaskPatch, actor: Actor) -> tuple[bool, str | None]:
    """(set, session): whether the request names an assignee, and which one."""
    if body.assignee_self:
        if actor.session_id is None:
            raise ApiError(422, "invalid_request", "assignee_self needs an agent session")
        return True, actor.session_id
    return "assignee_session" in body.model_fields_set, body.assignee_session


def build_router(actor_dep: Callable[..., Actor]) -> APIRouter:
    router = APIRouter()
    ActorDep = Annotated[Actor, Depends(actor_dep)]

    @router.post("/tasks", status_code=201)
    async def create_task(
        body: TaskCreate, request: Request, response: Response, actor: ActorDep
    ) -> dict[str, Any]:
        _, assignee = _resolve_assignee(body, actor)
        new = repo.NewTask(
            title=body.title,
            body=body.body,
            kind=body.kind,
            status=body.status or ("idea" if body.kind == "idea" else "open"),
            labels=body.labels,
            assignee_session=assignee,
            client_key=body.client_key,
        )
        async with pool_of(request).connection() as conn:
            task_id, created = await repo.create_task(conn, actor, new)
        if not created:
            response.status_code = 200
        return await load_one(request, task_id)

    @router.get("/tasks")
    async def list_tasks(
        request: Request,
        actor: ActorDep,
        status: ListStatus = "active",
        session: str | None = None,
        mine: bool = False,
        waiting: bool = False,
        label: Annotated[list[str] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if mine:
            if actor.session_id is None:
                raise ApiError(422, "invalid_request", "mine needs an agent session")
            if session is not None and session != actor.session_id:
                raise ApiError(422, "invalid_request", "mine conflicts with session")
            session = actor.session_id
        try:
            labels = normalize_labels(label) if label else None
        except ValueError as exc:
            raise ApiError(422, "invalid_request", str(exc)) from None
        try:
            async with pool_of(request).connection() as conn:
                ids, next_cursor = await repo.list_task_ids(
                    conn,
                    status=status,
                    session=session,
                    waiting=waiting,
                    labels=labels,
                    limit=limit,
                    cursor=cursor,
                )
                items = await repo.load_tasks(conn, ids, public_url(request))
        except repo.BadCursor as exc:
            raise ApiError(422, "invalid_request", str(exc)) from None
        return {"items": items, "next_cursor": next_cursor}

    @router.get("/tasks/{key}")
    async def get_task(key: str, request: Request, actor: ActorDep) -> dict[str, Any]:
        return await load_one(request, task_id_of(key))

    @router.patch("/tasks/{key}")
    async def patch_task(
        key: str, body: TaskPatch, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        task_id = task_id_of(key)
        changes: dict[str, Any] = {}
        for field in ("title", "body", "status", "labels"):
            if field in body.model_fields_set:
                value = getattr(body, field)
                if value is None:
                    raise ApiError(422, "invalid_request", f"{field} cannot be null")
                changes[field] = value
        is_set, assignee = _resolve_assignee(body, actor)
        if is_set:
            changes["assignee_session"] = assignee
        if not changes:
            raise ApiError(422, "invalid_request", "no fields to change")
        try:
            async with pool_of(request).connection() as conn:
                await repo.patch_task(conn, actor, task_id, body.version, changes)
        except repo.NotFound:
            raise ApiError(404, "not_found", f"no task {key}") from None
        except repo.StaleVersion:
            raise ApiError(412, "stale_version", "task changed since it was read") from None
        return await load_one(request, task_id)

    @router.post("/tasks/{key}/comments", status_code=201)
    async def add_comment(
        key: str, body: CommentCreate, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        task_id = task_id_of(key)
        try:
            async with pool_of(request).connection() as conn:
                await repo.add_comment(conn, actor, task_id, body.body)
        except repo.NotFound:
            raise ApiError(404, "not_found", f"no task {key}") from None
        return await load_one(request, task_id)

    return router
