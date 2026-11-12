"""The event log of tasks and specs and their NOTIFY hints (task_changed, spec_changed)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from agents_common.db.pool import notify
from psycopg.types.json import Jsonb

from agents_taskboard.api.actors import Actor

CHANNEL = "task_changed"
SPEC_CHANNEL = "spec_changed"


async def append_event(
    conn: psycopg.AsyncConnection,
    task_id: int,
    actor: Actor,
    kind: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Log one event of the task, stamped with the task's current project."""
    await conn.execute(
        "INSERT INTO events (task_id, project, actor_kind, actor_session, actor_slug, kind,"
        " detail) SELECT t.id, t.project, %s, %s, %s, %s, %s FROM tasks t WHERE t.id = %s",
        (
            actor.kind,
            actor.session_id,
            actor.slug,
            kind,
            Jsonb(detail) if detail else None,
            task_id,
        ),
    )


async def notify_changed(conn: psycopg.AsyncConnection, task_id: int) -> None:
    """The payload is the task id only and a hint; consumers must also poll."""
    await notify(conn, CHANNEL, str(task_id))


async def append_spec_event(
    conn: psycopg.AsyncConnection,
    spec_id: int,
    actor: Actor,
    kind: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Log one event of the spec (no task), stamped with the spec's current project."""
    await conn.execute(
        "INSERT INTO events (spec_id, project, actor_kind, actor_session, actor_slug, kind,"
        " detail) SELECT s.id, s.project, %s, %s, %s, %s, %s FROM specs s WHERE s.id = %s",
        (
            actor.kind,
            actor.session_id,
            actor.slug,
            kind,
            Jsonb(detail) if detail else None,
            spec_id,
        ),
    )


async def notify_spec_changed(conn: psycopg.AsyncConnection, spec_id: int) -> None:
    """The payload is the spec id only and a hint; consumers must also poll."""
    await notify(conn, SPEC_CHANNEL, str(spec_id))


@dataclass(frozen=True)
class LabelEvent:
    id: int
    task_id: int
    labels: list[str]
    actor_session: str | None
    project: str


async def claim_label_event(conn: psycopg.AsyncConnection) -> LabelEvent | None:
    """Lock the oldest unscanned `label_added` event no other notifier holds. The caller keeps
    the transaction open until `mark_scanned`, so a second notifier skips the row."""
    cur = await conn.execute(
        "SELECT e.id, e.task_id, e.detail->'labels', e.actor_session, t.project"
        " FROM events e JOIN tasks t ON t.id = e.task_id"
        " WHERE e.kind = 'label_added' AND e.scanned_at IS NULL"
        " ORDER BY e.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED"
    )
    row = await cur.fetchone()
    return LabelEvent(row[0], row[1], row[2] or [], row[3], row[4]) if row else None


async def mark_scanned(conn: psycopg.AsyncConnection, event_id: int) -> bool:
    """Flip one unscanned event to scanned; False when it already was."""
    cur = await conn.execute(
        "UPDATE events SET scanned_at = now() WHERE id = %s AND scanned_at IS NULL", (event_id,)
    )
    return cur.rowcount == 1


@dataclass(frozen=True)
class ExternalEvent:
    id: int
    task_id: int
    labels: list[str]
    assignee_session: str | None
    actor_session: str | None
    project: str


async def claim_external_events(conn: psycopg.AsyncConnection) -> ExternalEvent | None:
    """Lock the oldest unscanned `external_change` event no other notifier holds, with the task's
    labels, assignee and project. The caller keeps the transaction open until `mark_scanned`."""
    cur = await conn.execute(
        "SELECT e.id, e.task_id, t.labels, t.assignee_session, e.actor_session, t.project"
        " FROM events e JOIN tasks t ON t.id = e.task_id"
        " WHERE e.kind = 'external_change' AND e.scanned_at IS NULL"
        " ORDER BY e.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED"
    )
    row = await cur.fetchone()
    return ExternalEvent(*row) if row else None
