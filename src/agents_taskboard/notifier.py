"""The notifier: sends each pending notice to its session as a content-free chat message.

Delivery is at-least-once. The row lock (FOR UPDATE SKIP LOCKED) is held across the send and
mark_sent in one transaction, so a second notifier skips the row; a crash between the send and
the commit re-sends it after restart (a rare duplicate), and a pending row is never lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx
import psycopg
from agents_common.db import listen, open_pool
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from agents_common.runtime import advisory_singleton, run_service
from psycopg_pool import AsyncConnectionPool

from agents_taskboard.db.events import CHANNEL, claim_label_event, mark_scanned
from agents_taskboard.db.notices import (
    claim_notice,
    enqueue_label_notice,
    mark_sent,
    schedule_retry,
)
from agents_taskboard.db.tasks import task_key, task_url
from agents_taskboard.settings import Settings

log = logging.getLogger(__name__)

LOCK_NAME = "agents-taskboard-notifier"


def notice_text(key: str, public_url: str) -> str:
    """The whole message: a task key and a link, never any task content."""
    return f"Task {key} was updated: {task_url(public_url.rstrip('/'), key)}"


class SessionLookup:
    """Live orchestrator sessions by label: `GET /api/v1/sessions?label=..` (repeatable, any
    match) on the service port. agents-common's client has no such call and stays untouched.
    The response is a plain list (no paging)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def live_session_ids(self, labels: list[str]) -> list[str]:
        """Ids of live sessions carrying any of `labels`; OrchestratorUnavailable on any
        failure, so the caller retries."""
        path = "/api/v1/sessions"
        try:
            resp = await self._http.get(path, params=[("label", x) for x in labels])
        except httpx.HTTPError as exc:
            raise OrchestratorUnavailable(f"GET {path}: {exc!r}") from exc
        if resp.status_code != 200:
            raise OrchestratorUnavailable(f"GET {path}: HTTP {resp.status_code}")
        wanted = set(labels)
        try:
            return [
                str(s["id"])
                for s in resp.json()
                if s.get("desired", "active") == "active" and wanted & set(s.get("labels") or [])
            ]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise OrchestratorUnavailable(f"GET {path}: malformed response: {exc!r}") from exc


async def scan_label_events(conn: psycopg.AsyncConnection, lookup: SessionLookup) -> bool | None:
    """Scan one unscanned `label_added` event: queue a label notice for every live session
    sharing an added label, except the session that made the change. None: nothing to scan.
    A failed lookup raises and, with the caller's transaction rolled back, leaves the event
    unscanned (no loss; the unique index makes a re-scan duplicate-free)."""
    event = await claim_label_event(conn)
    if event is None:
        return None
    for session_id in await lookup.live_session_ids(event.labels):
        if session_id != event.actor_session:
            await enqueue_label_notice(conn, event.task_id, session_id)
    await mark_scanned(conn, event.id)
    return True


async def send_next(
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    public_url: str,
    backoff_base: float,
    backoff_max: float,
) -> bool | None:
    """Send one due pending notice. None: nothing due; True: ended (sent, or the session is
    gone); False: the send failed, the row stays pending on its own retry schedule so later
    notices are not held up."""
    async with pool.connection() as conn:
        notice = await claim_notice(conn)
        if notice is None:
            return None
        text = notice_text(task_key(notice.task_id), public_url)
        failed = None
        try:
            await client.post_message(notice.session_id, text)
        except NotFound:
            log.info("notice %s: session %s is gone; ended", notice.id, notice.session_id)
        except (OrchestratorError, OrchestratorUnavailable) as exc:
            log.warning("notice %s send failed, will retry: %s", notice.id, exc)
            failed = notice.id
        if failed is None:
            await mark_sent(conn, notice.id)
            return True
    # the send transaction ended without changing the row; reschedule it in a separate one
    async with pool.connection() as conn:
        await schedule_retry(conn, failed, backoff_base, backoff_max)
    return False


async def _wake_on_notify(url: str, wake: asyncio.Event) -> None:
    async for _ in listen(url, CHANNEL):
        wake.set()


async def _sleep(seconds: float, wake: asyncio.Event, stop: asyncio.Event) -> None:
    waits = [asyncio.create_task(e.wait()) for e in (wake, stop)]
    try:
        await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


async def run_notifier(
    settings: Settings,
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    stop: asyncio.Event,
    lookup: SessionLookup | None = None,
) -> None:
    """Scan label events, then drain due notices, then wait for NOTIFY task_changed or the poll
    interval. A failed send only reschedules its own row (exponential, capped), so it never
    stalls other notices; a failed label lookup leaves its event unscanned and pauses only the
    scanning (same backoff), never the sending. Runs until `stop` is set."""
    own = lookup is None
    if lookup is None:
        lookup = SessionLookup(
            settings.orchestrator_url, settings.orchestrator_token.get_secret_value()
        )
    wake = asyncio.Event()
    scan_failures = 0
    scan_resume = 0.0
    loop = asyncio.get_running_loop()
    listener = asyncio.create_task(_wake_on_notify(settings.database_url.get_secret_value(), wake))
    try:
        while not stop.is_set():
            wake.clear()
            scanned = None
            if loop.time() >= scan_resume:
                try:
                    async with pool.connection() as conn:
                        scanned = await scan_label_events(conn, lookup)
                    scan_failures = 0
                except (OrchestratorError, OrchestratorUnavailable) as exc:
                    delay = min(
                        settings.notifier_poll_seconds * 2**scan_failures,
                        settings.notifier_backoff_max_seconds,
                    )
                    scan_failures = min(scan_failures + 1, 30)
                    scan_resume = loop.time() + delay
                    log.warning("label scan failed, retrying in %.1fs: %s", delay, exc)
            result = await send_next(
                pool,
                client,
                settings.public_base_url,
                settings.notifier_poll_seconds,
                settings.notifier_backoff_max_seconds,
            )
            if result is None and scanned is None:
                wait = settings.notifier_poll_seconds
                if scan_resume > loop.time():
                    wait = min(wait, scan_resume - loop.time())
                await _sleep(wait, wake, stop)
    finally:
        if own:
            await lookup.aclose()
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener


async def run_singleton(
    settings: Settings,
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    stop: asyncio.Event,
    lookup: SessionLookup | None = None,
) -> None:
    """One notifier cluster-wide: wait for the advisory lock, then run."""
    async with advisory_singleton(pool, LOCK_NAME):
        await run_notifier(settings, pool, client, stop, lookup)


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    """The `taskboard notifier` process body."""
    pool = await open_pool(settings.database_url.get_secret_value(), min_size=2, max_size=4)
    client = OrchestratorClient(
        settings.orchestrator_url, settings.orchestrator_token.get_secret_value()
    )
    try:
        await run_singleton(settings, pool, client, stop)
    finally:
        await client.aclose()
        await pool.close()


def main(settings: Settings) -> int:
    """Run the notifier until SIGTERM or SIGINT; the exit code."""
    logging.basicConfig(level=settings.log_level.upper(), force=True)
    return asyncio.run(
        run_service(
            lambda stop: serve(settings, stop), grace_seconds=settings.shutdown_grace_seconds
        )
    )
