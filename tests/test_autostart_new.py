"""handle_new: nudge a live spec owner or queue the start (S-51 Q3, Q10)."""

import uuid

import httpx
import pytest
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient
from agents_common.runs import marker

from agents_taskboard.api.actors import Actor
from agents_taskboard.autostart import MARKER_KIND, handle_new, nudge_text
from agents_taskboard.db import spec_starts as repo
from agents_taskboard.db.events import append_spec_event
from agents_taskboard.db.specs import spec_key

PUBLIC = "https://tasks.example.test"
USER = Actor(kind="user")


@pytest.fixture
def fake() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
async def client(fake: FakeOrchestrator):
    c = OrchestratorClient(
        "http://orch.test", fake.token, transport=httpx.ASGITransport(app=fake.app)
    )
    yield c
    await c.aclose()


def add_session(fake: FakeOrchestrator, desired: str = "active") -> str:
    sid = str(uuid.uuid4())
    fake.sessions[sid] = {"id": sid, "slug": "s", "desired": desired}
    return sid


async def new_start(
    pool, name: str, owner: str | None, project: str = "default"
) -> tuple[int, int]:
    """One spec (assigned to `owner`) with a `new` start; returns (spec id, start id)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO specs (name, title, project, created_by_kind, assignee_session, status)"
            " VALUES (%s, 't', %s, 'user', %s, 'approved') RETURNING id",
            (name, project, owner),
        )
        spec_id = (await cur.fetchone())[0]
        event_id = await append_spec_event(conn, spec_id, USER, "spec_updated", {})
        start_id = await repo.insert_start(conn, spec_id, event_id, project)
    return spec_id, start_id


async def run(pool, client) -> bool | None:
    return await handle_new(pool, client, PUBLIC, 0.05, 0.1)


def row(conn, start_id):
    return conn.execute(
        "SELECT status, mode, position, attempts, next_attempt_at > now()"
        " FROM spec_starts WHERE id = %s",
        (start_id,),
    ).fetchone()


def events(conn, spec_id, kind):
    return [
        r[0]
        for r in conn.execute(
            "SELECT detail FROM events WHERE spec_id = %s AND kind = %s ORDER BY id",
            (spec_id, kind),
        ).fetchall()
    ]


def test_nudge_text_is_fixed_template():
    assert (
        nudge_text("S-4", PUBLIC) == f"Spec S-4 approved - take it into implementation now: "
        f"{PUBLIC}/specs/S-4"
    )


async def test_live_owner_is_nudged_once(pool, conn, fake, client):
    owner = add_session(fake)
    spec_id, start_id = await new_start(pool, "a", owner)
    assert await run(pool, client) is True
    (msg,) = fake.messages.values()
    assert msg["session_id"] == owner
    text = nudge_text(spec_key(spec_id), PUBLIC)
    assert msg["text"] == f"{text}\n\n{marker(MARKER_KIND, start_id)}"
    assert row(conn, start_id)[:2] == ("handled", "nudged")
    assert await run(pool, client) is None
    assert len(fake.messages) == 1
    (detail,) = events(conn, spec_id, "implementation_triggered")
    assert detail == {"start_id": start_id, "mode": "nudged", "session_id": owner}


async def test_nudge_not_gated_by_unconfigured_project(pool, conn, fake, client):
    owner = add_session(fake)
    _, start_id = await new_start(pool, "a", owner, project="nowhere")
    assert conn.execute("SELECT count(*) FROM project_settings").fetchone() == (0,)
    assert await run(pool, client) is True
    assert row(conn, start_id)[:2] == ("handled", "nudged")


async def test_gone_owner_enqueues(pool, conn, fake, client):
    spec_id, start_id = await new_start(pool, "a", str(uuid.uuid4()))
    assert await run(pool, client) is True
    assert row(conn, start_id)[:3] == ("queued", None, 1)
    assert not fake.messages
    (detail,) = events(conn, spec_id, "implementation_queued")
    assert detail == {"start_id": start_id, "project": "default", "position": 1}


async def test_deleting_owner_enqueues(pool, conn, fake, client):
    owner = add_session(fake, desired="deleting")
    _, start_id = await new_start(pool, "a", owner)
    assert await run(pool, client) is True
    assert row(conn, start_id)[0] == "queued"
    assert not fake.messages


async def test_no_owner_enqueues(pool, conn, fake, client):
    spec_id, start_id = await new_start(pool, "a", None)
    assert await run(pool, client) is True
    assert row(conn, start_id)[:3] == ("queued", None, 1)
    assert len(events(conn, spec_id, "implementation_queued")) == 1


async def test_orchestrator_down_retries_later(pool, conn, fake, client):
    owner = add_session(fake)
    _, start_id = await new_start(pool, "a", owner)
    fake.fail_next("get_session", 503)
    assert await run(pool, client) is False
    status, _, _, attempts, future = row(conn, start_id)
    assert (status, attempts, future) == ("new", 1, True)
    assert not fake.messages


async def test_crash_after_post_adopts_marker(pool, conn, fake, client):
    owner = add_session(fake)
    _, start_id = await new_start(pool, "a", owner)
    await client.post_message(owner, f"earlier\n\n{marker(MARKER_KIND, start_id)}")
    assert await run(pool, client) is True
    assert len(fake.messages) == 1
    assert row(conn, start_id)[:2] == ("handled", "nudged")


async def test_unconfigured_project_still_queues(pool, conn, fake, client):
    _, a = await new_start(pool, "a", None, project="nowhere")
    _, b = await new_start(pool, "b", None, project="nowhere")
    assert await run(pool, client) is True
    assert await run(pool, client) is True
    assert row(conn, a)[:3] == ("queued", None, 1)
    assert row(conn, b)[:3] == ("queued", None, 2)
    assert conn.execute(
        "SELECT count(*) FROM spec_starts WHERE status='in_progress'"
    ).fetchone() == (0,)


async def test_deleted_spec_fails_the_start(pool, conn, fake, client):
    spec_id, start_id = await new_start(pool, "a", None)
    conn.execute("UPDATE specs SET deleted_at = now() WHERE id = %s", (spec_id,))
    assert await run(pool, client) is True
    status = conn.execute(
        "SELECT status, error FROM spec_starts WHERE id = %s", (start_id,)
    ).fetchone()
    assert status == ("failed", "spec_deleted")


async def test_failed_post_retries_later(pool, conn, fake, client):
    owner = add_session(fake)
    _, start_id = await new_start(pool, "a", owner)
    fake.fail_next("post_message", 503)
    assert await run(pool, client) is False
    status, _, _, attempts, future = row(conn, start_id)
    assert (status, attempts, future) == ("new", 1, True)
    assert not fake.messages


async def test_no_trigger_event_when_nudge_not_recorded(pool, conn, fake, client, monkeypatch):
    owner = add_session(fake)
    spec_id, start_id = await new_start(pool, "a", owner)

    async def unchanged(*_args, **_kwargs):
        return False

    monkeypatch.setattr(repo, "mark_nudged", unchanged)
    assert await run(pool, client) is True
    assert not events(conn, spec_id, "implementation_triggered")


async def test_unapproved_spec_new_row_is_removed(pool, conn, fake, client):
    owner = add_session(fake)
    spec_id, start_id = await new_start(pool, "a", owner)
    conn.execute("UPDATE specs SET status = 'review' WHERE id = %s", (spec_id,))
    assert await run(pool, client) is True
    status = conn.execute(
        "SELECT status, error, position FROM spec_starts WHERE id = %s", (start_id,)
    ).fetchone()
    assert status == ("removed", "spec_not_approved", None)
    assert not fake.messages
    assert events(conn, spec_id, "implementation_trigger_removed") == [
        {"start_id": start_id, "reason": "spec_not_approved"}
    ]
