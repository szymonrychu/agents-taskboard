"""The per-task event log and the task_changed NOTIFY hint."""

from __future__ import annotations

from typing import Any

import psycopg
from agents_common.db.pool import notify
from psycopg.types.json import Jsonb

from agents_taskboard.api.actors import Actor

CHANNEL = "task_changed"


async def append_event(
    conn: psycopg.AsyncConnection,
    task_id: int,
    actor: Actor,
    kind: str,
    detail: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO events (task_id, actor_kind, actor_session, actor_slug, kind, detail)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (
            task_id,
            actor.kind,
            actor.session_id,
            actor.slug,
            kind,
            Jsonb(detail) if detail else None,
        ),
    )


async def notify_changed(conn: psycopg.AsyncConnection, task_id: int) -> None:
    """The payload is the task id only and a hint; consumers must also poll."""
    await notify(conn, CHANNEL, str(task_id))
