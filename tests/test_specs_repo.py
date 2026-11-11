"""Specs repository (task-plans A1, base design 3.5, 3.9): bundles of versioned markdown documents
with linear, immutable versions per document."""

import asyncio

import pytest

from agents_taskboard.api.actors import Actor
from agents_taskboard.db import specs as repo

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
AGENT = Actor(kind="agent", session_id=S1, slug="mx-1")
AGENT_B = Actor(kind="agent", session_id=S2, slug="mx-2", projects=frozenset({"b"}))
USER = Actor(kind="user")
CAP = 192 * 1024


async def make(pool, actor=AGENT, *, name="plan-a", project="default", docs=None, client_key=None):
    async with pool.connection() as conn:
        spec, created = await repo.create_spec(
            conn,
            actor,
            project=project,
            name=name,
            title="A plan",
            labels=["x"],
            client_key=client_key,
            docs=docs if docs is not None else [{"path": "plan.md", "body": "# Plan\n"}],
        )
    assert created
    return spec


async def put(pool, spec_id, path="plan.md", body="v", base=1, actor=AGENT, **kw):
    async with pool.connection() as conn:
        return await repo.put_document(
            conn,
            actor,
            spec_id,
            path,
            body=body,
            base_version=base,
            note=kw.get("note"),
            sources=kw.get("sources", []),
            client_key=kw.get("client_key"),
        )


def spec_id_of(spec: dict) -> int:
    return repo.parse_spec_key(spec["key"])


def events_of(conn, spec_id: int) -> list[tuple]:
    return conn.execute(
        "SELECT kind, project, task_id FROM events WHERE spec_id = %s ORDER BY id", (spec_id,)
    ).fetchall()


def test_keys():
    assert repo.spec_key(7) == "S-7"
    assert repo.parse_spec_key("S-7") == 7
    for bad in ("S-0", "S-x", "T-7", "S-", "s-7", "S-07"):
        assert repo.parse_spec_key(bad) is None


async def test_create_with_docs_and_replay(pool, conn):
    first = await make(pool, client_key="k1")
    assert first["key"].startswith("S-")
    assert first["status"] == "draft"
    assert [(d["path"], d["version"]) for d in first["documents"]] == [("plan.md", 1)]
    async with pool.connection() as c:
        again, created = await repo.create_spec(
            c,
            AGENT,
            project="default",
            name="other-name",
            title="t",
            labels=[],
            client_key="k1",
            docs=[],
        )
    assert created is False
    assert again["key"] == first["key"]
    assert conn.execute("SELECT count(*) FROM specs").fetchone()[0] == 1
    assert events_of(conn, spec_id_of(first)) == [("spec_created", "default", None)]


async def test_create_name_taken_per_project(pool):
    await make(pool)
    with pytest.raises(repo.NameTaken):
        await make(pool)
    await make(pool, project="b")


async def test_put_current_base_creates_next(pool, conn):
    spec = await make(pool)
    version, created = await put(pool, spec_id_of(spec), body="second", base=1, note="n")
    assert created and version["version"] == 2 and version["note"] == "n"
    async with pool.connection() as c:
        doc = await repo.get_document(c, spec_id_of(spec), "plan.md", None)
        old = await repo.get_document(c, spec_id_of(spec), "plan.md", 1)
    assert doc["body"] == "second" and doc["version"] == 2
    assert old["body"] == "# Plan\n"
    assert doc["sha256"] and doc["size"] == len(b"second")
    kinds = [k for k, *_ in events_of(conn, spec_id_of(spec))]
    assert kinds == ["spec_created", "document_put"]


async def test_new_document_needs_base_zero(pool):
    spec = await make(pool)
    version, _ = await put(pool, spec_id_of(spec), path="research/a.md", body="r", base=0)
    assert version["version"] == 1


async def test_put_stale_base_raises_with_current(pool):
    spec = await make(pool)
    await put(pool, spec_id_of(spec), base=1)
    with pytest.raises(repo.StaleVersion) as err:
        await put(pool, spec_id_of(spec), base=1)
    assert err.value.current_version == 2


async def test_put_replay_by_client_key(pool, conn):
    spec = await make(pool)
    first, created = await put(pool, spec_id_of(spec), base=1, client_key="c1")
    again, created_again = await put(pool, spec_id_of(spec), base=1, client_key="c1")
    assert (created, created_again) == (True, False)
    assert again["version"] == first["version"] == 2
    assert len(events_of(conn, spec_id_of(spec))) == 2


async def test_concurrent_puts_one_wins(pool, conn):
    spec = await make(pool)

    async def one(body):
        try:
            return await put(pool, spec_id_of(spec), body=body, base=1)
        except repo.StaleVersion as exc:
            return exc

    results = await asyncio.gather(one("a"), one("b"))
    stale = [r for r in results if isinstance(r, repo.StaleVersion)]
    won = [r for r in results if not isinstance(r, repo.StaleVersion)]
    assert len(stale) == 1 and len(won) == 1
    assert stale[0].current_version == 2
    count = conn.execute(
        "SELECT count(*) FROM spec_versions WHERE spec_id = %s", (spec_id_of(spec),)
    ).fetchone()[0]
    assert count == 2


async def test_tombstone_hides_document(pool):
    spec = await make(pool)
    await put(pool, spec_id_of(spec), body=None, base=1)
    async with pool.connection() as c:
        assert await repo.get_document(c, spec_id_of(spec), "plan.md", None) is None
        assert await repo.get_document(c, spec_id_of(spec), "plan.md", 2) is None
        assert (await repo.get_document(c, spec_id_of(spec), "plan.md", 1))["body"]
        assert await repo.list_documents(c, spec_id_of(spec)) == []
        got = await repo.get_spec(c, AGENT, spec_id_of(spec))
    assert got["documents"] == []


async def test_path_rules(pool):
    spec = await make(pool)
    for bad in ("../x.md", "a.txt", "A.md", "a/../b.md", "/a.md", "x" * 121 + ".md"):
        with pytest.raises(repo.InvalidPath):
            await put(pool, spec_id_of(spec), path=bad, base=0)
    version, _ = await put(pool, spec_id_of(spec), path="research/brief-1.md", base=0)
    assert version["path"] == "research/brief-1.md"
    with pytest.raises(repo.InvalidPath):
        await make(pool, name="other", docs=[{"path": "../x.md", "body": "x"}])


async def test_51st_document_refused(pool):
    docs = [{"path": f"d{i}.md", "body": "x"} for i in range(50)]
    spec = await make(pool, docs=docs)
    with pytest.raises(repo.TooManyDocuments):
        await put(pool, spec_id_of(spec), path="d50.md", base=0)
    with pytest.raises(repo.TooManyDocuments):
        await make(pool, name="other", docs=[*docs, {"path": "d50.md", "body": "x"}])
    # a removal frees a slot
    await put(pool, spec_id_of(spec), path="d0.md", body=None, base=1)
    await put(pool, spec_id_of(spec), path="d50.md", base=0)


async def test_body_cap(pool):
    spec = await make(pool)
    body = chr(0xE9) * (CAP // 2)  # two UTF-8 bytes each
    assert len(body.encode()) == CAP
    version, _ = await put(pool, spec_id_of(spec), body=body, base=1)
    assert version["size"] == CAP
    with pytest.raises(repo.TooLarge):
        await put(pool, spec_id_of(spec), body=body + "x", base=2)
    with pytest.raises(repo.TooLarge):
        await make(pool, name="other", docs=[{"path": "a.md", "body": "x" * (CAP + 1)}])


async def test_version_after_approved_returns_to_review(pool, conn):
    spec = await make(pool)
    conn.execute("UPDATE specs SET status = 'approved' WHERE id = %s", (spec_id_of(spec),))
    await put(pool, spec_id_of(spec), base=1)
    async with pool.connection() as c:
        got = await repo.get_spec(c, AGENT, spec_id_of(spec))
    assert got["status"] == "review"


async def test_get_spec_is_scoped(pool):
    spec = await make(pool, project="a")
    async with pool.connection() as c:
        assert await repo.get_spec(c, AGENT_B, spec_id_of(spec)) is None
        assert await repo.get_spec(c, USER, spec_id_of(spec)) is not None
    with pytest.raises(repo.NotFound):
        await put(pool, spec_id_of(spec), base=1, actor=AGENT_B)


async def test_list_versions_pages_newest_first(pool):
    spec = await make(pool)
    for base in (1, 2, 3):
        await put(pool, spec_id_of(spec), body=f"v{base + 1}", base=base)
    async with pool.connection() as c:
        page, cursor = await repo.list_versions(c, spec_id_of(spec), "plan.md", 3, None)
        rest, end = await repo.list_versions(c, spec_id_of(spec), "plan.md", 3, cursor)
    assert [v["version"] for v in page] == [4, 3, 2]
    assert [v["version"] for v in rest] == [1] and end is None
    assert "body" not in page[0]


async def test_soft_delete_frees_name_and_detaches_tasks(pool, conn):
    spec = await make(pool)
    sid = spec_id_of(spec)
    task = conn.execute(
        "INSERT INTO tasks (title, kind, status, created_by_kind, spec_id)"
        " VALUES ('t', 'task', 'open', 'user', %s) RETURNING id",
        (sid,),
    ).fetchone()[0]
    async with pool.connection() as c:
        await repo.soft_delete(c, USER, sid)
        assert await repo.get_spec(c, USER, sid) is None
    assert conn.execute("SELECT spec_id FROM tasks WHERE id = %s", (task,)).fetchone() == (None,)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    with pytest.raises(repo.NotFound):
        await put(pool, sid, base=1)
    again = await make(pool)
    assert again["key"] != spec["key"]
    assert events_of(conn, sid)[-1][0] == "spec_deleted"


async def test_moving_spec_project_moves_its_tasks(pool, conn):
    spec = await make(pool)
    sid = spec_id_of(spec)
    task = conn.execute(
        "INSERT INTO tasks (title, kind, status, created_by_kind, spec_id)"
        " VALUES ('t', 'task', 'open', 'user', %s) RETURNING id",
        (sid,),
    ).fetchone()[0]
    conn.execute("UPDATE specs SET project = 'b' WHERE id = %s", (sid,))
    assert conn.execute("SELECT project, spec_id FROM tasks WHERE id = %s", (task,)).fetchone() == (
        "b",
        sid,
    )


async def test_every_write_appends_one_event_with_spec_and_project(pool, conn):
    spec = await make(pool, project="a")
    sid = spec_id_of(spec)
    await put(pool, sid, base=1)
    await put(pool, sid, path="b.md", base=0)
    async with pool.connection() as c:
        await repo.soft_delete(c, USER, sid)
    rows = events_of(conn, sid)
    assert len(rows) == 4
    assert all(project == "a" and task is None for _, project, task in rows)
