"""The per-task event log and the task_changed NOTIFY hint."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class LabelEvent:
    id: int
    task_id: int
    labels: list[str]
    creator_session: str | None


async def claim_label_event(conn: psycopg.AsyncConnection) -> LabelEvent | None:
    """Lock the oldest unscanned `label_added` event no other notifier holds. The caller keeps
    the transaction open until `mark_scanned`, so a second notifier skips the row."""
    cur = await conn.execute(
        "SELECT e.id, e.task_id, e.detail->'labels', t.created_by_session"
        " FROM events e JOIN tasks t ON t.id = e.task_id"
        " WHERE e.kind = 'label_added' AND e.scanned_at IS NULL"
        " ORDER BY e.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED"
    )
    row = await cur.fetchone()
    return LabelEvent(row[0], row[1], row[2] or [], row[3]) if row else None


async def mark_scanned(conn: psycopg.AsyncConnection, event_id: int) -> bool:
    """Flip one unscanned event to scanned; False when it already was."""
    cur = await conn.execute(
        "UPDATE events SET scanned_at = now() WHERE id = %s AND scanned_at IS NULL", (event_id,)
    )
    return cur.rowcount == 1
