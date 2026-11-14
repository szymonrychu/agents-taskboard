"""Spec autostart routes (S-51): the per-project start queue with its state, move and remove of a
queued start, and the project's autostart limits.

The queue routes sit under `/specs` (so the orchestrator spec relay covers them) and are mounted
on every listener before the spec router, else `/specs/{key}` would capture `/specs/queue`. Remove
and the settings routes are user routes (web and delegate listeners). A move belongs to the user
and a superleader.
"""

from collections.abc import Callable
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, Request, Response

from agents_taskboard.api.actors import DEFAULT_PROJECT, Actor
from agents_taskboard.api.errors import ApiError
from agents_taskboard.api.schemas import (
    AutostartPut,
    AutostartView,
    ProjectQueue,
    QueueMove,
    SpecQueue,
)
from agents_taskboard.api.specs import require_user_or_superleader
from agents_taskboard.api.tasks import pool_of
from agents_taskboard.db import project_settings as settings_repo
from agents_taskboard.db import spec_starts as repo
from agents_taskboard.db.events import append_spec_event, notify_spec_changed


async def project_queues(
    conn: psycopg.AsyncConnection, actor: Actor, project: str | None
) -> list[dict[str, Any]]:
    """One entry per project in scope that has queued starts or a settings row. The state is
    `off` while a limit is unset, `idle` with an empty queue, else the head's hold or `ready`."""
    rows = await repo.list_queue(conn, actor, project)
    settings = {s.project: s for s in await settings_repo.list_settings(conn, actor, project)}
    items: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        items.setdefault(row["project"], []).append(
            {
                "id": row["id"],
                "spec": row["spec"],
                "title": row["title"],
                "position": row["rank"],
                "ordinal": row["ordinal"],
                "hold_reason": row["hold_reason"],
                "created_at": row["created_at"],
            }
        )
    out = []
    for name in sorted(items.keys() | settings.keys()):
        found = settings.get(name)
        queue = items.get(name, [])
        if found is None or not found.configured:
            state = "off"
        elif not queue:
            state = "idle"
        else:
            state = queue[0]["hold_reason"] or "ready"
        out.append(
            {
                "project": name,
                "max_active_sessions": found.max_active_sessions if found else None,
                "max_usage_pct": found.max_usage_pct if found else None,
                "state": state,
                "items": queue,
            }
        )
    return out


def settings_view(found: settings_repo.AutostartSettings) -> dict[str, Any]:
    return {
        "project": found.project,
        "max_active_sessions": found.max_active_sessions,
        "max_usage_pct": found.max_usage_pct,
        "configured": found.configured,
        "updated_at": found.updated_at,
    }


def build_autostart_router(actor_dep: Callable[..., Actor], *, user_routes: bool) -> APIRouter:
    router = APIRouter()
    ActorDep = Annotated[Actor, Depends(actor_dep)]

    @router.get("/specs/queue", response_model=SpecQueue)
    async def get_queue(
        request: Request, actor: ActorDep, project: str | None = None
    ) -> dict[str, Any]:
        async with pool_of(request).connection() as conn:
            return {"projects": await project_queues(conn, actor, project)}

    @router.post("/specs/queue/{start_id}/move", response_model=ProjectQueue)
    async def move_start(
        start_id: int, body: QueueMove, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        require_user_or_superleader(actor, "reorders the spec queue")
        async with pool_of(request).connection() as conn:
            found = await repo.get_scoped(conn, actor, start_id)
            if found is None:
                raise ApiError(404, "not_found", f"no queued start {start_id}")
            spec_id, project = found
            try:
                # a 1-based rank is what the repository's position already means: it orders the
                # queued ids (gaps in the stored positions do not matter) and rewrites them densely
                await repo.move(conn, start_id, position=body.position, before=body.before)
            except repo.NotQueued:
                raise ApiError(409, "not_queued", "the start is not in the queue") from None
            except repo.InvalidMove as exc:
                raise ApiError(422, "invalid_request", str(exc)) from None
            (queue,) = await project_queues(conn, actor, project)
            rank = next(i["position"] for i in queue["items"] if i["id"] == start_id)
            await append_spec_event(
                conn,
                spec_id,
                actor,
                "implementation_queue_moved",
                {"start_id": start_id, "position": rank},
            )
            await notify_spec_changed(conn, spec_id)
        return queue

    if user_routes:

        @router.delete("/specs/queue/{start_id}", status_code=204)
        async def remove_start(start_id: int, request: Request, actor: ActorDep) -> Response:
            async with pool_of(request).connection() as conn:
                if await repo.get_scoped(conn, actor, start_id) is None:
                    raise ApiError(404, "not_found", f"no queued start {start_id}")
                try:
                    spec_id = await repo.remove(conn, start_id)
                except repo.NotQueued:
                    raise ApiError(409, "not_queued", "the start is not in the queue") from None
                await append_spec_event(
                    conn, spec_id, actor, "implementation_trigger_removed", {"start_id": start_id}
                )
                await notify_spec_changed(conn, spec_id)
            return Response(status_code=204)

        @router.get("/projects/{slug}/autostart", response_model=AutostartView)
        async def get_autostart(slug: str, request: Request, actor: ActorDep) -> dict[str, Any]:
            async with pool_of(request).connection() as conn:
                return settings_view(await settings_repo.get_settings(conn, slug))

        @router.put("/projects/{slug}/autostart", response_model=AutostartView)
        async def put_autostart(
            slug: str, body: AutostartPut, request: Request, actor: ActorDep
        ) -> dict[str, Any]:
            if slug != DEFAULT_PROJECT:
                await request.app.state.projects.require_active(slug)
            async with pool_of(request).connection() as conn:
                saved = await settings_repo.put_settings(
                    conn, slug, body.max_active_sessions, body.max_usage_pct
                )
            return settings_view(saved)

    return router
