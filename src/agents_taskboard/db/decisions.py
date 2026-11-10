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
from agents_taskboard.db.scope import scoped
from agents_taskboard.db.tasks import task_key, task_url, touch_task

USER = Actor(kind="user")
_COLUMNS = (
    "d.id, d.task_id, d.asking_session, d.asking_slug, d.question, d.context, d.options,"
    " d.client_key, d.status, d.answer, d.note, d.created_at, d.answered_at,"
    " d.withdraw_reason, d.superseded_by, d.answered_elsewhere, d.superseded_by_task_id"
)
_FROM = (
    "(SELECT x.*, s.task_id AS superseded_by_task_id FROM decisions x"
    " LEFT JOIN decisions s ON s.id = x.superseded_by) d"
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
    withdraw_reason: str | None
    superseded_by: int | None
    answered_elsewhere: str | None
    superseded_by_task_id: int | None

    def as_json(self, public_url: str) -> dict[str, Any]:
        out = asdict(self)
        out.pop("client_key")
        key = task_key(out.pop("task_id"))
        out["task_key"] = key
        target = out.pop("superseded_by_task_id")
        out["superseded_by_task_key"] = None if target is None else task_key(target)
        out["url"] = task_url(public_url, key)
        return out


async def get_decision(
    conn: psycopg.AsyncConnection, actor: Actor, decision_id: int
) -> Decision | None:
    """The decision, or None when it is absent or its task is outside the actor's projects."""
    scope, scope_params = scoped(actor)
    cur = conn.cursor(row_factory=class_row(Decision))
    await cur.execute(
        f"SELECT {_COLUMNS} FROM {_FROM} JOIN tasks t ON t.id = d.task_id WHERE d.id = %s{scope}",
        (decision_id, *scope_params),
    )
    return await cur.fetchone()


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
    client key) returns the existing decision. Raises NotFound for an absent task or one outside
    the actor's projects."""
    assert actor.session_id is not None
    async with conn.transaction():
        await touch_task(conn, actor, task_id)
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
        decision = await get_decision(conn, actor, decision_id)
        assert decision is not None
        return decision, created


async def withdraw_decision(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    decision_id: int,
    task_id: int,
    reason: str,
    superseded_by: int | None,
    answered_elsewhere: str | None,
) -> Decision | None:
    """Withdraw an open decision of the task asked by the actor's session, recording why and the
    outcome (exactly one of superseded_by and answered_elsewhere); None when no row matched (also
    when the task is outside the actor's projects)."""
    scope, scope_params = scoped(actor)
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE decisions d SET status = 'withdrawn', withdraw_reason = %s,"
            " superseded_by = %s, answered_elsewhere = %s FROM tasks t"
            " WHERE d.id = %s AND d.task_id = %s AND d.status = 'open'"
            f" AND d.asking_session = %s AND t.id = d.task_id{scope}"
            " RETURNING d.task_id",
            (
                reason,
                superseded_by,
                answered_elsewhere,
                decision_id,
                task_id,
                actor.session_id,
                *scope_params,
            ),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        task_id = row[0]
        await touch_task(conn, actor, task_id)
        detail: dict[str, Any] = {"decision_id": decision_id}
        if superseded_by is not None:
            detail["superseded_by"] = superseded_by
        await append_event(conn, task_id, actor, "decision_withdrawn", detail)
        await notify_changed(conn, task_id)
        return await get_decision(conn, actor, decision_id)


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
        await touch_task(conn, USER, task_id)
        await append_event(conn, task_id, USER, "decision_answered", {"decision_id": decision_id})
        await enqueue_decision_notice(conn, task_id, asking_session)
        await notify_changed(conn, task_id)
        return await get_decision(conn, USER, decision_id)
