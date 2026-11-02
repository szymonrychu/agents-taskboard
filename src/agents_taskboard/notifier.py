"""The notifier: sends each pending notice to its session as a content-free chat message.

Delivery is at-least-once. The row lock (FOR UPDATE SKIP LOCKED) is held across the send and
mark_sent in one transaction, so a second notifier skips the row; a crash between the send and
the commit re-sends it after restart (a rare duplicate), and a pending row is never lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from agents_common.db import listen, open_pool
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from agents_common.runtime import advisory_singleton, run_service
from psycopg_pool import AsyncConnectionPool

from agents_taskboard.db.events import CHANNEL
from agents_taskboard.db.notices import claim_notice, mark_sent, schedule_retry
from agents_taskboard.db.tasks import task_key, task_url
from agents_taskboard.settings import Settings

log = logging.getLogger(__name__)

LOCK_NAME = "agents-taskboard-notifier"


def notice_text(key: str, public_url: str) -> str:
    """The whole message: a task key and a link, never any task content."""
    return f"Task {key} was updated: {task_url(public_url.rstrip('/'), key)}"


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
) -> None:
    """Drain due notices, then wait for NOTIFY task_changed or the poll interval. A failed send
    only reschedules its own row (exponential, capped), so it never stalls other notices. Runs
    until `stop` is set."""
    wake = asyncio.Event()
    listener = asyncio.create_task(_wake_on_notify(settings.database_url.get_secret_value(), wake))
    try:
        while not stop.is_set():
            wake.clear()
            result = await send_next(
                pool,
                client,
                settings.public_base_url,
                settings.notifier_poll_seconds,
                settings.notifier_backoff_max_seconds,
            )
            if result is None:
                await _sleep(settings.notifier_poll_seconds, wake, stop)
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener


async def run_singleton(
    settings: Settings,
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    stop: asyncio.Event,
) -> None:
    """One notifier cluster-wide: wait for the advisory lock, then run."""
    async with advisory_singleton(pool, LOCK_NAME):
        await run_notifier(settings, pool, client, stop)


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
