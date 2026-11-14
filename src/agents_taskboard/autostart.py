"""Spec autostart (S-51): what the notifier does with a `new` spec start.

A live owner session is nudged with a fixed message (never gated by limits or usage, Q10); with no
owner, or an owner that is gone or being deleted, the start joins its project's queue.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from agents_common.orchestrator import (
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)
from agents_common.runs import RunId, RunRef, RunStatus, deliver, marker, running_session
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agents_taskboard.api.actors import Actor
from agents_taskboard.db import spec_starts
from agents_taskboard.db.events import append_spec_event, notify_spec_changed
from agents_taskboard.db.specs import spec_key, spec_owner

log = logging.getLogger(__name__)

AUTOSTART_ACTOR = Actor(kind="agent", slug="taskboard-autostart")
MARKER_KIND = "taskboard-spec-start"


def nudge_text(spec_key: str, public_url: str) -> str:
    """The whole nudge: a spec key and a link, never any spec content."""
    return (
        f"Spec {spec_key} approved - take it into implementation now: {public_url}/specs/{spec_key}"
    )


class _MessageStore:
    """A RunStore that only remembers the posted message id; the caller writes the row."""

    def __init__(self) -> None:
        self.message_id: str | None = None

    async def set_session(self, run_id: RunId, session_id: str) -> None:
        pass

    async def set_message(self, run_id: RunId, message_id: str) -> None:
        self.message_id = message_id

    async def finish(self, run_id: RunId, status: RunStatus, reason: str | None) -> bool:
        return True


async def handle_new(
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    public_url: str,
    backoff_base: float,
    backoff_max: float,
) -> bool | None:
    """Handle one due `new` start. None: nothing due; True: the row moved on (nudged, queued or
    failed); False: the orchestrator failed, the row stays `new` on its own retry schedule."""
    failed = None
    async with pool.connection() as conn:
        start = await spec_starts.claim_new(conn)
        if start is None:
            return None
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT assignee_session, created_by_session FROM specs"
            " WHERE id = %s AND deleted_at IS NULL",
            (start.spec_id,),
        )
        spec = await cur.fetchone()
        if spec is None:
            await spec_starts.mark_failed(conn, start.id, "new", "spec_deleted")
            return True
        owner = await spec_owner(
            conn, start.spec_id, spec["assignee_session"], spec["created_by_session"]
        )
        try:
            nudged = None
            if owner is not None and await running_session(client, owner) is not None:
                store = _MessageStore()
                run = RunRef(start.id, "", "", None, None, datetime.now(UTC))
                text = nudge_text(spec_key(start.spec_id), public_url.rstrip("/"))
                nudged = await deliver(
                    client, store, run, owner, text, marker(MARKER_KIND, start.id)
                )
        except (OrchestratorError, OrchestratorUnavailable) as exc:
            log.warning("spec start %s: nudge failed, will retry: %s", start.id, exc)
            failed = start.id
        else:
            if nudged is not None and nudged.message_id is not None:
                if await spec_starts.mark_nudged(conn, start.id, owner, nudged.message_id):
                    detail = {"start_id": start.id, "mode": "nudged", "session_id": owner}
                    await append_spec_event(
                        conn, start.spec_id, AUTOSTART_ACTOR, "implementation_triggered", detail
                    )
            else:
                position = await spec_starts.enqueue(conn, start.id, owner)
                detail = {"start_id": start.id, "project": start.project, "position": position}
                await append_spec_event(
                    conn, start.spec_id, AUTOSTART_ACTOR, "implementation_queued", detail
                )
            await notify_spec_changed(conn, start.spec_id)
            return True
    # the claim transaction wrote nothing; count the failed attempt in a separate one
    async with pool.connection() as conn:
        await spec_starts.retry_later(conn, failed, "new", backoff_base, backoff_max)
    return False
