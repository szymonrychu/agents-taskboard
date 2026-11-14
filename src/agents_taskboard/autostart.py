"""Spec autostart (S-51): what the notifier does with spec starts.

`handle_new`: a live owner session is nudged with a fixed message (never gated by limits or
usage, Q10); with no owner, or an owner that is gone or being deleted, the start joins its
project's queue. `run_queues`: the head of each project's queue starts an implementation session
when the project is configured, under its active-session limit and its usage ceiling (Q11-Q14);
queued rows never expire (Q9).
"""

from __future__ import annotations

import dataclasses
import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Protocol

from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
    UsageQuotaRefused,
)
from agents_common.runs import (
    RunId,
    RunRef,
    RunStatus,
    deliver,
    dispatch,
    marker,
    running_session,
)
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agents_taskboard.api.actors import Actor
from agents_taskboard.db import spec_starts
from agents_taskboard.db.events import append_spec_event, notify_spec_changed
from agents_taskboard.db.project_settings import AutostartSettings, settings_for
from agents_taskboard.db.spec_starts import SpecStart, SpecStartStore
from agents_taskboard.db.specs import spec_key, spec_owner
from agents_taskboard.db.steps import gate_task_id
from agents_taskboard.db.tasks import add_comment, task_key
from agents_taskboard.presets import PresetLookup
from agents_taskboard.usage import UsageReading, usage_gate

log = logging.getLogger(__name__)

AUTOSTART_ACTOR = Actor(kind="agent", slug="taskboard-autostart")
MARKER_KIND = "taskboard-spec-start"


def nudge_text(spec_key: str, public_url: str) -> str:
    """The whole nudge: a spec key and a link, never any spec content."""
    return (
        f"Spec {spec_key} approved - take it into implementation now: {public_url}/specs/{spec_key}"
    )


def spec_url(public_url: str, key: str) -> str:
    return f"{public_url.rstrip('/')}/specs/{key}"


def start_slug(spec_id: int, ordinal: int) -> str:
    """The deterministic slug of a start's session (a 409 on create adopts it)."""
    return f"spec-s-{spec_id}-impl-{ordinal}"


def start_prompt(spec_key: str, spec_url: str, gate_task_key: str | None) -> str:
    """The whole start prompt: keys, a link and fixed workflow text, never any spec content."""
    return (
        f"Spec {spec_key} was approved and is yours to implement: {spec_url}."
        f" Gate task: {gate_task_key or 'none'}. Follow the workbench superpowers flow without"
        " asking again: claim the gate task, pull the spec, run superpowers:writing-plans from the"
        " approved spec (plan header and steps on the spec), then"
        " superpowers:subagent-driven-development with reviews, release and deploy, the final"
        " review with retro.md, archive the spec, close the task, then self-close."
    )


class UsageSource(Protocol):
    """What the gate reads: `UsageClient` (None when agents-usage has no usable answer)."""

    async def current(self) -> UsageReading | None: ...


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
            "SELECT assignee_session, created_by_session, status FROM specs"
            " WHERE id = %s AND deleted_at IS NULL",
            (start.spec_id,),
        )
        spec = await cur.fetchone()
        if spec is None:
            await spec_starts.mark_failed(conn, start.id, "new", "spec_deleted")
            return True
        if spec["status"] != "approved":
            await _drop_unapproved(conn, start, "new")
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


async def _trigger_failed(conn, start: SpecStart, error: str) -> None:
    detail = {"start_id": start.id, "error": error}
    await append_spec_event(
        conn, start.spec_id, AUTOSTART_ACTOR, "implementation_trigger_failed", detail
    )
    await notify_spec_changed(conn, start.spec_id)


async def _drop_unapproved(conn, start: SpecStart, from_status: str) -> bool:
    """Remove a start whose spec is not approved any more, with a spec event."""
    if not await spec_starts.drop_unapproved(conn, start.id, from_status):
        return False
    detail = {"start_id": start.id, "reason": "spec_not_approved"}
    await append_spec_event(
        conn, start.spec_id, AUTOSTART_ACTOR, "implementation_trigger_removed", detail
    )
    await notify_spec_changed(conn, start.spec_id)
    return True


async def _dispatch_start(
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    start: SpecStart,
    public_url: str,
    backoff_base: float,
    backoff_max: float,
) -> bool:
    """Create the in-progress start's session and post its prompt (each at most once). Returns
    True: the row always leaves `in_progress` (handled, failed or back at the queue head), except
    when a concurrent writer already moved it."""
    assert start.slug is not None and start.preset_id is not None
    key = spec_key(start.spec_id)
    async with pool.connection() as conn:
        gate = await gate_task_id(conn, start.spec_id)
    prompt = start_prompt(key, spec_url(public_url, key), None if gate is None else task_key(gate))
    store = SpecStartStore(pool)
    run = RunRef(
        start.id, start.slug, start.preset_id, start.session_id, start.message_id, start.updated_at
    )
    now = datetime.now(UTC)
    try:
        done = await dispatch(
            client, store, run, prompt, marker(MARKER_KIND, start.id), project=start.project
        )
    except UsageQuotaRefused as exc:
        log.info("spec start %s: usage refused, held until %s", start.id, exc.retry_at(now))
        async with pool.connection() as conn:
            moved = await spec_starts.requeue_head(
                conn, start.id, "usage_refused", exc.retry_at(now)
            )
            await notify_spec_changed(conn, start.spec_id)
        return moved
    except NotFound:
        # dispatch finished the row failed (preset_not_found or session_not_found)
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT error FROM spec_starts WHERE id = %s", (start.id,))
            (error,) = await cur.fetchone() or ("not_found",)
            await _trigger_failed(conn, start, error)
        return True
    except OrchestratorError as exc:
        # a permanent 4xx (422 unknown project, 400, 403) never succeeds on retry and would block
        # the project's queue at the head: fail the start instead
        error = exc.code or "orchestrator_rejected"
        log.warning("spec start %s: orchestrator rejected the start: %s", start.id, exc)
        async with pool.connection() as conn:
            if not await spec_starts.mark_failed(conn, start.id, "in_progress", error):
                return False
            await _trigger_failed(conn, start, error)
        return True
    except OrchestratorUnavailable as exc:
        log.warning("spec start %s: orchestrator failed, will retry: %s", start.id, exc)
        async with pool.connection() as conn:
            moved = await spec_starts.requeue_head(conn, start.id, "orchestrator_unavailable", now)
            if moved:
                await spec_starts.retry_later(conn, start.id, "queued", backoff_base, backoff_max)
            await notify_spec_changed(conn, start.spec_id)
        return moved
    if not await store.finish(start.id, "handled", None):
        return False
    async with pool.connection() as conn:
        detail = {
            "start_id": start.id,
            "mode": "started",
            "slug": start.slug,
            "session_id": done.session_id,
        }
        await append_spec_event(
            conn, start.spec_id, AUTOSTART_ACTOR, "implementation_triggered", detail
        )
        await notify_spec_changed(conn, start.spec_id)
    return True


async def resume_in_progress(
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    public_url: str,
    backoff_base: float,
    backoff_max: float,
) -> bool:
    """Dispatch again every `in_progress` row (a crash leftover): the stored slug (a 409 on
    create adopts the session), session id, message id and the marker make it idempotent."""
    async with pool.connection() as conn:
        rows = await spec_starts.in_progress(conn)
    changed = False
    for start in rows:
        if await _dispatch_start(pool, client, start, public_url, backoff_base, backoff_max):
            changed = True
    return changed


async def _fail_no_preset(pool: AsyncConnectionPool, head: SpecStart) -> bool:
    async with pool.connection() as conn:
        if not await spec_starts.mark_failed(conn, head.id, "queued", "no_preset"):
            return False
        await _trigger_failed(conn, head, "no_preset")
        gate = await gate_task_id(conn, head.spec_id)
        if gate is not None:
            await add_comment(
                conn,
                AUTOSTART_ACTOR,
                gate,
                f"Spec {spec_key(head.spec_id)} was approved but no implementation session"
                " could start: no preset. Start one by hand.",
            )
    return True


async def _serve_project(
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    presets: PresetLookup,
    project: str,
    settings: AutostartSettings,
    active: int,
    reading: UsageReading | None,
    public_url: str,
    backoff_base: float,
    backoff_max: float,
) -> bool:
    """Hold or start the head of one project's queue; True when a row changed status."""
    async with pool.connection() as conn:
        head = await spec_starts.queue_head(conn, project)
        if head is None:
            return False
        cur = await conn.execute(
            "SELECT next_attempt_at > now() FROM spec_starts WHERE id = %s", (head.id,)
        )
        if (await cur.fetchone())[0]:
            return False
        hold: str | None = None
        if settings.max_active_sessions is None or settings.max_usage_pct is None:
            hold = "autostart_off"
        elif head.session_id is None and active >= settings.max_active_sessions:
            # a requeued start whose session already exists holds its own slot
            hold = "at_limit"
        else:
            gate = usage_gate(reading, settings.max_usage_pct)
            if gate != "ok":
                hold = gate
        if hold is not None:
            await spec_starts.set_hold(conn, head.id, hold)
            return False
        cur = await conn.execute(
            "SELECT preset_id, status FROM specs WHERE id = %s AND deleted_at IS NULL",
            (head.spec_id,),
        )
        spec = await cur.fetchone()
        if spec is None:
            return await spec_starts.mark_failed(conn, head.id, "queued", "spec_deleted")
        if spec[1] != "approved":
            return await _drop_unapproved(conn, head, "queued")
    preset = spec[0] or await presets.project_default(project)
    if preset is None:
        return await _fail_no_preset(pool, head)
    slug = start_slug(head.spec_id, head.ordinal)
    async with pool.connection() as conn:
        # under the queue lock: a move or remove since the head was read wins this pass
        await spec_starts.lock_queue(conn, project)
        current = await spec_starts.queue_head(conn, project)
        if current is None or current.id != head.id:
            return False
        if not await spec_starts.begin_start(conn, head.id, slug, preset):
            return False
    started = dataclasses.replace(current, status="in_progress", slug=slug, preset_id=preset)
    return await _dispatch_start(pool, client, started, public_url, backoff_base, backoff_max)


async def run_queues(
    pool: AsyncConnectionPool,
    client: OrchestratorClient,
    usage: UsageSource | None,
    presets: PresetLookup,
    public_url: str,
    backoff_base: float,
    backoff_max: float,
) -> bool:
    """One pass over every project queue: resume crash leftovers, then start at most one head
    per project (the next pass recounts the active sessions). True when any row changed
    status. Only one notifier runs (advisory singleton), so check-then-create is safe; a crash
    can overshoot a limit by one."""
    changed = await resume_in_progress(pool, client, public_url, backoff_base, backoff_max)
    async with pool.connection() as conn:
        projects = await spec_starts.queued_projects(conn)
    if not projects:
        return changed
    try:
        sessions = await client.list_sessions()
    except (OrchestratorError, OrchestratorUnavailable) as exc:
        log.warning("autostart: session list unavailable, queues held: %s", exc)
        async with pool.connection() as conn:
            for project in projects:
                head = await spec_starts.queue_head(conn, project)
                if head is not None:
                    await spec_starts.set_hold(conn, head.id, "orchestrator_unavailable")
        return changed
    active = Counter(s.project for s in sessions if s.desired == "active")
    reading = None if usage is None else await usage.current()
    async with pool.connection() as conn:
        limits = await settings_for(conn, projects)
    for project in projects:
        if await _serve_project(
            pool,
            client,
            presets,
            project,
            limits[project],
            active[project],
            reading,
            public_url,
            backoff_base,
            backoff_max,
        ):
            changed = True
    return changed
