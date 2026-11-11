"""The task_changed SSE stream (web listener only).

One shared LISTEN connection per process fans task ids out to the in-process subscribers. A
NOTIFY is only a hint, so the hub also polls the events table every few seconds with an id
cursor (every mutation appends an event) and reports tasks whose events it has not already
heard about; a missed NOTIFY, or a down listener, delays an update by at most one poll.

The hub starts lazily with the first subscriber, stops when the last one leaves and on app
shutdown. FastAPI's SSE response sends the 15 s keepalive comment while the stream is idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable

from agents_common.db import listen
from fastapi import APIRouter, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from psycopg_pool import AsyncConnectionPool

from agents_taskboard.db.events import CHANNEL
from agents_taskboard.db.tasks import task_key

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0
BATCH = 1000
LISTEN_RETRY_SECONDS = 0.5
LISTEN_RETRY_MAX_SECONDS = 30.0

ListenFn = Callable[[str, str], AsyncIterator[str]]


class Subscription:
    """The (task id, project) pairs changed since the subscriber last looked; a dict, so a
    slow client coalesces instead of growing a queue."""

    def __init__(self, hub: TaskChangeHub) -> None:
        self._hub = hub
        self._ids: dict[int, str] = {}
        self._wake = asyncio.Event()

    def offer(self, task_id: int, project: str) -> None:
        self._ids[task_id] = project
        self._wake.set()

    async def get(self) -> list[tuple[int, str]]:
        await self._wake.wait()
        self._wake.clear()
        ids, self._ids = sorted(self._ids.items()), {}
        return ids

    def close(self) -> None:
        self._hub.unsubscribe(self)


class TaskChangeHub:
    def __init__(
        self,
        dsn: str,
        pool: AsyncConnectionPool,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        listen_fn: ListenFn = listen,
    ) -> None:
        self._dsn = dsn
        self._pool = pool
        self._poll_seconds = poll_seconds
        self._listen = listen_fn
        self._subs: set[Subscription] = set()
        self._tasks: list[asyncio.Task] = []
        self._heard: set[int] = set()

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    def subscriber_count(self) -> int:
        return len(self._subs)

    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        self._subs.add(sub)
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._listen_loop(), name="taskboard-listen"),
                asyncio.create_task(self._poll_loop(), name="taskboard-poll"),
            ]
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)
        if not self._subs:
            self._cancel()

    def _cancel(self) -> list[asyncio.Task]:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        return tasks

    async def stop(self) -> None:
        self._subs.clear()
        for task in self._cancel():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _publish(self, task_id: int, project: str) -> None:
        for sub in self._subs:
            sub.offer(task_id, project)

    async def _project_of(self, task_id: int) -> str | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT project FROM tasks WHERE id = %s", (task_id,))
            row = await cur.fetchone()
        return row[0] if row else None

    async def _listen_loop(self) -> None:
        """Run the shared LISTEN; any unexpected error restarts it after a capped backoff
        (agents-common reconnects only on connection errors)."""
        delay = LISTEN_RETRY_SECONDS
        while True:
            try:
                async for payload in self._listen(self._dsn, CHANNEL):
                    delay = LISTEN_RETRY_SECONDS
                    try:
                        task_id = int(payload)
                    except ValueError:
                        continue
                    self._heard.add(task_id)
                    project = await self._project_of(task_id)
                    if project is not None:
                        self._publish(task_id, project)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("task_changed listener failed; restarting in %.1fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, LISTEN_RETRY_MAX_SECONDS)

    async def _max_event_id(self) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT coalesce(max(id), 0) FROM events")
            row = await cur.fetchone()
        return int(row[0])

    async def _poll_loop(self) -> None:
        cursor = None
        while True:
            try:
                if cursor is None:
                    cursor = await self._max_event_id()
                else:
                    cursor = await self._poll_once(cursor)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("task_changed poll failed")
            await asyncio.sleep(self._poll_seconds)

    async def _poll_once(self, cursor: int) -> int:
        """Publish tasks with events past the cursor that no NOTIFY announced; return the
        new cursor."""
        heard, self._heard = self._heard, set()
        while True:
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT e.id, e.task_id, coalesce(e.project, 'default') FROM events e"
                    " WHERE e.id > %s AND e.task_id IS NOT NULL ORDER BY e.id LIMIT %s",
                    (cursor, BATCH),
                )
                rows = await cur.fetchall()
            for event_id, task_id, project in rows:
                cursor = event_id
                if task_id not in heard:
                    heard.add(task_id)
                    self._publish(task_id, project)
            if len(rows) < BATCH:
                return cursor


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/stream", response_class=EventSourceResponse)
    async def stream(
        request: Request, project: str | None = None
    ) -> AsyncIterator[ServerSentEvent]:
        sub = request.app.state.hub.subscribe()
        try:
            while True:
                for task_id, task_project in await sub.get():
                    if project is None or project == task_project:
                        yield ServerSentEvent(
                            data={"key": task_key(task_id), "project": task_project},
                            event="task_changed",
                        )
        finally:
            sub.close()

    return router
