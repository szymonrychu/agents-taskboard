"""Spec persistence (base design 3.5, 3.9): a spec is a named bundle of markdown documents with a
linear, immutable version history per document.

Every write to one spec runs under `SELECT ... FOR UPDATE` on the spec row, so writes to a spec
are serialised; a document write names the version it edited (`base_version`, 0 for a new
document) and loses with `StaleVersion(current)` when another write came first. Each write
appends exactly one event (with `spec_id` and the spec's project) and NOTIFYs `spec_changed` in
the same transaction.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agents_taskboard.api.actors import Actor
from agents_taskboard.db.events import (
    append_event,
    append_spec_event,
    notify_changed,
    notify_spec_changed,
)
from agents_taskboard.db.scope import scoped

_KEY = re.compile(r"S-([1-9][0-9]{0,17})")
PATH = re.compile(r"[a-z0-9][a-z0-9._/-]{0,119}\.md")
BODY_CAP = 192 * 1024
MAX_DOCUMENTS = 50
MAX_SOURCES = 20
NOTE_CAP = 500


class NotFound(Exception):
    pass


class NameTaken(Exception):
    pass


class StaleVersion(Exception):
    def __init__(self, current_version: int) -> None:
        super().__init__(f"current version is {current_version}")
        self.current_version = current_version


class TooLarge(Exception):
    pass


class InvalidPath(ValueError):
    pass


class InvalidRequest(ValueError):
    pass


class TooManyDocuments(Exception):
    pass


def spec_key(spec_id: int) -> str:
    return f"S-{spec_id}"


def parse_spec_key(key: str) -> int | None:
    match = _KEY.fullmatch(key)
    return int(match.group(1)) if match else None


def check_path(path: str) -> None:
    if not PATH.fullmatch(path) or ".." in path:
        raise InvalidPath(f"invalid document path {path!r}")


def check_body(body: str | None, cap: int = BODY_CAP) -> None:
    if body is not None and len(body.encode()) > cap:
        raise TooLarge(f"over {cap} bytes")


def _check_version_input(note: str | None, sources: list) -> None:
    if note is not None and len(note) > NOTE_CAP:
        raise InvalidRequest(f"note is longer than {NOTE_CAP}")
    if len(sources) > MAX_SOURCES:
        raise InvalidRequest(f"more than {MAX_SOURCES} sources")


def _by(prefix: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """Pop `<prefix>_kind/_session/_slug` into one actor object (None when kind is null)."""
    kind = row.pop(f"{prefix}_kind")
    session = row.pop(f"{prefix}_session")
    slug = row.pop(f"{prefix}_slug")
    return None if kind is None else {"kind": kind, "session": session, "slug": slug}


_SPEC_COLUMNS = (
    "s.id, s.project, s.name, s.title, s.status, s.labels, s.assignee_session,"
    " s.assignee_slug, s.created_by_kind, s.created_by_session, s.created_by_slug, s.version,"
    " s.created_at, s.updated_at"
)
_VERSION_COLUMNS = (
    "v.path, v.version, v.sha256, v.size, v.note, v.sources, v.created_by_kind,"
    " v.created_by_session, v.created_by_slug, v.created_at"
)


def spec_row_json(row: dict[str, Any]) -> dict[str, Any]:
    """The summary JSON of one spec row (no documents)."""
    row = dict(row)
    spec_id = row.pop("id")
    created_by = _by("created_by", row)
    return {"key": spec_key(spec_id), **row, "created_by": created_by}


def version_json(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    return {**row, "created_by": _by("created_by", row)}


async def _spec_rows(
    conn: psycopg.AsyncConnection, where: str, params: tuple
) -> list[dict[str, Any]]:
    cur = await conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_SPEC_COLUMNS} FROM specs s WHERE s.deleted_at IS NULL AND {where}", params
    )
    return await cur.fetchall()


async def get_spec(
    conn: psycopg.AsyncConnection, actor: Actor, spec_id: int
) -> dict[str, Any] | None:
    """The spec with its current documents, linked task keys and open comment count; None when
    absent, deleted or outside the actor's projects."""
    scope, params = scoped(actor, "s.project")
    rows = await _spec_rows(conn, f"s.id = %s{scope}", (spec_id, *params))
    if not rows:
        return None
    spec = spec_row_json(rows[0])
    spec["documents"] = [
        {k: d[k] for k in ("path", "version", "size", "sha256", "created_by")}
        | {"updated_at": d["created_at"]}
        for d in await list_documents(conn, spec_id)
    ]
    cur = await conn.execute("SELECT id FROM tasks WHERE spec_id = %s ORDER BY id", (spec_id,))
    spec["tasks"] = [f"T-{r[0]}" for r in await cur.fetchall()]
    cur = await conn.execute(
        "SELECT count(*) FROM spec_comments WHERE spec_id = %s AND resolved_at IS NULL",
        (spec_id,),
    )
    spec["open_comments"] = (await cur.fetchone())[0]
    return spec


async def _lock_spec(conn: psycopg.AsyncConnection, actor: Actor, spec_id: int) -> dict[str, Any]:
    """Row-lock the live spec within the actor's scope; NotFound otherwise."""
    scope, params = scoped(actor, "s.project")
    cur = await conn.cursor(row_factory=dict_row).execute(
        "SELECT s.id, s.project, s.status, s.version, s.assignee_session, s.created_by_session"
        f" FROM specs s WHERE s.id = %s AND s.deleted_at IS NULL{scope} FOR UPDATE",
        (spec_id, *params),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound
    return row


async def _insert_version(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    spec_id: int,
    path: str,
    version: int,
    body: str | None,
    note: str | None,
    sources: list,
    client_key: str | None,
) -> dict[str, Any]:
    encoded = None if body is None else body.encode()
    cur = await conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO spec_versions AS v (spec_id, path, version, body, sha256, size, note,"
        " sources, created_by_kind, created_by_session, created_by_slug, client_key)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        f" RETURNING {_VERSION_COLUMNS}",
        (
            spec_id,
            path,
            version,
            body,
            None if encoded is None else hashlib.sha256(encoded).hexdigest(),
            None if encoded is None else len(encoded),
            note,
            Jsonb(sources),
            actor.kind,
            actor.session_id,
            actor.slug,
            client_key,
        ),
    )
    return version_json(await cur.fetchone())


async def create_spec(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    *,
    project: str,
    name: str,
    title: str,
    labels: list[str],
    client_key: str | None,
    docs: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Create a spec with its first documents (each at version 1); returns (spec, created). A
    replayed (creator, client_key) returns the stored spec with created False. Raises NameTaken,
    InvalidPath, TooLarge, TooManyDocuments, InvalidRequest."""
    if len(docs) > MAX_DOCUMENTS:
        raise TooManyDocuments
    paths = [d["path"] for d in docs]
    if len(set(paths)) != len(paths):
        raise InvalidRequest("duplicate document path")
    for doc in docs:
        check_path(doc["path"])
        if doc.get("body") is None:
            raise InvalidRequest("a new document needs a body")
        check_body(doc["body"])
        _check_version_input(doc.get("note"), doc.get("sources") or [])
    async with conn.transaction():
        if client_key is not None:
            existing = await _replayed_spec(conn, actor, client_key)
            if existing is not None:
                spec = await get_spec(conn, actor, existing)
                if spec is None:
                    raise NotFound
                return spec, False
        cur = await conn.execute(
            "INSERT INTO specs (project, name, title, labels, created_by_kind,"
            " created_by_session, created_by_slug, client_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
            (project, name, title, labels, actor.kind, actor.session_id, actor.slug, client_key),
        )
        row = await cur.fetchone()
        if row is None:
            if client_key is not None:
                existing = await _replayed_spec(conn, actor, client_key)
                if existing is not None:
                    spec = await get_spec(conn, actor, existing)
                    if spec is None:
                        raise NotFound
                    return spec, False
            raise NameTaken
        spec_id = row[0]
        for doc in docs:
            await _insert_version(
                conn,
                actor,
                spec_id,
                doc["path"],
                1,
                doc["body"],
                doc.get("note"),
                doc.get("sources") or [],
                None,
            )
        await append_spec_event(conn, spec_id, actor, "spec_created", {"paths": paths})
        await notify_spec_changed(conn, spec_id)
        spec = await get_spec(conn, Actor(kind=actor.kind), spec_id)
        assert spec is not None
        return spec, True


async def _replayed_spec(
    conn: psycopg.AsyncConnection, actor: Actor, client_key: str
) -> int | None:
    """The id of the spec this actor already created with `client_key`. Users have no session
    and the unique index treats NULLs as distinct, so user creates serialise per key."""
    if actor.session_id is None:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('spec:' || %s))", (client_key,))
        cur = await conn.execute(
            "SELECT id FROM specs WHERE created_by_session IS NULL AND client_key = %s",
            (client_key,),
        )
    else:
        cur = await conn.execute(
            "SELECT id FROM specs WHERE created_by_session = %s AND client_key = %s",
            (actor.session_id, client_key),
        )
    row = await cur.fetchone()
    return row[0] if row else None


async def _current(conn: psycopg.AsyncConnection, spec_id: int, path: str) -> tuple[int, bool]:
    """(current version, live) of one document: (0, False) when it never existed; live is
    False for a removal tombstone."""
    cur = await conn.execute(
        "SELECT version, body IS NOT NULL FROM spec_versions WHERE spec_id = %s AND path = %s"
        " ORDER BY version DESC LIMIT 1",
        (spec_id, path),
    )
    row = await cur.fetchone()
    return (row[0], row[1]) if row else (0, False)


async def _live_count(conn: psycopg.AsyncConnection, spec_id: int) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM (SELECT DISTINCT ON (path) body FROM spec_versions"
        " WHERE spec_id = %s ORDER BY path, version DESC) d WHERE body IS NOT NULL",
        (spec_id,),
    )
    return (await cur.fetchone())[0]


async def write_version_locked(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    spec: dict[str, Any],
    path: str,
    *,
    body: str | None,
    base_version: int,
    note: str | None,
    sources: list,
    client_key: str | None,
    event: tuple[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Write the next version of `path` in a spec the caller holds locked (`_lock_spec` row).
    A version after `approved` returns the spec to `review`. `event` overrides the event kind
    and adds to its detail (accepting a suggestion is one event, not two)."""
    spec_id = spec["id"]
    if client_key is not None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_VERSION_COLUMNS} FROM spec_versions v"
            " WHERE v.spec_id = %s AND v.client_key = %s",
            (spec_id, client_key),
        )
        stored = await cur.fetchone()
        if stored is not None:
            return version_json(stored), False
    current, live = await _current(conn, spec_id, path)
    if base_version != current:
        raise StaleVersion(current)
    if body is None and not live:
        raise NotFound
    if body is not None and not live and await _live_count(conn, spec_id) >= MAX_DOCUMENTS:
        raise TooManyDocuments
    version = await _insert_version(
        conn, actor, spec_id, path, current + 1, body, note, sources, client_key
    )
    returned = spec["status"] == "approved"
    await conn.execute(
        "UPDATE specs SET updated_at = now()"
        + (", status = 'review', version = version + 1" if returned else "")
        + " WHERE id = %s",
        (spec_id,),
    )
    kind, extra = event or ("document_put", {})
    detail: dict[str, Any] = {"path": path, "version": current + 1, **extra}
    if body is None:
        detail["removed"] = True
    if returned:
        detail["status"] = "review"
    await append_spec_event(conn, spec_id, actor, kind, detail)
    await notify_spec_changed(conn, spec_id)
    return version, True


async def put_document(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    spec_id: int,
    path: str,
    *,
    body: str | None,
    base_version: int,
    note: str | None,
    sources: list,
    client_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """Write the next version of one document (body None removes it); returns (version,
    created). A replayed (spec, client_key) returns the stored version. Raises NotFound,
    StaleVersion, InvalidPath, TooLarge, TooManyDocuments, InvalidRequest."""
    check_path(path)
    check_body(body)
    _check_version_input(note, sources)
    async with conn.transaction():
        spec = await _lock_spec(conn, actor, spec_id)
        return await write_version_locked(
            conn,
            actor,
            spec,
            path,
            body=body,
            base_version=base_version,
            note=note,
            sources=sources,
            client_key=client_key,
        )


async def get_document(
    conn: psycopg.AsyncConnection, spec_id: int, path: str, version: int | None
) -> dict[str, Any] | None:
    """One version of a document with its body (the current one when `version` is None); None
    when missing or a removal tombstone. The caller has checked the spec is visible."""
    where = "v.spec_id = %s AND v.path = %s"
    params: tuple = (spec_id, path)
    if version is not None:
        where += " AND v.version = %s"
        params = (*params, version)
    cur = await conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_VERSION_COLUMNS}, v.body FROM spec_versions v WHERE {where}"
        " ORDER BY v.version DESC LIMIT 1",
        params,
    )
    row = await cur.fetchone()
    if row is None or row["body"] is None:
        return None
    return version_json(row)


async def list_documents(conn: psycopg.AsyncConnection, spec_id: int) -> list[dict[str, Any]]:
    """The current version of every live document (no bodies), by path."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        f"SELECT * FROM (SELECT DISTINCT ON (v.path) {_VERSION_COLUMNS}, v.body IS NULL AS gone"
        " FROM spec_versions v WHERE v.spec_id = %s ORDER BY v.path, v.version DESC) d"
        " WHERE NOT gone ORDER BY path",
        (spec_id,),
    )
    out = []
    for row in await cur.fetchall():
        row.pop("gone")
        out.append(version_json(row))
    return out


async def list_versions(
    conn: psycopg.AsyncConnection, spec_id: int, path: str, limit: int, cursor: int | None
) -> tuple[list[dict[str, Any]], int | None]:
    """One document's history, newest first, without bodies; the next cursor is the last
    version listed (pass it back to page further)."""
    where = "v.spec_id = %s AND v.path = %s"
    params: tuple = (spec_id, path)
    if cursor is not None:
        where += " AND v.version < %s"
        params = (*params, cursor)
    cur = await conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_VERSION_COLUMNS}, v.body IS NULL AS removed FROM spec_versions v"
        f" WHERE {where} ORDER BY v.version DESC LIMIT %s",
        (*params, limit + 1),
    )
    rows = await cur.fetchall()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = rows[-1]["version"]
    return [version_json(r) for r in rows], next_cursor


async def soft_delete(conn: psycopg.AsyncConnection, actor: Actor, spec_id: int) -> None:
    """Delete a spec for good from every read: sets `deleted_at` (the name is free again) and
    detaches its tasks (each gets a `spec_detached` event). Raises NotFound."""
    async with conn.transaction():
        await _lock_spec(conn, actor, spec_id)
        cur = await conn.execute(
            "UPDATE tasks SET spec_id = NULL, updated_at = now() WHERE spec_id = %s RETURNING id",
            (spec_id,),
        )
        detached = [r[0] for r in await cur.fetchall()]
        await conn.execute(
            "UPDATE specs SET deleted_at = now(), updated_at = now() WHERE id = %s", (spec_id,)
        )
        for task_id in detached:
            await append_event(conn, task_id, actor, "spec_detached", {"spec": spec_key(spec_id)})
            await notify_changed(conn, task_id)
        await append_spec_event(conn, spec_id, actor, "spec_deleted", {"deleted": True})
        await notify_spec_changed(conn, spec_id)
