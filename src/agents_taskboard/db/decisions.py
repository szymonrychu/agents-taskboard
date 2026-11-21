"""Decisions: an agent asks, a user answers once, the asker may withdraw while open.

Answer and withdraw are single status-guarded UPDATEs; the loser of a race matches zero rows.
Like comments they touch `updated_at` but never bump the task version. Closing a task does not
withdraw its open decisions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.rows import class_row, dict_row
from psycopg.types.json import Jsonb

from agents_taskboard.api.actors import Actor
from agents_taskboard.db.events import append_event, notify_changed
from agents_taskboard.db.notices import enqueue_approval_notice, enqueue_decision_notice
from agents_taskboard.db.scope import scoped
from agents_taskboard.db.specs import NotFound, approve_on_last_answer, lock_spec
from agents_taskboard.db.tasks import task_key, task_url, touch_task

USER = Actor(kind="user")
_COLUMNS = (
    "d.id, d.task_id, d.asking_session, d.asking_slug, d.question, d.context, d.options,"
    " d.client_key, d.status, d.answer, d.note, d.created_at, d.answered_at,"
    " d.withdraw_reason, d.superseded_by, d.answered_elsewhere, d.superseded_by_task_id,"
    " d.kind, d.subject, d.expires_at, d.answered_via, d.answer_ref"
)
DEFAULT_APPROVAL_TTL_SECONDS = 259200
EXPIRE_BATCH = 100
APPROVAL_OPTIONS: list[dict[str, Any]] = [
    {"label": "Go", "description": None, "recommended": True},
    {"label": "No-go", "description": None, "recommended": False},
]
VERDICT_CHOICE = {"go": 0, "no_go": 1}
ApprovalResult = Literal["answered", "already_answered", "expired", "withdrawn", "not_found"]
_FROM = (
    "(SELECT x.*, s.task_id AS superseded_by_task_id FROM decisions x"
    " LEFT JOIN decisions s ON s.id = x.superseded_by) d"
)


@dataclass(frozen=True)
class Decision:
    id: int
    task_id: int
    asking_session: str
    asking_slug: str | None
    question: str
    context: str
    options: list[dict[str, Any]]
    client_key: str | None
    status: str
    answer: Any
    note: str | None
    created_at: datetime
    answered_at: datetime | None
    withdraw_reason: str | None
    superseded_by: int | None
    answered_elsewhere: str | None
    superseded_by_task_id: int | None
    kind: str
    subject: str | None
    expires_at: datetime | None
    answered_via: str | None
    answer_ref: str | None

    def as_json(self, public_url: str) -> dict[str, Any]:
        out = asdict(self)
        out.pop("client_key")
        key = task_key(out.pop("task_id"))
        out["task_key"] = key
        target = out.pop("superseded_by_task_id")
        out["superseded_by_task_key"] = None if target is None else task_key(target)
        out["url"] = task_url(public_url, key)
        return out


async def get_decision(
    conn: psycopg.AsyncConnection, actor: Actor, decision_id: int
) -> Decision | None:
    """The decision, or None when it is absent or its task is outside the actor's projects."""
    scope, scope_params = scoped(actor)
    cur = conn.cursor(row_factory=class_row(Decision))
    await cur.execute(
        f"SELECT {_COLUMNS} FROM {_FROM} JOIN tasks t ON t.id = d.task_id WHERE d.id = %s{scope}",
        (decision_id, *scope_params),
    )
    return await cur.fetchone()


async def ask_decision(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    task_id: int,
    question: str,
    context: str,
    options: list[dict[str, Any]],
    client_key: str | None,
    *,
    kind: str = "question",
    subject: str | None = None,
    ttl_seconds: int | None = None,
) -> tuple[Decision, bool]:
    """Insert an open decision; returns (decision, created). A replayed (task, asking session,
    client key) returns the existing decision. Raises NotFound for an absent task or one outside
    the actor's projects."""
    assert actor.session_id is not None
    async with conn.transaction():
        await touch_task(conn, actor, task_id)
        cur = await conn.execute(
            "INSERT INTO decisions (task_id, asking_session, asking_slug, question, context,"
            " options, client_key, status, kind, subject, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s, %s,"
            " CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(secs => %s) END)"
            " ON CONFLICT DO NOTHING RETURNING id",
            (
                task_id,
                actor.session_id,
                actor.slug,
                question,
                context,
                Jsonb(options),
                client_key,
                kind,
                subject,
                ttl_seconds,
                ttl_seconds,
            ),
        )
        row = await cur.fetchone()
        created = row is not None
        if row is None:
            cur = await conn.execute(
                "SELECT id FROM decisions"
                " WHERE task_id = %s AND asking_session = %s AND client_key = %s",
                (task_id, actor.session_id, client_key),
            )
            row = await cur.fetchone()
            assert row is not None
        decision_id = row[0]
        if created:
            detail: dict[str, Any] = {"decision_id": decision_id}
            if kind == "approval":
                detail["kind"] = "approval"
            await append_event(conn, task_id, actor, "decision_asked", detail)
            await notify_changed(conn, task_id)
        decision = await get_decision(conn, actor, decision_id)
        assert decision is not None
        return decision, created


async def withdraw_decision(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    decision_id: int,
    task_id: int,
    reason: str,
    superseded_by: int | None,
    answered_elsewhere: str | None,
) -> Decision | None:
    """Withdraw an open decision of the task asked by the actor's session (or by any session when
    the actor is a superleader), recording why and the outcome (exactly one of superseded_by and
    answered_elsewhere); None when no row matched (also when the task is outside the actor's
    projects)."""
    scope, scope_params = scoped(actor)
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE decisions d SET status = 'withdrawn', withdraw_reason = %s,"
            " superseded_by = %s, answered_elsewhere = %s FROM tasks t"
            " WHERE d.id = %s AND d.task_id = %s AND d.status = 'open'"
            f" AND (d.asking_session = %s OR %s) AND t.id = d.task_id{scope}"
            " RETURNING d.task_id",
            (
                reason,
                superseded_by,
                answered_elsewhere,
                decision_id,
                task_id,
                actor.session_id,
                actor.is_superleader,
                *scope_params,
            ),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        task_id = row[0]
        await touch_task(conn, actor, task_id)
        detail: dict[str, Any] = {"decision_id": decision_id}
        if superseded_by is not None:
            detail["superseded_by"] = superseded_by
        await append_event(conn, task_id, actor, "decision_withdrawn", detail)
        await notify_changed(conn, task_id)
        return await get_decision(conn, actor, decision_id)


async def answer_decision(
    conn: psycopg.AsyncConnection,
    decision_id: int,
    answer: Any,
    note: str | None,
    *,
    via: str = "web",
    answer_ref: str | None = None,
) -> Decision | None:
    """Answer an open, unexpired decision as a user, once, recording the channel (`via`) and its
    reference. None when it was already answered, withdrawn or expired. Event, notice for the
    asking session (one per approval, folded per task for a question) and NOTIFY share the
    transaction."""
    async with conn.transaction():
        # lock order spec, decision, task: patch_spec and the step writes lock the spec first, and
        # two concurrent last answers serialise on the spec row (S-78)
        spec_id = await _question_spec(conn, decision_id)
        locked = None
        if spec_id is not None:
            try:
                locked = await lock_spec(conn, USER, spec_id)
            except NotFound:
                locked = None
        cur = await conn.execute(
            "UPDATE decisions SET status = 'answered', answer = %s, note = %s, answered_at = now(),"
            " answered_via = %s, answer_ref = %s"
            " WHERE id = %s AND status = 'open' AND (expires_at IS NULL OR expires_at > now())"
            " RETURNING asking_session, task_id, kind",
            (Jsonb(answer), note, via, answer_ref, decision_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        asking_session, task_id, kind = row
        await touch_task(conn, USER, task_id)
        await append_event(conn, task_id, USER, "decision_answered", {"decision_id": decision_id})
        if kind == "approval":
            await enqueue_approval_notice(conn, task_id, asking_session, decision_id)
        else:
            await enqueue_decision_notice(conn, task_id, asking_session)
        await notify_changed(conn, task_id)
        if locked is not None:
            await approve_on_last_answer(conn, USER, locked, task_id, decision_id)
        return await get_decision(conn, USER, decision_id)


async def _question_spec(conn: psycopg.AsyncConnection, decision_id: int) -> int | None:
    """The spec worth locking for an open question decision (unlocked read): its task is linked
    to a live draft or review spec and is not a step, else None. Rechecked under the lock."""
    cur = await conn.execute(
        "SELECT t.spec_id FROM decisions d JOIN tasks t ON t.id = d.task_id"
        " JOIN specs s ON s.id = t.spec_id"
        " WHERE d.id = %s AND d.kind = 'question' AND d.status = 'open' AND t.step IS NULL"
        " AND s.deleted_at IS NULL AND s.status IN ('draft', 'review')",
        (decision_id,),
    )
    row = await cur.fetchone()
    return None if row is None else row[0]


async def unanswerable_reason(
    conn: psycopg.AsyncConnection, decision_id: int
) -> Literal["already_answered", "expired", "withdrawn"]:
    """Why an answer's guarded UPDATE matched no row: `expired` for an expired row or one still
    open past its `expires_at`, `withdrawn`, else `already_answered`."""
    cur = await conn.execute(
        "SELECT status, expires_at IS NOT NULL AND expires_at <= now() FROM decisions"
        " WHERE id = %s",
        (decision_id,),
    )
    row = await cur.fetchone()
    status, past = row if row is not None else ("answered", False)
    if status == "expired" or (status == "open" and past):
        return "expired"
    if status == "withdrawn":
        return "withdrawn"
    return "already_answered"


async def expire_approvals(conn: psycopg.AsyncConnection, limit: int = EXPIRE_BATCH) -> int:
    """Flip up to `limit` open approvals past their `expires_at` to expired, in one transaction;
    each gets a `decision_expired` event, an approval notice for the asking session and a NOTIFY.
    Rows an answer holds locked are skipped (the answer wins or loses on its own guard). Returns
    the number expired."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE decisions SET status = 'expired' WHERE id IN (SELECT id FROM decisions"
            " WHERE kind = 'approval' AND status = 'open' AND expires_at <= now() ORDER BY id"
            " LIMIT %s FOR UPDATE SKIP LOCKED) AND status = 'open'"
            " RETURNING id, task_id, asking_session",
            (limit,),
        )
        rows = await cur.fetchall()
        for decision_id, task_id, asking_session in rows:
            await touch_task(conn, USER, task_id)
            await append_event(
                conn, task_id, USER, "decision_expired", {"decision_id": decision_id, "by": "sweep"}
            )
            await enqueue_approval_notice(conn, task_id, asking_session, decision_id)
            await notify_changed(conn, task_id)
        return len(rows)


async def approval_outcome(
    conn: psycopg.AsyncConnection, decision_id: int
) -> tuple[int, str, Any, str | None] | None:
    """(task_id, status, answer, note) of the decision; None when it is gone."""
    cur = await conn.execute(
        "SELECT task_id, status, answer, note FROM decisions WHERE id = %s", (decision_id,)
    )
    row = await cur.fetchone()
    return None if row is None else (row[0], row[1], row[2], row[3])


async def answer_approval(
    conn: psycopg.AsyncConnection,
    decision_id: int,
    verdict: Literal["go", "no_go"],
    note: str | None,
    *,
    via: str,
    answer_ref: str | None,
) -> tuple[ApprovalResult, Decision | None]:
    """Answer an approval Go or No-go. `not_found` (no decision) when it is absent or not an
    approval; otherwise the decision as it stands after the attempt. The read and the answer share
    one transaction, which commits here when `conn` is idle (the batch route's per-item unit)."""
    async with conn.transaction():
        current = await get_decision(conn, USER, decision_id)
        if current is None or current.kind != "approval":
            return "not_found", None
        choice = VERDICT_CHOICE[verdict]
        given = {"choice": choice, "label": APPROVAL_OPTIONS[choice]["label"]}
        decision = await answer_decision(
            conn, decision_id, given, note, via=via, answer_ref=answer_ref
        )
        if decision is not None:
            return "answered", decision
        reason = await unanswerable_reason(conn, decision_id)
        return reason, await get_decision(conn, USER, decision_id)


async def list_approvals(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    *,
    status: str,
    session: str | None,
    project: str | None,
    limit: int,
) -> list[tuple[Decision, str, str]]:
    """Approvals (never questions) on the actor's tasks as (decision, task title, task project),
    soonest expiry first. `open` also requires an unexpired `expires_at`; `all` is no status
    filter."""
    scope, params = scoped(actor)
    where = f"d.kind = 'approval'{scope}"
    if status == "open":
        where += " AND d.status = 'open' AND d.expires_at > now()"
    elif status != "all":
        where += " AND d.status = %s"
        params.append(status)
    if session is not None:
        where += " AND d.asking_session = %s"
        params.append(session)
    if project is not None:
        where += " AND t.project = %s"
        params.append(project)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {_COLUMNS}, t.title AS task_title, t.project AS task_project"
        f" FROM {_FROM} JOIN tasks t ON t.id = d.task_id WHERE {where}"
        " ORDER BY d.expires_at, d.id LIMIT %s",
        (*params, limit),
    )
    out = []
    for row in await cur.fetchall():
        title, proj = row.pop("task_title"), row.pop("task_project")
        out.append((Decision(**row), title, proj))
    return out
