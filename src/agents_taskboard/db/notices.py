"""The notice outbox. Rows are only inserted here and flipped pending -> sent by the notifier,
through status-guarded updates."""

from __future__ import annotations

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
