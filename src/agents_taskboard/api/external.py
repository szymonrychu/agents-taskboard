"""External changes: another service (agents-mindmap) tells the taskboard that something linked
to a task changed, so the task's audience gets a `linked` notice.

The route lives on the delegate listener only and opens with the mindmap token list alone: a
delegate token acts as a user on every other delegate route and is refused here (403
`forbidden_token`), and a mindmap token is refused on every other path (401). One token, one
caller (secrets invariant 9)."""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from agents_common.service_gate import EXEMPT_PATHS, HEALTH_METHODS, ServiceTokenGate
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.errors import ApiError, error_response
from agents_taskboard.api.tasks import pool_of, task_id_of
from agents_taskboard.db import events as events_repo

EXTERNAL_PATH = re.compile(r"/taskboard/api/v1/tasks/[^/]+/external-changes")


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            token = token.strip()
            return token if scheme.lower() == "bearer" and token else None
    return None


class ExternalChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["mindmap"]
    ref: Annotated[str, StringConstraints(pattern=r"^M-[1-9][0-9]*$", max_length=40)]
    actor_session: str | None

    @field_validator("actor_session")
    @classmethod
    def _uuid(cls, value: str | None) -> str | None:
        if value is not None:
            uuid.UUID(value)
        return value


def build_external_router() -> APIRouter:
    router = APIRouter()

    @router.post("/tasks/{key}/external-changes", status_code=202)
    async def external_change(key: str, body: ExternalChange, request: Request) -> dict[str, Any]:
        task_id = task_id_of(key)
        actor = (
            Actor(kind="agent", session_id=body.actor_session)
            if body.actor_session
            else Actor(kind="user")
        )
        async with pool_of(request).connection() as conn:
            found = await conn.execute("SELECT 1 FROM tasks WHERE id = %s", (task_id,))
            if await found.fetchone() is None:
                raise ApiError(404, "not_found", f"no task {key}")
            await events_repo.append_event(
                conn,
                task_id,
                actor,
                "external_change",
                {"source": body.source, "ref": body.ref},
            )
            await events_repo.notify_changed(conn, task_id)
        return {"status": "accepted"}

    return router


class DelegateGate:
    """Bearer gate of the delegate listener. The external-changes path takes mindmap tokens
    (delegate tokens: 403 `forbidden_token`); every other path takes delegate tokens only
    (mindmap tokens: 401). Health probes are exempt. Digest compare via ServiceTokenGate."""

    def __init__(self, app: ASGIApp, delegate_tokens: list[str], mindmap_tokens: list[str]):
        self.app = app
        self.delegate = ServiceTokenGate(app, delegate_tokens)
        self.mindmap = ServiceTokenGate(app, mindmap_tokens)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.delegate(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in EXEMPT_PATHS and scope["method"] in HEALTH_METHODS:
            await self.app(scope, receive, send)
            return
        if not EXTERNAL_PATH.fullmatch(path):
            await self.delegate(scope, receive, send)
            return
        token = _bearer(scope)
        # evaluate both lists so the timing does not tell which list a token belongs to
        is_mindmap = token is not None and self.mindmap.authenticate(token)
        is_delegate = token is not None and self.delegate.authenticate(token)
        if is_mindmap:
            await self.app(scope, receive, send)
        elif is_delegate:
            response = error_response(
                403, "forbidden_token", "this route takes the mindmap token only"
            )
            await response(scope, receive, send)
        else:
            response = JSONResponse(
                {"error": {"code": "unauthorized", "message": "a valid service token is required"}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
