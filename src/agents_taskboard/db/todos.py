"""Todo checklist persistence. Edits log an event and NOTIFY but, like comments, leave the task
version alone and never create a notice."""

from __future__ import annotations

import psycopg

from agents_taskboard.api.actors import Actor
from agents_taskboard.db.events import append_event, notify_changed
from agents_taskboard.db.tasks import NotFound, touch_task


async def replace_todo(
    conn: psycopg.AsyncConnection, actor: Actor, task_id: int, items: list[tuple[str, str]]
) -> None:
    """Replace the whole list with `items` (text, status), positions 0..n-1."""
    async with conn.transaction():
        await touch_task(conn, actor, task_id)
        await conn.execute("DELETE FROM todo_items WHERE task_id = %s", (task_id,))
        if items:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO todo_items (task_id, text, status, position)"
                    " VALUES (%s, %s, %s, %s)",
                    [(task_id, text, status, pos) for pos, (text, status) in enumerate(items)],
                )
        await append_event(conn, task_id, actor, "todo_replaced", {"count": len(items)})
        await notify_changed(conn, task_id)


async def patch_item(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    task_id: int,
    item_id: int,
    changes: dict[str, str],
) -> None:
    """Change text and/or status of one item of the task. Raises NotFound (also for a task
    outside the actor's projects)."""
    async with conn.transaction():
        await touch_task(conn, actor, task_id)
        cur = await conn.execute(
            "UPDATE todo_items SET text = COALESCE(%s, text), status = COALESCE(%s, status)"
            " WHERE id = %s AND task_id = %s RETURNING id",
            (changes.get("text"), changes.get("status"), item_id, task_id),
        )
        if await cur.fetchone() is None:
            raise NotFound
        await append_event(
            conn, task_id, actor, "todo_updated", {"item_id": item_id, "fields": list(changes)}
        )
        await notify_changed(conn, task_id)
