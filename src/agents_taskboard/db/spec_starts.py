"""The spec start queue (S-51): one `spec_starts` row per approval event of a spec.

A row is `new` until the notifier handles it: a live owner is nudged (`handled`, mode `nudged`)
or the row is queued per project (`queued`, a 1-based position) and later started under the
per-project gate (`in_progress`, then `handled` mode `started` or `failed`). Every change is a
status-guarded UPDATE; the queued rows of one project change only under the advisory lock
`spec_queue:<project>`, so positions stay a strict order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from agents_common.db.pool import notify
from agents_common.runs import RunId, RunStatus
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agents_taskboard.api.actors import Actor
from agents_taskboard.db.scope import scoped
from agents_taskboard.db.specs import spec_key

START_CHANNEL = "spec_start"
HOLD_REASONS = (
    "autostart_off",
    "at_limit",
    "usage_high",
    "usage_unknown",
    "usage_refused",
    "orchestrator_unavailable",
)


class NotQueued(Exception):
    """The start is not in the queue (already started, handled, failed or removed)."""


class StartNotFound(Exception):
    pass


class InvalidMove(ValueError):
    pass


@dataclass(frozen=True)
class SpecStart:
    id: int
    spec_id: int
    ordinal: int
    project: str
    status: str
    position: int | None
    mode: str | None
    owner_session: str | None
    slug: str | None
    preset_id: str | None
    session_id: str | None
    message_id: str | None
    error: str | None
    hold_reason: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


_COLUMNS = (
    "id, spec_id, ordinal, project, status, position, mode, owner_session, slug, preset_id,"
    " session_id, message_id, error, hold_reason, attempts, created_at, updated_at"
)
_SELECT = f"SELECT {_COLUMNS} FROM spec_starts"


async def lock_queue(conn: psycopg.AsyncConnection, project: str) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('spec_queue:' || %s))", (project,))


async def _renumber(conn: psycopg.AsyncConnection, project: str) -> None:
    """Rewrite the queued positions of the project densely 1..n in their current order."""
    await conn.execute(
        "UPDATE spec_starts s SET position = r.n, updated_at = now() FROM ("
        " SELECT id, row_number() OVER (ORDER BY position, id) AS n FROM spec_starts"
        " WHERE project = %s AND status = 'queued') r"
        " WHERE s.id = r.id AND s.position IS DISTINCT FROM r.n",
        (project,),
    )


async def _project_of(conn: psycopg.AsyncConnection, start_id: int) -> str | None:
    cur = await conn.execute("SELECT project FROM spec_starts WHERE id = %s", (start_id,))
    row = await cur.fetchone()
    return row[0] if row else None


async def insert_start(
    conn: psycopg.AsyncConnection, spec_id: int, approval_event_id: int, project: str
) -> int | None:
    """Record one start for the approval event: ordinal is the spec's next. None when the event
    already has a row."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('spec_start:' || %s))", (spec_id,))
    cur = await conn.execute(
        "INSERT INTO spec_starts (spec_id, approval_event_id, ordinal, project)"
        " SELECT %s, %s, coalesce(max(ordinal), 0) + 1, %s FROM spec_starts WHERE spec_id = %s"
        " ON CONFLICT DO NOTHING RETURNING id",
        (spec_id, approval_event_id, project, spec_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    await notify(conn, START_CHANNEL)
    return row[0]


async def claim_new(conn: psycopg.AsyncConnection) -> SpecStart | None:
    """Lock the oldest due `new` row no other notifier holds; the caller keeps the transaction."""
    cur = await conn.execute(
        f"{_SELECT} WHERE status = 'new' AND next_attempt_at <= now()"
        " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
    )
    row = await cur.fetchone()
    return SpecStart(*row) if row else None


async def mark_nudged(
    conn: psycopg.AsyncConnection, start_id: int, owner_session: str, message_id: str
) -> bool:
    cur = await conn.execute(
        "UPDATE spec_starts SET status = 'handled', mode = 'nudged', owner_session = %s,"
        " message_id = %s, hold_reason = NULL, finished_at = now(), updated_at = now()"
        " WHERE id = %s AND status = 'new'",
        (owner_session, message_id, start_id),
    )
    return cur.rowcount == 1


async def enqueue(
    conn: psycopg.AsyncConnection, start_id: int, owner_session: str | None
) -> int | None:
    """Append a `new` row to the end of its project's queue; returns the position."""
    project = await _project_of(conn, start_id)
    if project is None:
        return None
    await lock_queue(conn, project)
    cur = await conn.execute(
        "UPDATE spec_starts SET status = 'queued', owner_session = %s, updated_at = now(),"
        " position = (SELECT coalesce(max(position), 0) + 1 FROM spec_starts"
        "             WHERE project = %s AND status = 'queued')"
        " WHERE id = %s AND status = 'new' RETURNING position",
        (owner_session, project, start_id),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def retry_later(
    conn: psycopg.AsyncConnection,
    start_id: int,
    status: str,
    backoff_base: float,
    backoff_max: float,
) -> bool:
    """Count one failed attempt of a row in `status` and push its next attempt out."""
    cur = await conn.execute(
        "UPDATE spec_starts SET attempts = attempts + 1, updated_at = now(),"
        " next_attempt_at = now() + make_interval(secs => least(%s::float8 * power(2, attempts),"
        " %s::float8)) WHERE id = %s AND status = %s",
        (backoff_base, backoff_max, start_id, status),
    )
    return cur.rowcount == 1


async def queued_projects(conn: psycopg.AsyncConnection) -> list[str]:
    cur = await conn.execute(
        "SELECT DISTINCT project FROM spec_starts WHERE status = 'queued' ORDER BY project"
    )
    return [row[0] for row in await cur.fetchall()]


async def queue_head(conn: psycopg.AsyncConnection, project: str) -> SpecStart | None:
    cur = await conn.execute(
        f"{_SELECT} WHERE project = %s AND status = 'queued' ORDER BY position LIMIT 1",
        (project,),
    )
    row = await cur.fetchone()
    return SpecStart(*row) if row else None


async def set_hold(conn: psycopg.AsyncConnection, start_id: int, reason: str | None) -> bool:
    """Record why a queued row waits; False when it is not queued or the reason is unchanged."""
    cur = await conn.execute(
        "UPDATE spec_starts SET hold_reason = %s, updated_at = now()"
        " WHERE id = %s AND status = 'queued' AND hold_reason IS DISTINCT FROM %s",
        (reason, start_id, reason),
    )
    return cur.rowcount == 1


async def begin_start(
    conn: psycopg.AsyncConnection, start_id: int, slug: str, preset_id: str
) -> bool:
    """Take the queued row out of the queue (it can no longer be moved or removed)."""
    cur = await conn.execute(
        "UPDATE spec_starts SET status = 'in_progress', position = NULL, hold_reason = NULL,"
        " slug = %s, preset_id = %s, updated_at = now() WHERE id = %s AND status = 'queued'",
        (slug, preset_id, start_id),
    )
    return cur.rowcount == 1


async def requeue_head(
    conn: psycopg.AsyncConnection, start_id: int, reason: str, next_attempt_at: datetime
) -> bool:
    """Put an in-progress row back at the head of its queue (it keeps slug and session ids) and
    renumber the project's queue densely 1..n."""
    project = await _project_of(conn, start_id)
    if project is None:
        return False
    await lock_queue(conn, project)
    cur = await conn.execute(
        "UPDATE spec_starts SET status = 'queued', position = 0, hold_reason = %s,"
        " next_attempt_at = %s, updated_at = now() WHERE id = %s AND status = 'in_progress'",
        (reason, next_attempt_at, start_id),
    )
    if cur.rowcount != 1:
        return False
    await _renumber(conn, project)
    return True


async def in_progress(conn: psycopg.AsyncConnection) -> list[SpecStart]:
    cur = await conn.execute(f"{_SELECT} WHERE status = 'in_progress' ORDER BY id")
    return [SpecStart(*row) for row in await cur.fetchall()]


class SpecStartStore:
    """`agents_common.runs.RunStore` over the pool: the run id is the spec_starts id and every
    write is guarded on `in_progress`."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _update(self, sql: str, params: tuple[Any, ...]) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount == 1

    async def set_session(self, run_id: RunId, session_id: str) -> None:
        await self._update(
            "UPDATE spec_starts SET session_id = %s, updated_at = now()"
            " WHERE id = %s AND status = 'in_progress'",
            (session_id, int(run_id)),
        )

    async def set_message(self, run_id: RunId, message_id: str) -> None:
        await self._update(
            "UPDATE spec_starts SET message_id = %s, updated_at = now()"
            " WHERE id = %s AND status = 'in_progress'",
            (message_id, int(run_id)),
        )

    async def finish(self, run_id: RunId, status: RunStatus, reason: str | None) -> bool:
        if status == "handled":
            return await self._update(
                "UPDATE spec_starts SET status = 'handled', mode = 'started', hold_reason = NULL,"
                " finished_at = now(), updated_at = now()"
                " WHERE id = %s AND status = 'in_progress'",
                (int(run_id),),
            )
        return await self._update(
            "UPDATE spec_starts SET status = 'failed', error = %s, hold_reason = NULL,"
            " finished_at = now(), updated_at = now() WHERE id = %s AND status = 'in_progress'",
            (reason, int(run_id)),
        )


async def mark_failed(
    conn: psycopg.AsyncConnection, start_id: int, from_status: str, error: str
) -> bool:
    cur = await conn.execute(
        "UPDATE spec_starts SET status = 'failed', position = NULL, error = %s,"
        " hold_reason = NULL, finished_at = now(), updated_at = now()"
        " WHERE id = %s AND status = %s",
        (error, start_id, from_status),
    )
    return cur.rowcount == 1


async def list_queue(
    conn: psycopg.AsyncConnection, actor: Actor, project: str | None
) -> list[dict[str, Any]]:
    """Queued starts the actor may see, optionally of one project, in queue order. `rank` is the
    displayed 1..n place among the project's queued rows (positions may have gaps)."""
    scope, params = scoped(actor, "q.project")
    where = " AND q.project = %s" if project is not None else ""
    if project is not None:
        params = [*params, project]
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT q.id, q.spec_id, q.ordinal, q.project, q.position, q.hold_reason,"
            " q.owner_session, q.attempts, q.created_at, q.updated_at, s.title,"
            " row_number() OVER (PARTITION BY q.project ORDER BY q.position, q.id) AS rank"
            " FROM spec_starts q JOIN specs s ON s.id = q.spec_id"
            f" WHERE q.status = 'queued'{scope}{where} ORDER BY q.project, q.position",
            params,
        )
        rows = await cur.fetchall()
    return [{**r, "spec": spec_key(r["spec_id"])} for r in rows]


async def get_scoped(
    conn: psycopg.AsyncConnection, actor: Actor, start_id: int
) -> tuple[int, str] | None:
    """(spec id, project) of the start, or None when absent or outside the actor's projects."""
    scope, params = scoped(actor, "project")
    cur = await conn.execute(
        f"SELECT spec_id, project FROM spec_starts WHERE id = %s{scope}", (start_id, *params)
    )
    row = await cur.fetchone()
    return (row[0], row[1]) if row else None


async def move(
    conn: psycopg.AsyncConnection,
    start_id: int,
    *,
    position: int | None,
    before: int | None,
) -> str:
    """Move a queued start to `position` (1-based, clamped) or just before the queued start
    `before` of the same project, rewriting the project's positions densely; returns the
    project."""
    if (position is None) == (before is None):
        raise InvalidMove("give exactly one of position and before")
    if before == start_id:
        raise InvalidMove("before must name another start")
    project = await _project_of(conn, start_id)
    if project is None:
        raise StartNotFound(start_id)
    await lock_queue(conn, project)
    cur = await conn.execute(
        "SELECT id FROM spec_starts WHERE project = %s AND status = 'queued'"
        " ORDER BY position, id FOR UPDATE",
        (project,),
    )
    ids = [row[0] for row in await cur.fetchall()]
    if start_id not in ids:
        raise NotQueued(start_id)
    ids.remove(start_id)
    if position is not None:
        index = min(max(position, 1), len(ids) + 1) - 1
    elif before in ids:
        index = ids.index(before)
    else:
        raise InvalidMove("before is not a queued start of the same project")
    ids.insert(index, start_id)
    await conn.execute(
        "UPDATE spec_starts s SET position = v.n, updated_at = now()"
        " FROM unnest(%s::bigint[], %s::int[]) AS v(id, n)"
        " WHERE s.id = v.id AND s.position IS DISTINCT FROM v.n",
        (ids, list(range(1, len(ids) + 1))),
    )
    return project


async def remove(conn: psycopg.AsyncConnection, start_id: int) -> int:
    """Take a queued start out of the queue for good; returns its spec id."""
    project = await _project_of(conn, start_id)
    if project is None:
        raise StartNotFound(start_id)
    await lock_queue(conn, project)
    cur = await conn.execute(
        "UPDATE spec_starts SET status = 'removed', position = NULL, hold_reason = NULL,"
        " finished_at = now(), updated_at = now() WHERE id = %s AND status = 'queued'"
        " RETURNING spec_id",
        (start_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotQueued(start_id)
    return row[0]


async def starts_of_spec(
    conn: psycopg.AsyncConnection, spec_id: int, limit: int = 10
) -> list[dict[str, Any]]:
    """The newest starts of the spec, newest first."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS}, finished_at FROM spec_starts WHERE spec_id = %s"
            " ORDER BY id DESC LIMIT %s",
            (spec_id, limit),
        )
        return list(await cur.fetchall())
