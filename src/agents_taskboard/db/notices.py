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


@dataclass(frozen=True)
class Notice:
    id: int
    task_id: int
    session_id: str
    reason: str


async def claim_notice(conn: psycopg.AsyncConnection) -> Notice | None:
    """Lock the oldest pending notice no other notifier holds. The caller keeps the transaction
    open across the send and `mark_sent`, so a second notifier skips the row meanwhile."""
    cur = await conn.execute(
        "SELECT id, task_id, session_id, reason FROM notices WHERE status = 'pending'"
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
