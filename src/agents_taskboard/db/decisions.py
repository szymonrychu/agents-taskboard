"""Decisions: an agent asks, a user answers once, the asker may withdraw while open.

Answer and withdraw are single status-guarded UPDATEs; the loser of a race matches zero rows.
Like comments they touch `updated_at` but never bump the task version. Closing a task does not
withdraw its open decisions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from agents_taskboard.api.actors import Actor
from agents_taskboard.db.events import append_event, notify_changed
from agents_taskboard.db.notices import enqueue_decision_notice
from agents_taskboard.db.tasks import NotFound

USER = Actor(kind="user")
_COLUMNS = (
    "id, task_id, asking_session, asking_slug, question, context, options, client_key,"
    " status, answer, note, created_at, answered_at"
)


@dataclass(frozen=True)
class Decision:
    id: int
    task_id: int
    asking_session: str
    asking_slug: str | None
    question: str
    context: str
    options: list[dict[str, Any]]
    client_key: str | None
    status: str
    answer: Any
    note: str | None
    created_at: datetime
    answered_at: datetime | None

    def as_json(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("client_key")
        out["task_key"] = f"T-{out.pop('task_id')}"
        return out


async def get_decision(conn: psycopg.AsyncConnection, decision_id: int) -> Decision | None:
    cur = conn.cursor(row_factory=class_row(Decision))
    await cur.execute(f"SELECT {_COLUMNS} FROM decisions WHERE id = %s", (decision_id,))
    return await cur.fetchone()


async def _touch(conn: psycopg.AsyncConnection, task_id: int) -> None:
    cur = await conn.execute(
        "UPDATE tasks SET updated_at = now() WHERE id = %s RETURNING id", (task_id,)
    )
    if await cur.fetchone() is None:
        raise NotFound


async def ask_decision(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    task_id: int,
    question: str,
    context: str,
    options: list[dict[str, Any]],
    client_key: str | None,
) -> tuple[Decision, bool]:
    """Insert an open decision; returns (decision, created). A replayed (task, asking session,
    client key) returns the existing decision. Raises NotFound for an absent task."""
    assert actor.session_id is not None
    async with conn.transaction():
        await _touch(conn, task_id)
        cur = await conn.execute(
            "INSERT INTO decisions (task_id, asking_session, asking_slug, question, context,"
            " options, client_key, status) VALUES (%s, %s, %s, %s, %s, %s, %s, 'open')"
            " ON CONFLICT DO NOTHING RETURNING id",
            (
                task_id,
                actor.session_id,
                actor.slug,
                question,
                context,
                Jsonb(options),
                client_key,
            ),
        )
        row = await cur.fetchone()
        created = row is not None
        if row is None:
            cur = await conn.execute(
                "SELECT id FROM decisions"
                " WHERE task_id = %s AND asking_session = %s AND client_key = %s",
                (task_id, actor.session_id, client_key),
            )
            row = await cur.fetchone()
            assert row is not None
        decision_id = row[0]
        if created:
            await append_event(conn, task_id, actor, "decision_asked", {"decision_id": decision_id})
            await notify_changed(conn, task_id)
        decision = await get_decision(conn, decision_id)
        assert decision is not None
        return decision, created


async def withdraw_decision(
    conn: psycopg.AsyncConnection, actor: Actor, decision_id: int, task_id: int
) -> Decision | None:
    """Withdraw an open decision of the task asked by the actor's session; None when no row
    matched."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE decisions SET status = 'withdrawn'"
            " WHERE id = %s AND task_id = %s AND status = 'open' AND asking_session = %s"
            " RETURNING task_id",
            (decision_id, task_id, actor.session_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        task_id = row[0]
        await _touch(conn, task_id)
        await append_event(conn, task_id, actor, "decision_withdrawn", {"decision_id": decision_id})
        await notify_changed(conn, task_id)
        return await get_decision(conn, decision_id)


async def answer_decision(
    conn: psycopg.AsyncConnection, decision_id: int, answer: Any, note: str | None
) -> Decision | None:
    """Answer an open decision as a user, once. None when it was already answered or withdrawn.
    Event, notice for the asking session and NOTIFY share the transaction."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE decisions SET status = 'answered', answer = %s, note = %s, answered_at = now()"
            " WHERE id = %s AND status = 'open' RETURNING asking_session, task_id",
            (Jsonb(answer), note, decision_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        asking_session, task_id = row
        await _touch(conn, task_id)
        await append_event(conn, task_id, USER, "decision_answered", {"decision_id": decision_id})
        await enqueue_decision_notice(conn, task_id, asking_session)
        await notify_changed(conn, task_id)
        return await get_decision(conn, decision_id)
