"""Per-project autostart settings (S-51): no defaults, both values needed to be configured."""

from agents_taskboard.db import project_settings as repo


async def test_missing_row_is_unconfigured(pool):
    async with pool.connection() as conn:
        got = await repo.get_settings(conn, "a")
    assert got.project == "a"
    assert got.max_active_sessions is None and got.max_usage_pct is None
    assert got.updated_at is None and got.configured is False


async def test_put_then_get(pool):
    async with pool.connection() as conn:
        put = await repo.put_settings(conn, "a", 2, 80, updated_by="alice")
    async with pool.connection() as conn:
        got = await repo.get_settings(conn, "a")
        many = await repo.settings_for(conn, ["a", "b"])
    assert got == put and got.configured is True and got.updated_at is not None
    assert (got.max_active_sessions, got.max_usage_pct) == (2, 80)
    assert many["a"] == got and many["b"].configured is False
    async with pool.connection() as conn:
        again = await repo.put_settings(conn, "a", 3, 70)
        row = await (await conn.execute("SELECT updated_by FROM project_settings")).fetchone()
    assert (again.max_active_sessions, again.max_usage_pct) == (3, 70)
    assert row == (None,)


async def test_one_value_null_is_unconfigured(pool):
    async with pool.connection() as conn:
        await repo.put_settings(conn, "a", 2, None)
        await repo.put_settings(conn, "b", None, 50)
    async with pool.connection() as conn:
        got = await repo.settings_for(conn, ["a", "b"])
    assert got["a"].configured is False and got["b"].configured is False
    assert got["a"].max_active_sessions == 2 and got["b"].max_usage_pct == 50
