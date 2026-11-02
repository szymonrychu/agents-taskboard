"""Who is acting: a user on the web listener, an agent on the service listener.

The web listener never reads agent headers. On the service listener the bearer gate (the
orchestrator relay's token) has already run when a route resolves the actor, so the
`X-Orch-Session` and `X-Orch-Slug` headers it forwards can be trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi import Request

from agents_taskboard.api.errors import ApiError

SESSION_HEADER = "X-Orch-Session"
SLUG_HEADER = "X-Orch-Slug"


@dataclass(frozen=True)
class Actor:
    kind: Literal["user", "agent"]
    session_id: str | None = None
    slug: str | None = None


def actor_from_web() -> Actor:
    return Actor(kind="user")


def actor_from_service(request: Request) -> Actor:
    session = request.headers.get(SESSION_HEADER, "").strip()
    if not session:
        raise ApiError(400, "actor_required", f"{SESSION_HEADER} header is required")
    slug = request.headers.get(SLUG_HEADER, "").strip() or None
    return Actor(kind="agent", session_id=session, slug=slug)
