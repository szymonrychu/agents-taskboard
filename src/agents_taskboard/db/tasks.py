"""Task persistence: idempotent create, versioned patch, comments, filtered listing and the
hydrated read model. Every mutation appends its events and NOTIFYs in one transaction."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from agents_taskboard.api.actors import Actor
from agents_taskboard.db.events import append_event, notify_changed

ACTIVE = ("idea", "open", "in_progress")
_KEY = re.compile(r"T-([1-9][0-9]{0,17})")
PATCH_FIELDS = ("title", "body", "status", "labels", "assignee_session")


class NotFound(Exception):
    pass


class StaleVersion(Exception):
    pass


class BadCursor(ValueError):
    pass


@dataclass(frozen=True)
class NewTask:
    title: str
    body: str
    kind: str
    status: str
    labels: list[str]
    assignee_session: str | None
    client_key: str | None


def parse_key(key: str) -> int | None:
    match = _KEY.fullmatch(key)
    return int(match.group(1)) if match else None


def task_key(task_id: int) -> str:
    return f"T-{task_id}"


def task_url(public_url: str, key: str) -> str:
    return f"{public_url}/tasks/{key}"


async def create_task(
    conn: psycopg.AsyncConnection, actor: Actor, new: NewTask
) -> tuple[int, bool]:
    """Insert a task; returns (id, created). A replayed (creator, client_key) returns the
    existing id with created False."""
    async with conn.transaction():
        if new.client_key is not None and actor.session_id is None:
            # The unique index cannot dedupe NULL creators, so users serialise per key.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (new.client_key,))
            cur = await conn.execute(
                "SELECT id FROM tasks WHERE created_by_session IS NULL AND client_key = %s",
                (new.client_key,),
            )
            existing = await cur.fetchone()
            if existing:
                return existing[0], False
        cur = await conn.execute(
            "INSERT INTO tasks (title, body, kind, status, labels, assignee_session,"
            " created_by_kind, created_by_session, created_by_slug, client_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT DO NOTHING RETURNING id",
            (
                new.title,
                new.body,
                new.kind,
                new.status,
                new.labels,
                new.assignee_session,
                actor.kind,
                actor.session_id,
                actor.slug,
                new.client_key,
            ),
        )
        row = await cur.fetchone()
        if row is None:
            cur = await conn.execute(
                "SELECT id FROM tasks WHERE created_by_session = %s AND client_key = %s",
                (actor.session_id, new.client_key),
            )
            existing = await cur.fetchone()
            assert existing is not None
            return existing[0], False
        task_id = row[0]
        await append_event(
            conn, task_id, actor, "created", {"kind": new.kind, "status": new.status}
        )
        if new.labels:
            await append_event(conn, task_id, actor, "label_added", {"labels": new.labels})
        await notify_changed(conn, task_id)
        return task_id, True


async def patch_task(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    task_id: int,
    version: int,
    changes: dict[str, Any],
) -> bool:
    """Apply `changes` (keys of PATCH_FIELDS) if `version` is current; returns whether anything
    changed. Raises NotFound or StaleVersion."""
    async with conn.transaction():
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT version, title, body, status, labels, assignee_session"
            " FROM tasks WHERE id = %s FOR UPDATE",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFound
        if row["version"] != version:
            raise StaleVersion
        changed = {k: v for k, v in changes.items() if row[k] != v}
        if not changed:
            return False
        assignments = [sql.SQL("{} = %s").format(sql.Identifier(k)) for k in changed]
        cur = await conn.execute(
            sql.SQL(
                "UPDATE tasks SET {}, version = version + 1, updated_at = now()"
                " WHERE id = %s AND version = %s RETURNING id"
            ).format(sql.SQL(", ").join(assignments)),
            (*changed.values(), task_id, version),
        )
        if await cur.fetchone() is None:
            raise StaleVersion
        plain = [k for k in ("title", "body", "assignee_session") if k in changed]
        if plain:
            await append_event(conn, task_id, actor, "updated", {"fields": plain})
        if "status" in changed:
            await append_event(
                conn,
                task_id,
                actor,
                "status_changed",
                {"from": row["status"], "to": changed["status"]},
            )
        if "labels" in changed:
            old, new = row["labels"], changed["labels"]
            added = [lab for lab in new if lab not in old]
            removed = [lab for lab in old if lab not in new]
            if added:
                await append_event(conn, task_id, actor, "label_added", {"labels": added})
            if removed:
                await append_event(
                    conn, task_id, actor, "labels_changed", {"labels": new, "removed": removed}
                )
        await notify_changed(conn, task_id)
        return True


async def add_comment(conn: psycopg.AsyncConnection, actor: Actor, task_id: int, body: str) -> None:
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE tasks SET updated_at = now() WHERE id = %s RETURNING id", (task_id,)
        )
        if await cur.fetchone() is None:
            raise NotFound
        cur = await conn.execute(
            "INSERT INTO comments (task_id, author_kind, author_session, author_slug, body)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (task_id, actor.kind, actor.session_id, actor.slug, body),
        )
        comment_id = (await cur.fetchone())[0]
        await append_event(conn, task_id, actor, "comment_added", {"comment_id": comment_id})
        await notify_changed(conn, task_id)


def encode_cursor(blocked: bool, task_id: int) -> str:
    return base64.urlsafe_b64encode(f"{int(blocked)}:{task_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[bool, int]:
    try:
        blocked, task_id = base64.urlsafe_b64decode(cursor.encode()).decode().split(":")
        if blocked not in ("0", "1"):
            raise ValueError
        return blocked == "1", int(task_id)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise BadCursor("invalid cursor") from None


_OPEN_DECISION = "EXISTS (SELECT 1 FROM decisions d WHERE d.task_id = t.id AND d.status = 'open')"


async def list_task_ids(
    conn: psycopg.AsyncConnection,
    *,
    status: str = "active",
    session: str | None = None,
    waiting: bool | None = None,
    labels: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[int], str | None]:
    """Task ids, blocked first then newest first, plus the cursor of the next page.

    `session` matches tasks the session is assignee of, creator of, or asked a decision on
    (open or not); `waiting` keeps tasks with an open decision (asked by `session` when given).
    """
    where: list[str] = []
    params: list[Any] = []
    if status == "active":
        where.append("t.status = ANY(%s)")
        params.append(list(ACTIVE))
    elif status != "all":
        where.append("t.status = %s")
        params.append(status)
    if session is not None:
        where.append(
            "(t.assignee_session = %s OR t.created_by_session = %s OR EXISTS ("
            "SELECT 1 FROM decisions d WHERE d.task_id = t.id AND d.asking_session = %s))"
        )
        params += [session, session, session]
    if waiting:
        if session is None:
            where.append(_OPEN_DECISION)
        else:
            where.append(
                "EXISTS (SELECT 1 FROM decisions d WHERE d.task_id = t.id"
                " AND d.status = 'open' AND d.asking_session = %s)"
            )
            params.append(session)
    if labels:
        where.append("t.labels && %s::text[]")
        params.append(labels)
    outer = ""
    if cursor is not None:
        blocked, last_id = decode_cursor(cursor)
        outer = "WHERE (s.blocked, s.id) < (%s, %s)"
        outer_params = [blocked, last_id]
    else:
        outer_params = []
    query = (
        f"SELECT s.id, s.blocked FROM (SELECT t.id, {_OPEN_DECISION} AS blocked FROM tasks t"
        f" {'WHERE ' + ' AND '.join(where) if where else ''}) s {outer}"
        " ORDER BY s.blocked DESC, s.id DESC LIMIT %s"
    )
    cur = await conn.execute(query, (*params, *outer_params, limit + 1))
    rows = await cur.fetchall()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1][1], rows[-1][0])
    return [r[0] for r in rows], next_cursor


async def _rows(conn: psycopg.AsyncConnection, query: str, ids: list[int]) -> list[dict]:
    cur = await conn.cursor(row_factory=dict_row).execute(query, (ids,))
    return await cur.fetchall()


async def load_tasks(
    conn: psycopg.AsyncConnection, ids: list[int], public_url: str
) -> list[dict[str, Any]]:
    """The task JSON for `ids`, in that order (missing ids are skipped)."""
    if not ids:
        return []
    tasks = {
        r["id"]: r
        for r in await _rows(
            conn,
            "SELECT id, title, body, kind, status, labels, assignee_session, version,"
            " created_at, updated_at FROM tasks WHERE id = ANY(%s)",
            ids,
        )
    }
    children: dict[str, dict[int, list[dict]]] = {}
    for name, query in (
        (
            "todo",
            "SELECT id, task_id, text, status, position FROM todo_items"
            " WHERE task_id = ANY(%s) ORDER BY position, id",
        ),
        (
            "decisions",
            "SELECT id, task_id, asking_session, asking_slug, question, context, options,"
            " status, answer, note, created_at, answered_at FROM decisions"
            " WHERE task_id = ANY(%s) ORDER BY id",
        ),
        (
            "comments",
            "SELECT id, task_id, author_kind, author_session, author_slug, body, created_at"
            " FROM comments WHERE task_id = ANY(%s) ORDER BY id",
        ),
        (
            "events",
            "SELECT id, task_id, actor_kind, actor_session, actor_slug, kind, detail, at"
            " FROM events WHERE task_id = ANY(%s) ORDER BY id",
        ),
    ):
        grouped: dict[int, list[dict]] = {}
        for row in await _rows(conn, query, ids):
            grouped.setdefault(row.pop("task_id"), []).append(row)
        children[name] = grouped
    out = []
    for task_id in ids:
        task = tasks.get(task_id)
        if task is None:
            continue
        decisions = children["decisions"].get(task_id, [])
        waiting: dict[str, str | None] = {}
        open_count = 0
        for d in decisions:
            if d["status"] == "open":
                open_count += 1
                waiting.setdefault(d["asking_session"], d["asking_slug"])
        key = task_key(task_id)
        task.pop("id")
        out.append(
            {
                "key": key,
                **task,
                "blocked": open_count > 0,
                "open_decisions": open_count,
                "waiting_sessions": [
                    {"session_id": s, "slug": slug} for s, slug in waiting.items()
                ],
                "todo": children["todo"].get(task_id, []),
                "decisions": decisions,
                "comments": children["comments"].get(task_id, []),
                "events": children["events"].get(task_id, []),
                "url": task_url(public_url, key),
            }
        )
    return out
