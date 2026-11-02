import asyncio

import pytest

from agents_taskboard.api.actors import Actor
from agents_taskboard.db import tasks as repo

S1 = "11111111-1111-1111-1111-111111111111"
AGENT = Actor(kind="agent", session_id=S1, slug="mx-1")
USER = Actor(kind="user")


def new(**over):
    base = dict(
        title="t",
        body="",
        kind="task",
        status="open",
        labels=[],
        assignee_session=None,
        client_key=None,
    )
    return repo.NewTask(**{**base, **over})


async def test_create_reports_created_flag_and_dedupes(pool):
    async with pool.connection() as conn:
        first, created = await repo.create_task(conn, AGENT, new(client_key="k"))
        again, created_again = await repo.create_task(conn, AGENT, new(title="x", client_key="k"))
    assert (created, created_again) == (True, False)
    assert first == again


async def test_concurrent_creates_with_one_key_make_one_task(pool, conn):
    async def one(actor):
        async with pool.connection() as c:
            return await repo.create_task(c, actor, new(client_key="same"))

    for actor in (AGENT, USER):
        results = await asyncio.gather(*(one(actor) for _ in range(6)))
        assert len({r[0] for r in results}) == 1
        assert sum(1 for r in results if r[1]) == 1
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


async def test_patch_raises_not_found_and_stale(pool):
    async with pool.connection() as conn:
        task_id, _ = await repo.create_task(conn, AGENT, new())
        with pytest.raises(repo.NotFound):
            await repo.patch_task(conn, AGENT, task_id + 1000, 1, {"title": "x"})
        with pytest.raises(repo.StaleVersion):
            await repo.patch_task(conn, AGENT, task_id, 7, {"title": "x"})
        assert await repo.patch_task(conn, AGENT, task_id, 1, {"title": "x"}) is True
        assert await repo.patch_task(conn, AGENT, task_id, 2, {"title": "x"}) is False


async def test_concurrent_patches_with_one_version_only_one_wins(pool):
    async with pool.connection() as conn:
        task_id, _ = await repo.create_task(conn, AGENT, new())

    async def patch(n):
        async with pool.connection() as c:
            try:
                return await repo.patch_task(c, AGENT, task_id, 1, {"title": f"t{n}"})
            except repo.StaleVersion:
                return None

    results = await asyncio.gather(*(patch(i) for i in range(5)))
    assert results.count(True) == 1 and results.count(None) == 4


async def test_parse_task_key():
    assert repo.parse_key("T-12") == 12
    for bad in ("T-0", "T-", "t-1", "T-01x", "12", "T-" + "9" * 30):
        assert repo.parse_key(bad) is None
