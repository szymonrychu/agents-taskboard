"""The notice outbox. Rows are only inserted here and flipped pending -> sent by the notifier,
through status-guarded updates."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg


async def enqueue_decision_notice(
    conn: psycopg.AsyncConnection, task_id: int, session_id: str
) -> None:
    """Queue a regular-lane notice for the asking session; a notice already pending for the
    same task and session absorbs this one."""
    await conn.execute(
        "INSERT INTO notices (task_id, session_id, reason, status)"
        " VALUES (%s, %s, 'decision', 'pending') ON CONFLICT DO NOTHING",
        (task_id, session_id),
    )


async def enqueue_label_notice(
    conn: psycopg.AsyncConnection, task_id: int, session_id: str
) -> None:
    """Queue a regular-lane notice for a session sharing a label with the task. The unique
    index on (task, session) for reason 'label' makes it at most once, ever."""
    await conn.execute(
        "INSERT INTO notices (task_id, session_id, reason, status)"
        " VALUES (%s, %s, 'label', 'pending') ON CONFLICT DO NOTHING",
        (task_id, session_id),
    )


async def enqueue_linked_notice(
    conn: psycopg.AsyncConnection, task_id: int, session_id: str
) -> None:
    """Queue a regular-lane notice that something linked to the task changed outside the
    taskboard; a notice already pending for the same task and session absorbs this one."""
    await conn.execute(
        "INSERT INTO notices (task_id, session_id, reason, status)"
        " VALUES (%s, %s, 'linked', 'pending') ON CONFLICT DO NOTHING",
        (task_id, session_id),
    )


async def enqueue_spec_notice(conn: psycopg.AsyncConnection, spec_id: int, session_id: str) -> None:
    """Queue a regular-lane notice that a spec changed by the user; a notice already pending
    for the same spec and session absorbs this one."""
    await conn.execute(
        "INSERT INTO notices (spec_id, session_id, reason, status)"
        " VALUES (%s, %s, 'spec', 'pending') ON CONFLICT DO NOTHING",
        (spec_id, session_id),
    )


@dataclass(frozen=True)
class Notice:
    id: int
    task_id: int | None
    spec_id: int | None
    session_id: str
    reason: str


async def claim_notice(conn: psycopg.AsyncConnection) -> Notice | None:
    """Lock the oldest due pending notice no other notifier holds. The caller keeps the
    transaction open across the send and `mark_sent`, so a second notifier skips the row."""
    cur = await conn.execute(
        "SELECT id, task_id, spec_id, session_id, reason FROM notices"
        " WHERE status = 'pending' AND next_attempt_at <= now()"
        " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
    )
    row = await cur.fetchone()
    return Notice(*row) if row else None


async def mark_sent(conn: psycopg.AsyncConnection, notice_id: int) -> bool:
    """Flip one pending notice to sent; False when it was not pending."""
    cur = await conn.execute(
        "UPDATE notices SET status = 'sent', sent_at = now() WHERE id = %s AND status = 'pending'",
        (notice_id,),
    )
    return cur.rowcount == 1


async def schedule_retry(
    conn: psycopg.AsyncConnection, notice_id: int, base_seconds: float, max_seconds: float
) -> None:
    """After a failed send: count the attempt and push the next one out by base * 2^attempts
    seconds, capped at max_seconds. Only a still-pending row changes."""
    await conn.execute(
        "UPDATE notices SET attempts = attempts + 1,"
        " next_attempt_at = now() + make_interval(secs =>"
        "   LEAST(%s::float8 * power(2, LEAST(attempts, 30)), %s::float8))"
        " WHERE id = %s AND status = 'pending'",
        (base_seconds, max_seconds, notice_id),
    )
