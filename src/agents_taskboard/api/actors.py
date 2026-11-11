"""Who is acting: a user on the web listener, an agent on the service listener.

The web listener never reads agent headers. On the service listener the bearer gate (the
orchestrator relay's token) has already run when a route resolves the actor, so the
`X-Orch-Session`, `X-Orch-Slug` and `X-Orch-Projects` headers it forwards can be trusted.
`X-Orch-Projects` is the caller's project scope: `*` (every project) or a comma list of 1..10
project slugs whose first is the home project (where a create without `project` lands).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

from agents_taskboard.api.errors import ApiError

SESSION_HEADER = "X-Orch-Session"
SLUG_HEADER = "X-Orch-Slug"
PROJECTS_HEADER = "X-Orch-Projects"
ALL_PROJECTS = "*"
DEFAULT_PROJECT = "default"
PROJECT_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
MAX_PROJECTS = 10


@dataclass(frozen=True)
class Actor:
    kind: Literal["user", "agent"]
    session_id: str | None = None
    slug: str | None = None
    projects: frozenset[str] | None = None  # None: every project
    home_project: str | None = None  # first of the header list; None: every project

    def may_use(self, project: str) -> bool:
        return self.projects is None or project in self.projects

    def default_project(self) -> str:
        """Where a create without `project` goes: an agent's home project, else `default`."""
        return self.home_project or DEFAULT_PROJECT


def actor_from_web() -> Actor:
    return Actor(kind="user")


def actor_from_service(request: Request) -> Actor:
    session = request.headers.get(SESSION_HEADER, "").strip()
    if not session:
        raise ApiError(400, "actor_required", f"{SESSION_HEADER} header is required")
    scope = request.headers.get(PROJECTS_HEADER, "").strip()
    slug = request.headers.get(SLUG_HEADER, "").strip() or None
    if scope == ALL_PROJECTS:
        return Actor(kind="agent", session_id=session, slug=slug)
    listed = scope.split(",")
    if (
        len(listed) > MAX_PROJECTS
        or len(set(listed)) != len(listed)
        or not all(PROJECT_SLUG.fullmatch(p) for p in listed)
    ):
        raise ApiError(
            400,
            "actor_required",
            f"{PROJECTS_HEADER} header must be {ALL_PROJECTS} or 1..{MAX_PROJECTS} distinct"
            " comma-separated projects",
        )
    return Actor(
        kind="agent",
        session_id=session,
        slug=slug,
        projects=frozenset(listed),
        home_project=listed[0],
    )
