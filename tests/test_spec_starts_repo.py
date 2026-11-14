"""The spec start queue repository (S-51)."""

from datetime import UTC, datetime

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agents_taskboard.api.actors import Actor
from agents_taskboard.db import spec_starts as repo
from agents_taskboard.db.events import append_spec_event

USER = Actor(kind="user")


async def make_spec(conn, name="a", project="default") -> int:
    cur = await conn.execute(
        "INSERT INTO specs (name, title, project, created_by_kind) VALUES (%s, 't', %s, 'user')"
        " RETURNING id",
        (name, project),
    )
    return (await cur.fetchone())[0]


async def queued(pool, project="p", n=3) -> list[int]:
    """n queued starts in `project`, positions 1..n, one spec each."""
    ids = []
    async with pool.connection() as conn:
        for i in range(n):
            spec = await make_spec(conn, f"{project}-{i}", project)
            sid = await repo.insert_start(conn, spec, 100 + i, project)
            assert await repo.enqueue(conn, sid, None) == i + 1
            ids.append(sid)
    return ids


async def order(pool, project="p") -> list[tuple[int, int]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, position FROM spec_starts WHERE project = %s AND status = 'queued'"
            " ORDER BY position",
            (project,),
        )
        return await cur.fetchall()


async def status_of(pool, sid) -> tuple:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, position, hold_reason, slug, mode FROM spec_starts WHERE id = %s",
            (sid,),
        )
        return await cur.fetchone()


async def test_insert_start_is_idempotent_per_event(pool):
    async with pool.connection() as conn:
        spec = await make_spec(conn)
        first = await repo.insert_start(conn, spec, 1, "default")
        again = await repo.insert_start(conn, spec, 1, "default")
        second = await repo.insert_start(conn, spec, 2, "default")
        rows = await (
            await conn.execute("SELECT id, ordinal FROM spec_starts ORDER BY id")
        ).fetchall()
    assert first is not None and again is None and second is not None
    assert rows == [(first, 1), (second, 2)]


async def test_append_spec_event_returns_the_id(pool):
    async with pool.connection() as conn:
        spec = await make_spec(conn)
        one = await append_spec_event(conn, spec, USER, "approved")
        two = await append_spec_event(conn, spec, USER, "approved")
    assert isinstance(one, int) and two > one


async def test_enqueue_appends_fifo_per_project(pool):
    ids_a = await queued(pool, "a", 2)
    ids_b = await queued(pool, "b", 1)
    assert await order(pool, "a") == [(ids_a[0], 1), (ids_a[1], 2)]
    assert await order(pool, "b") == [(ids_b[0], 1)]
    async with pool.connection() as conn:
        assert await repo.queued_projects(conn) == ["a", "b"]
        head = await repo.queue_head(conn, "a")
        assert head is not None and head.id == ids_a[0] and head.position == 1
        assert await repo.queue_head(conn, "zzz") is None
        assert await repo.enqueue(conn, ids_a[0], None) is None


async def test_move_to_position_rewrites_dense_order(pool):
    one, two, three = await queued(pool)
    async with pool.connection() as conn:
        assert await repo.move(conn, three, position=1, before=None) == "p"
    assert await order(pool) == [(three, 1), (one, 2), (two, 3)]
    async with pool.connection() as conn:
        await repo.move(conn, three, position=99, before=None)
    assert await order(pool) == [(one, 1), (two, 2), (three, 3)]


async def test_move_before_row(pool):
    one, two, three = await queued(pool)
    async with pool.connection() as conn:
        await repo.move(conn, three, position=None, before=two)
    assert await order(pool) == [(one, 1), (three, 2), (two, 3)]
    async with pool.connection() as conn:
        await repo.move(conn, one, position=None, before=two)
    assert await order(pool) == [(three, 1), (one, 2), (two, 3)]


async def test_move_before_other_project_or_unqueued_is_invalid(pool):
    one, two = (await queued(pool, "p", 2))[:2]
    (other,) = await queued(pool, "q", 1)
    async with pool.connection() as conn:
        with pytest.raises(repo.InvalidMove):
            await repo.move(conn, one, position=None, before=other)
    async with pool.connection() as conn:
        with pytest.raises(repo.InvalidMove):
            await repo.move(conn, one, position=None, before=one)
    assert await order(pool) == [(one, 1), (two, 2)]


async def test_move_refuses_non_queued(pool):
    one, two = (await queued(pool, "p", 2))[:2]
    async with pool.connection() as conn:
        assert await repo.begin_start(conn, one, "slug-1", "preset")
    async with pool.connection() as conn:
        with pytest.raises(repo.NotQueued):
            await repo.move(conn, one, position=1, before=None)
    async with pool.connection() as conn:
        with pytest.raises(repo.StartNotFound):
            await repo.move(conn, 999999, position=1, before=None)
    assert await order(pool) == [(two, 2)]


async def test_move_refuses_both_or_neither(pool):
    one, two = (await queued(pool, "p", 2))[:2]
    async with pool.connection() as conn:
        with pytest.raises(repo.InvalidMove):
            await repo.move(conn, one, position=1, before=two)
    async with pool.connection() as conn:
        with pytest.raises(repo.InvalidMove):
            await repo.move(conn, one, position=None, before=None)


async def test_remove_only_queued(pool):
    one, two = (await queued(pool, "p", 2))[:2]
    async with pool.connection() as conn:
        assert isinstance(await repo.remove(conn, one), int)
    assert await status_of(pool, one) == ("removed", None, None, None, None)
    async with pool.connection() as conn:
        with pytest.raises(repo.NotQueued):
            await repo.remove(conn, one)
    async with pool.connection() as conn:
        with pytest.raises(repo.StartNotFound):
            await repo.remove(conn, 999999)
    assert await order(pool) == [(two, 2)]


async def test_begin_start_guarded(pool):
    (one,) = await queued(pool, "p", 1)
    async with pool.connection() as conn:
        assert await repo.begin_start(conn, one, "slug-1", "preset") is True
        assert await repo.begin_start(conn, one, "slug-2", "preset") is False
    assert await status_of(pool, one) == ("in_progress", None, None, "slug-1", None)
    async with pool.connection() as conn:
        assert [s.id for s in await repo.in_progress(conn)] == [one]


async def test_requeue_head_goes_first_and_renumbers_densely(pool):
    one, two, three = await queued(pool)
    async with pool.connection() as conn:
        await repo.begin_start(conn, one, "slug-1", "preset")
        await repo.begin_start(conn, two, "slug-2", "preset")
    assert await order(pool) == [(three, 3)]
    async with pool.connection() as conn:
        assert await repo.requeue_head(conn, two, "usage_refused", datetime.now(UTC)) is True
    assert await order(pool) == [(two, 1), (three, 2)]
    async with pool.connection() as conn:
        assert await repo.requeue_head(conn, one, "orchestrator_unavailable", datetime.now(UTC))
        assert await repo.requeue_head(conn, one, "usage_refused", datetime.now(UTC)) is False
    assert await order(pool) == [(one, 1), (two, 2), (three, 3)]
    assert await status_of(pool, one) == ("queued", 1, "orchestrator_unavailable", "slug-1", None)


async def test_set_hold_guarded_and_idempotent(pool):
    one, two = (await queued(pool, "p", 2))[:2]
    async with pool.connection() as conn:
        assert await repo.set_hold(conn, one, "at_limit") is True
        assert await repo.set_hold(conn, one, "at_limit") is False
        assert await repo.set_hold(conn, one, None) is True
        await repo.begin_start(conn, two, "s", "p")
        assert await repo.set_hold(conn, two, "at_limit") is False


async def test_store_finish_guarded(pool):
    one, two, three = await queued(pool)
    store = repo.SpecStartStore(pool)
    async with pool.connection() as conn:
        for sid in (one, two, three):
            await repo.begin_start(conn, sid, f"s{sid}", "preset")
    await store.set_session(one, "sess")
    await store.set_message(one, "msg")
    assert await store.finish(one, "handled", None) is True
    assert await store.finish(one, "handled", None) is False
    assert await store.finish(one, "failed", "late") is False
    await store.set_session(one, "other")
    assert await store.finish(two, "failed", "preset_not_found") is True
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT id, status, mode, session_id, message_id, error, finished_at IS NOT NULL"
                " FROM spec_starts ORDER BY id"
            )
        ).fetchall()
    assert rows[0] == (one, "handled", "started", "sess", "msg", None, True)
    assert rows[1] == (two, "failed", None, None, None, "preset_not_found", True)
    assert rows[2][1] == "in_progress"


async def test_mark_failed_from_queued_clears_position(pool):
    one, two = (await queued(pool, "p", 2))[:2]
    async with pool.connection() as conn:
        assert await repo.mark_failed(conn, one, "queued", "no_preset") is True
        assert await repo.mark_failed(conn, one, "queued", "again") is False
    assert await status_of(pool, one) == ("failed", None, None, None, None)
    assert await order(pool) == [(two, 2)]


async def test_mark_nudged_and_retry_later(pool):
    async with pool.connection() as conn:
        spec = await make_spec(conn)
        sid = await repo.insert_start(conn, spec, 1, "default")
        assert await repo.retry_later(conn, sid, "new", 10.0, 25.0) is True
        assert await repo.retry_later(conn, sid, "new", 10.0, 25.0) is True
        assert await repo.retry_later(conn, sid, "queued", 10.0, 25.0) is False
        row = await (
            await conn.execute(
                "SELECT attempts, next_attempt_at > now() + interval '19 seconds',"
                " next_attempt_at < now() + interval '26 seconds' FROM spec_starts"
            )
        ).fetchone()
        assert row == (2, True, True)
        assert await repo.claim_new(conn) is None
        await conn.execute("UPDATE spec_starts SET next_attempt_at = now()")
        assert await repo.mark_nudged(conn, sid, "owner", "msg") is True
        assert await repo.mark_nudged(conn, sid, "owner", "msg") is False
    assert await status_of(pool, sid) == ("handled", None, None, None, "nudged")


async def test_claim_new_skips_locked_row(pool, db_url):
    async with pool.connection() as conn:
        spec = await make_spec(conn)
        sid = await repo.insert_start(conn, spec, 1, "default")
    async with await psycopg.AsyncConnection.connect(db_url) as first:
        claimed = await repo.claim_new(first)
        assert claimed is not None and claimed.id == sid and claimed.ordinal == 1
        async with await psycopg.AsyncConnection.connect(db_url) as second:
            assert await repo.claim_new(second) is None


async def test_list_queue_scoped_and_ordered(pool: AsyncConnectionPool):
    a = await queued(pool, "a", 2)
    b = await queued(pool, "b", 1)
    async with pool.connection() as conn:
        everything = await repo.list_queue(conn, USER, None)
        only_b = await repo.list_queue(conn, USER, "b")
        scoped_actor = Actor(kind="agent", session_id="s", projects=frozenset({"a"}))
        scoped_rows = await repo.list_queue(conn, scoped_actor, None)
        denied = await repo.list_queue(conn, scoped_actor, "b")
    assert [r["id"] for r in everything] == [*a, *b]
    assert [r["id"] for r in only_b] == b
    assert [r["id"] for r in scoped_rows] == a
    assert denied == []
    assert everything[0]["spec"].startswith("S-") and everything[0]["title"] == "t"


async def test_starts_of_spec_newest_first(pool):
    async with pool.connection() as conn:
        spec = await make_spec(conn)
        first = await repo.insert_start(conn, spec, 1, "default")
        second = await repo.insert_start(conn, spec, 2, "default")
        rows = await repo.starts_of_spec(conn, spec)
        limited = await repo.starts_of_spec(conn, spec, limit=1)
    assert [r["id"] for r in rows] == [second, first]
    assert [r["ordinal"] for r in rows] == [2, 1]
    assert len(limited) == 1
