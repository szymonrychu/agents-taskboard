"""Plan header and plan steps repository (task-plans L1, base design 3.17): a step is a task of the
spec, numbered densely from 1; `specs.steps_version` guards the step set and order."""

import asyncio

import pytest

from agents_taskboard.api.actors import Actor
from agents_taskboard.db import specs
from agents_taskboard.db import steps as repo
from agents_taskboard.db import tasks as tasks_repo

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
AGENT = Actor(kind="agent", session_id=S1, slug="mx-1")
AGENT_2 = Actor(kind="agent", session_id=S2, slug="mx-2")
OUTSIDER = Actor(kind="agent", session_id=S2, slug="mx-2", projects=frozenset({"b"}))
USER = Actor(kind="user")
STEP_CAP = 32 * 1024


async def make_spec(pool, name="plan-a") -> int:
    async with pool.connection() as conn:
        spec, _ = await specs.create_spec(
            conn,
            AGENT,
            project="default",
            name=name,
            title="A plan",
            labels=[],
            client_key=None,
            docs=[{"path": "plan.md", "body": "# Plan\n"}],
        )
    return specs.parse_spec_key(spec["key"])


def step(n, title=None, deps=()):
    """One step of a `set_steps` body; callers merge extra keys with `|`."""
    return {
        "n": n,
        "title": f"step {n}" if title is None else title,
        "body": f"body {n}",
        "depends_on": list(deps),
    }


def steps_of(*ns):
    return [step(n) for n in ns]


async def set_steps(pool, spec_id, items, version, actor=AGENT):
    async with pool.connection() as conn:
        return await repo.set_steps(conn, actor, spec_id, steps_version=version, steps=items)


async def listed(pool, spec_id):
    async with pool.connection() as conn:
        return await repo.list_steps(conn, spec_id)


def task_id(key: str) -> int:
    return tasks_repo.parse_key(key)


# -- plan header -----------------------------------------------------------------------------


async def put_plan(pool, spec_id, version, actor=AGENT, **kw):
    fields = {
        "goal": "g",
        "architecture": "a",
        "tech_stack": "t",
        "constraints": ["c1"],
        "review_focus": ["r1"],
    } | kw
    async with pool.connection() as conn:
        return await repo.put_plan(conn, actor, spec_id, version=version, **fields)


async def test_plan_header_put_and_get(pool, conn):
    sid = await make_spec(pool)
    async with pool.connection() as c:
        empty = await repo.get_plan(c, sid)
    assert empty["version"] == 0 and empty["goal"] == "" and empty["constraints"] == []
    plan = await put_plan(pool, sid, 0)
    assert plan["version"] == 1 and plan["goal"] == "g" and plan["constraints"] == ["c1"]
    assert plan["updated_by"] == {"kind": "agent", "session": S1, "slug": "mx-1"}
    plan = await put_plan(pool, sid, 1, goal="g2")
    async with pool.connection() as c:
        got = await repo.get_plan(c, sid)
    assert got["version"] == 2 and got["goal"] == "g2"
    kinds = [
        r[0]
        for r in conn.execute(
            "SELECT kind FROM events WHERE spec_id = %s ORDER BY id", (sid,)
        ).fetchall()
    ]
    assert kinds == ["spec_created", "plan_set", "plan_set"]


async def test_plan_header_stale_version(pool):
    sid = await make_spec(pool)
    await put_plan(pool, sid, 0)
    with pytest.raises(repo.StaleVersion) as err:
        await put_plan(pool, sid, 0)
    assert err.value.current_version == 1


async def test_plan_header_caps(pool):
    sid = await make_spec(pool)
    plan = await put_plan(
        pool,
        sid,
        0,
        goal="g" * 500,
        architecture="a" * 4000,
        tech_stack="t" * 1000,
        constraints=["c" * 1000] * 50,
        review_focus=["r" * 1000] * 50,
    )
    assert plan["version"] == 1
    for bad in (
        {"goal": "g" * 501},
        {"architecture": "a" * 4001},
        {"tech_stack": "t" * 1001},
        {"constraints": ["c"] * 51},
        {"constraints": ["c" * 1001]},
        {"review_focus": ["r"] * 51},
        {"review_focus": ["r" * 1001]},
    ):
        with pytest.raises(repo.InvalidRequest):
            await put_plan(pool, sid, 1, **bad)


async def test_plan_scope_and_missing_spec(pool):
    sid = await make_spec(pool)
    with pytest.raises(repo.NotFound):
        await put_plan(pool, sid, 0, actor=OUTSIDER)
    async with pool.connection() as c:
        with pytest.raises(repo.NotFound):
            await repo.get_plan(c, sid + 1000)


# -- step set --------------------------------------------------------------------------------


async def test_set_steps_creates_numbered_tasks(pool, conn):
    sid = await make_spec(pool)
    keys = await set_steps(
        pool,
        sid,
        [step(1), step(2, deps=[1]) | {"model": "sonnet", "labels": ["x"], "todo": ["a", "b"]}],
        0,
    )
    assert len(keys) == 2
    rows, version = await listed(pool, sid)
    assert version == 1
    assert [(r["key"], r["step"], r["title"]) for r in rows] == [
        (keys[0], 1, "step 1"),
        (keys[1], 2, "step 2"),
    ]
    assert rows[1]["model"] == "sonnet"
    assert rows[1]["deps"] == [keys[0]]
    assert rows[1]["todo_total"] == 2 and rows[1]["todo_done"] == 0
    assert "body" not in rows[0]
    row = conn.execute(
        "SELECT spec_id, project, status, kind, labels, client_key, body FROM tasks WHERE id = %s",
        (task_id(keys[1]),),
    ).fetchone()
    assert row == (sid, "default", "open", "task", ["x"], f"plan-{sid}-2", "body 2")
    kinds = [
        r[0]
        for r in conn.execute(
            "SELECT kind FROM events WHERE spec_id = %s ORDER BY id", (sid,)
        ).fetchall()
    ]
    assert kinds == ["spec_created", "steps_set"]


async def test_set_steps_idempotent_by_n(pool, conn):
    sid = await make_spec(pool)
    first = await set_steps(pool, sid, steps_of(1, 2, 3), 0)
    again = await set_steps(pool, sid, steps_of(1, 2, 3), 1)
    assert again == first
    _, version = await listed(pool, sid)
    assert version == 1
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3
    changed = await set_steps(pool, sid, [step(1), step(2, title="renamed"), step(3)], 1)
    assert changed == first
    rows, version = await listed(pool, sid)
    assert version == 2
    assert rows[1]["title"] == "renamed"
    assert conn.execute(
        "SELECT version FROM tasks WHERE id = %s", (task_id(first[1]),)
    ).fetchone() == (2,)


async def test_removed_unstarted_step_dropped_and_unnumbered(pool, conn):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, [step(1), step(2), step(3, deps=[2])], 0)
    kept = await set_steps(pool, sid, steps_of(1, 2), 1)
    assert kept == keys[:2]
    row = conn.execute(
        "SELECT status, step, spec_id, client_key FROM tasks WHERE id = %s",
        (task_id(keys[2]),),
    ).fetchone()
    assert row == ("dropped", None, sid, None)
    assert conn.execute(
        "SELECT count(*) FROM task_deps WHERE task_id = %s", (task_id(keys[2]),)
    ).fetchone() == (0,)
    rows, _ = await listed(pool, sid)
    assert [r["key"] for r in rows] == keys[:2]
    # the number is free for a new step again
    readded = await set_steps(pool, sid, steps_of(1, 2, 3), 2)
    assert readded[:2] == keys[:2] and readded[2] != keys[2]


async def test_removed_started_step_raises(pool, conn):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, steps_of(1, 2, 3), 0)
    for status in ("in_progress", "done"):
        conn.execute("UPDATE tasks SET status = %s WHERE id = %s", (status, task_id(keys[2])))
        with pytest.raises(repo.StepInProgress):
            await set_steps(pool, sid, steps_of(1, 2), 1)
    rows, version = await listed(pool, sid)
    assert len(rows) == 3 and version == 1


async def test_stale_steps_version_raises(pool):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, steps_of(1, 2), 0)
    with pytest.raises(repo.StaleVersion) as err:
        await set_steps(pool, sid, steps_of(1, 2, 3), 0)
    assert err.value.current_version == 1
    async with pool.connection() as c:
        with pytest.raises(repo.StaleVersion):
            await repo.insert_step(
                c,
                AGENT,
                sid,
                steps_version=0,
                after=None,
                title="x",
                body="",
                depends_on=[],
                model=None,
            )
        with pytest.raises(repo.StaleVersion):
            await repo.reorder(c, AGENT, sid, steps_version=0, order=keys)


async def test_set_steps_input_rules(pool):
    sid = await make_spec(pool)
    for bad in (
        [step(1), step(3)],  # not dense
        [step(2)],  # not from 1
        [step(1), step(1)],  # duplicate
        [step(1, deps=[2])],  # unknown dependency
        [step(1) | {"model": "gpt"}],
        [step(1, title="")],
        [step(1) | {"todo": ["t"] * 101}],
    ):
        with pytest.raises(repo.InvalidRequest):
            await set_steps(pool, sid, bad, 0)
    with pytest.raises(repo.TooLarge):
        await set_steps(pool, sid, [step(1) | {"body": "x" * (STEP_CAP + 1)}], 0)
    keys = await set_steps(pool, sid, [step(1) | {"body": "x" * STEP_CAP}], 0)
    assert len(keys) == 1


async def test_steps_scope(pool):
    sid = await make_spec(pool)
    with pytest.raises(repo.NotFound):
        await set_steps(pool, sid, steps_of(1), 0, actor=OUTSIDER)


# -- insert and reorder ----------------------------------------------------------------------


async def insert(pool, sid, version, after, title="new", deps=(), model=None):
    async with pool.connection() as conn:
        return await repo.insert_step(
            conn,
            AGENT,
            sid,
            steps_version=version,
            after=after,
            title=title,
            body="b",
            depends_on=list(deps),
            model=model,
        )


async def test_insert_renumbers_densely(pool):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, steps_of(1, 2, 3), 0)
    middle = await insert(pool, sid, 1, after=1, deps=[1], model="opus")
    assert middle["step"] == 2 and middle["model"] == "opus"
    rows, version = await listed(pool, sid)
    assert version == 2
    assert [r["key"] for r in rows] == [keys[0], middle["key"], keys[1], keys[2]]
    assert [r["step"] for r in rows] == [1, 2, 3, 4]
    assert rows[1]["deps"] == [keys[0]]
    first = await insert(pool, sid, 2, after=0, title="first")
    last = await insert(pool, sid, 3, after=None, title="last")
    rows, version = await listed(pool, sid)
    assert version == 4
    assert [r["key"] for r in rows][0] == first["key"]
    assert [r["key"] for r in rows][-1] == last["key"]
    assert [r["step"] for r in rows] == [1, 2, 3, 4, 5, 6]
    with pytest.raises(repo.InvalidRequest):
        await insert(pool, sid, 4, after=7)


async def test_reorder_needs_every_live_step_exactly_once(pool):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, steps_of(1, 2, 3), 0)
    async with pool.connection() as conn:
        for bad in (
            keys[:2],
            [*keys, keys[0]],
            [keys[0], keys[1], keys[1]],
            [keys[0], keys[1], "T-999999"],
            [keys[0], keys[1], "bogus"],
        ):
            with pytest.raises(repo.InvalidRequest):
                await repo.reorder(conn, AGENT, sid, steps_version=1, order=bad)
    async with pool.connection() as conn:
        version = await repo.reorder(conn, AGENT, sid, steps_version=1, order=keys[::-1])
    assert version == 2
    rows, current = await listed(pool, sid)
    assert current == 2
    assert [(r["key"], r["step"]) for r in rows] == [(keys[2], 1), (keys[1], 2), (keys[0], 3)]


# -- dependencies and readiness --------------------------------------------------------------


async def test_cycle_refused(pool, conn):
    sid = await make_spec(pool)
    with pytest.raises(repo.DependencyCycle):
        await set_steps(pool, sid, [step(1, deps=[2]), step(2, deps=[1])], 0)
    with pytest.raises(repo.DependencyCycle):
        await set_steps(pool, sid, [step(1, deps=[1])], 0)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone() == (0,)
    keys = await set_steps(pool, sid, [step(1), step(2, deps=[1]), step(3, deps=[2])], 0)
    async with pool.connection() as c:
        with pytest.raises(repo.DependencyCycle):
            await repo.set_deps(c, AGENT, task_id(keys[0]), [task_id(keys[2])])
    assert conn.execute(
        "SELECT count(*) FROM task_deps WHERE task_id = %s", (task_id(keys[0]),)
    ).fetchone() == (0,)
    async with pool.connection() as c:
        await repo.set_deps(c, AGENT, task_id(keys[2]), [task_id(keys[0])])
    rows, _ = await listed(pool, sid)
    assert rows[2]["deps"] == [keys[0]]


async def test_set_deps_rules(pool, conn):
    sid = await make_spec(pool)
    other = await make_spec(pool, name="plan-b")
    keys = await set_steps(pool, sid, steps_of(1, 2), 0)
    foreign = await set_steps(pool, other, steps_of(1), 0)
    loose = conn.execute(
        "INSERT INTO tasks (title, kind, status, created_by_kind, spec_id)"
        " VALUES ('gate', 'task', 'open', 'user', %s) RETURNING id",
        (sid,),
    ).fetchone()[0]
    async with pool.connection() as c:
        for task, deps in (
            (task_id(keys[1]), [task_id(foreign[0])]),  # another spec
            (task_id(keys[1]), [loose]),  # not a step
            (loose, [task_id(keys[0])]),  # the task is not a step
        ):
            with pytest.raises(repo.InvalidRequest):
                await repo.set_deps(c, AGENT, task, deps)
        with pytest.raises(repo.NotFound):
            await repo.set_deps(c, OUTSIDER, task_id(keys[1]), [task_id(keys[0])])
        with pytest.raises(repo.NotFound):
            await repo.set_deps(c, AGENT, 999999, [])


async def test_ready_only_when_every_dep_done_or_dropped(pool, conn):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, [step(1), step(2), step(3, deps=[1, 2])], 0)
    rows, _ = await listed(pool, sid)
    assert [r["ready"] for r in rows] == [True, True, False]
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task_id(keys[0]),))
    rows, _ = await listed(pool, sid)
    assert [r["ready"] for r in rows] == [False, True, False]
    conn.execute("UPDATE tasks SET status = 'dropped' WHERE id = %s", (task_id(keys[1]),))
    rows, _ = await listed(pool, sid)
    assert [r["ready"] for r in rows] == [False, False, True]
    conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = %s", (task_id(keys[2]),))
    rows, _ = await listed(pool, sid)
    assert [r["ready"] for r in rows] == [False, False, False]


async def test_list_counts_todo(pool, conn):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, [step(1) | {"todo": ["a", "b", "c"]}], 0)
    conn.execute(
        "UPDATE todo_items SET status = 'done' WHERE task_id = %s AND text = 'a'",
        (task_id(keys[0]),),
    )
    rows, _ = await listed(pool, sid)
    assert (rows[0]["todo_total"], rows[0]["todo_done"]) == (3, 1)
    # an unchanged todo list keeps its statuses, a changed one keeps matching texts
    await set_steps(pool, sid, [step(1) | {"todo": ["a", "b", "c"]}], 1)
    await set_steps(pool, sid, [step(1) | {"todo": ["a", "d"]}], 1)
    got = conn.execute(
        "SELECT text, status FROM todo_items WHERE task_id = %s ORDER BY position",
        (task_id(keys[0]),),
    ).fetchall()
    assert got == [("a", "done"), ("d", "todo")]


# -- next step and claims --------------------------------------------------------------------


async def next_step(pool, sid, claim, actor=AGENT):
    async with pool.connection() as conn:
        return await repo.next_step(conn, actor, sid, claim=claim)


async def test_next_step_without_claim_assigns_nothing(pool, conn):
    sid = await make_spec(pool)
    assert await next_step(pool, sid, False) is None
    keys = await set_steps(pool, sid, [step(1, deps=[2]), step(2)], 0)
    got = await next_step(pool, sid, False)
    assert got["key"] == keys[1] and got["status"] == "open"
    assert conn.execute(
        "SELECT status, assignee_session FROM tasks WHERE id = %s", (task_id(keys[1]),)
    ).fetchone() == ("open", None)
    claimed = await next_step(pool, sid, True)
    assert claimed["key"] == keys[1] and claimed["status"] == "in_progress"
    assert claimed["assignee_session"] == S1
    assert conn.execute(
        "SELECT status, assignee_session, version FROM tasks WHERE id = %s",
        (task_id(keys[1]),),
    ).fetchone() == ("in_progress", S1, 2)
    assert await next_step(pool, sid, True) is None
    with pytest.raises(repo.NotFound):
        await next_step(pool, sid, True, actor=OUTSIDER)


async def test_concurrent_claims_get_different_steps(pool, conn):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, steps_of(1, 2, 3), 0)
    results = await asyncio.gather(
        next_step(pool, sid, True, AGENT),
        next_step(pool, sid, True, AGENT_2),
    )
    claimed = [r["key"] for r in results]
    assert len(set(claimed)) == 2 and set(claimed) <= set(keys)
    assert conn.execute("SELECT count(*) FROM tasks WHERE status = 'in_progress'").fetchone() == (
        2,
    )


# -- caps and gate ---------------------------------------------------------------------------


def test_refs_cap_and_shape():
    refs = [{"kind": "pr", "value": f"o/r#{i}"} for i in range(20)]
    assert repo.check_refs(refs) == refs
    for bad in (
        [*refs, {"kind": "url", "value": "https://x"}],
        [{"kind": "issue", "value": "x"}],
        [{"kind": "pr", "value": ""}],
        [{"kind": "pr"}],
    ):
        with pytest.raises(repo.InvalidRequest):
            repo.check_refs(bad)


def test_step_body_cap():
    repo.check_step_body("x" * STEP_CAP)
    with pytest.raises(repo.TooLarge):
        repo.check_step_body("x" * (STEP_CAP + 1))


async def test_gate_task_is_lowest_non_step_task(pool, conn):
    sid = await make_spec(pool)
    async with pool.connection() as c:
        assert await repo.gate_task_id(c, sid) is None

    def add(status="open"):
        return conn.execute(
            "INSERT INTO tasks (title, kind, status, created_by_kind, spec_id)"
            " VALUES ('t', 'task', %s, 'user', %s) RETURNING id",
            (status, sid),
        ).fetchone()[0]

    add("dropped")
    await set_steps(pool, sid, steps_of(1, 2), 0)
    gate = add()
    add()
    async with pool.connection() as c:
        assert await repo.gate_task_id(c, sid) == gate


# -- leaving the spec ------------------------------------------------------------------------


async def test_soft_delete_unnumbers_steps(pool, conn):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, [step(1), step(2, deps=[1])], 0)
    async with pool.connection() as c:
        await specs.soft_delete(c, USER, sid)
    assert conn.execute(
        "SELECT spec_id, step FROM tasks WHERE id = ANY(%s) ORDER BY id",
        ([task_id(k) for k in keys],),
    ).fetchall() == [(None, None), (None, None)]
    assert conn.execute("SELECT count(*) FROM task_deps").fetchone() == (0,)


async def test_task_leaving_spec_loses_step_and_deps(pool, conn):
    sid = await make_spec(pool)
    keys = await set_steps(pool, sid, [step(1), step(2, deps=[1])], 0)
    async with pool.connection() as c:
        changed = await tasks_repo.patch_task(c, USER, task_id(keys[1]), 1, {"spec_id": None})
    assert changed
    assert conn.execute(
        "SELECT spec_id, step FROM tasks WHERE id = %s", (task_id(keys[1]),)
    ).fetchone() == (None, None)
    assert conn.execute("SELECT count(*) FROM task_deps").fetchone() == (0,)
