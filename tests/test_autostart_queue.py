"""run_queues: start queued specs under the project limit and usage gate (S-51 Q9, Q11-Q14)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient
from agents_common.runs import marker

from agents_taskboard.api.actors import Actor
from agents_taskboard.autostart import (
    MARKER_KIND,
    run_queues,
    start_prompt,
    start_slug,
)
from agents_taskboard.db import spec_starts as repo
from agents_taskboard.db.events import append_spec_event
from agents_taskboard.db.project_settings import put_settings
from agents_taskboard.db.specs import spec_key
from agents_taskboard.notifier import run_notifier
from agents_taskboard.presets import PresetLookup
from agents_taskboard.usage import UsageReading

PUBLIC = "https://tasks.example.test"
USER = Actor(kind="user")
LOW = UsageReading(five_hour=10.0, seven_day=10.0)


class StubUsage:
    def __init__(self, reading: UsageReading | None) -> None:
        self.reading = reading

    async def current(self) -> UsageReading | None:
        return self.reading


@pytest.fixture
def fake() -> FakeOrchestrator:
    f = FakeOrchestrator()
    f.add_project("a", "A")
    f.add_project("b", "B")
    return f


@pytest.fixture
async def client(fake: FakeOrchestrator):
    c = OrchestratorClient(
        "http://orch.test", fake.token, transport=httpx.ASGITransport(app=fake.app)
    )
    yield c
    await c.aclose()


@pytest.fixture
def presets(client) -> PresetLookup:
    return PresetLookup(client)


@pytest.fixture
def preset(fake: FakeOrchestrator) -> str:
    return str(fake.add_preset("impl", project="a").id)


def add_session(fake: FakeOrchestrator, project: str, desired: str = "active") -> str:
    sid = str(uuid.uuid4())
    fake.sessions[sid] = {"id": sid, "slug": f"s-{sid}", "desired": desired, "project": project}
    return sid


async def queued(
    pool, name: str, preset_id: str | None, project: str = "a", gate: bool = False
) -> tuple[int, int, int | None]:
    """One spec with a queued start; returns (spec id, start id, gate task id)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO specs (name, title, project, created_by_kind, preset_id, status)"
            " VALUES (%s, 't', %s, 'user', %s, 'approved') RETURNING id",
            (name, project, preset_id),
        )
        spec_id = (await cur.fetchone())[0]
        task_id = None
        if gate:
            cur = await conn.execute(
                "INSERT INTO tasks (title, kind, status, created_by_kind, spec_id, project)"
                " VALUES ('gate', 'task', 'open', 'user', %s, %s) RETURNING id",
                (spec_id, project),
            )
            task_id = (await cur.fetchone())[0]
        event_id = await append_spec_event(conn, spec_id, USER, "spec_updated", {})
        start_id = await repo.insert_start(conn, spec_id, event_id, project)
        await repo.enqueue(conn, start_id, None)
    return spec_id, start_id, task_id


async def configure(pool, project: str, limit: int | None, pct: int | None) -> None:
    async with pool.connection() as conn:
        await put_settings(conn, project, limit, pct)


async def run(pool, client, presets, usage=LOW) -> bool:
    stub = None if usage is None else StubUsage(usage)
    return await run_queues(pool, client, stub, presets, PUBLIC, 0.0, 0.0)


def row(conn, start_id):
    return conn.execute(
        "SELECT status, mode, position, hold_reason, error FROM spec_starts WHERE id = %s",
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


def created(fake: FakeOrchestrator, project: str = "a") -> list[dict]:
    return [
        s
        for s in fake.sessions.values()
        if s["slug"].startswith("spec-s-") and s["project"] == project
    ]


def test_start_slug_and_prompt_are_fixed():
    assert start_slug(12, 3) == "spec-s-12-impl-3"
    text = start_prompt("S-12", f"{PUBLIC}/specs/S-12", None)
    assert text.startswith(
        f"Spec S-12 was approved and is yours to implement: {PUBLIC}/specs/S-12. Gate task: none."
    )
    assert text.endswith("archive the spec, close the task, then self-close.")
    assert "Gate task: T-4." in start_prompt("S-12", "u", "T-4")


async def test_unconfigured_project_holds_autostart_off(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    await run(pool, client, presets)
    assert row(conn, a)[:4] == ("queued", None, 1, "autostart_off")
    await configure(pool, "a", 2, None)
    await run(pool, client, presets)
    assert row(conn, a)[3] == "autostart_off"
    assert not created(fake)


async def test_setting_both_values_releases_queue(pool, conn, fake, client, presets, preset):
    spec_id, a, gate_id = await queued(pool, "a", preset, gate=True)
    await run(pool, client, presets)
    await configure(pool, "a", 2, 90)
    assert await run(pool, client, presets) is True
    (session,) = created(fake)
    assert session["slug"] == f"spec-s-{spec_id}-impl-1"
    (msg,) = fake.messages.values()
    key = spec_key(spec_id)
    prompt = start_prompt(key, f"{PUBLIC}/specs/{key}", f"T-{gate_id}")
    assert msg["text"] == f"{prompt}\n\n{marker(MARKER_KIND, a)}"
    assert msg["session_id"] == session["id"]
    assert row(conn, a)[:4] == ("handled", "started", None, None)
    (detail,) = events(conn, spec_id, "implementation_triggered")
    assert detail == {
        "start_id": a,
        "mode": "started",
        "slug": session["slug"],
        "session_id": session["id"],
    }


async def test_at_limit_holds_head(pool, conn, fake, client, presets, preset):
    add_session(fake, "a")
    add_session(fake, "a")
    add_session(fake, "a", desired="deleting")
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 2, 90)
    assert await run(pool, client, presets) is False
    assert row(conn, a)[:4] == ("queued", None, 1, "at_limit")
    assert not created(fake)


async def test_other_project_sessions_do_not_count(pool, conn, fake, client, presets, preset):
    for _ in range(5):
        add_session(fake, "b")
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 1, 90)
    assert await run(pool, client, presets) is True
    assert row(conn, a)[:2] == ("handled", "started")


async def test_five_hour_at_pct_holds(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 2, 80)
    await run(pool, client, presets, UsageReading(five_hour=80.0, seven_day=10.0))
    assert row(conn, a)[3] == "usage_high"
    assert not created(fake)


async def test_weekly_at_pct_holds(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 2, 80)
    await run(pool, client, presets, UsageReading(five_hour=10.0, seven_day=85.0))
    assert row(conn, a)[3] == "usage_high"
    assert not created(fake)


async def test_both_below_starts(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 2, 80)
    await run(pool, client, presets, UsageReading(five_hour=79.0, seven_day=79.9))
    assert row(conn, a)[:2] == ("handled", "started")


async def test_usage_missing_holds_usage_unknown(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 2, 80)
    assert await run(pool, client, presets, None) is False
    assert row(conn, a)[3] == "usage_unknown"
    await run(pool, client, presets, UsageReading(five_hour=10.0, seven_day=None))
    assert row(conn, a)[3] == "usage_unknown"
    assert not created(fake)
    assert await run(pool, client, presets) is True
    assert row(conn, a)[:2] == ("handled", "started")


async def test_fifo_by_position_and_one_start_per_pass(pool, conn, fake, client, presets, preset):
    rows = [(await queued(pool, n, preset))[1] for n in ("a", "b", "c")]
    await configure(pool, "a", 3, 90)
    await run(pool, client, presets)
    assert [row(conn, r)[0] for r in rows] == ["handled", "queued", "queued"]
    await run(pool, client, presets)
    assert [row(conn, r)[0] for r in rows] == ["handled", "handled", "queued"]
    assert len(created(fake)) == 2


async def test_moved_row_starts_first(pool, conn, fake, client, presets, preset):
    rows = [(await queued(pool, n, preset))[1] for n in ("a", "b", "c")]
    async with pool.connection() as c:
        await repo.move(c, rows[2], position=1, before=None)
    await configure(pool, "a", 3, 90)
    await run(pool, client, presets)
    assert [row(conn, r)[0] for r in rows] == ["queued", "queued", "handled"]
    first, second = (row(conn, r)[2] for r in rows[:2])
    assert first < second


async def test_slot_frees_then_next_starts(pool, conn, fake, client, presets, preset):
    first = (await queued(pool, "a", preset))[1]
    second = (await queued(pool, "b", preset))[1]
    await configure(pool, "a", 1, 90)
    await run(pool, client, presets)
    await run(pool, client, presets)
    assert row(conn, first)[0] == "handled"
    assert row(conn, second)[0::3] == ("queued", "at_limit")
    (session,) = created(fake)
    fake.discard_session(session["id"])
    assert await run(pool, client, presets) is True
    assert row(conn, second)[:2] == ("handled", "started")


async def test_no_preset_fails_with_comment(pool, conn, fake, client, presets):
    spec_id, a, gate_id = await queued(pool, "a", None, gate=True)
    fake.add_preset("elsewhere", is_default=True, project="b")
    await configure(pool, "a", 2, 90)
    assert await run(pool, client, presets) is True
    assert row(conn, a) == ("failed", None, None, None, "no_preset")
    (body,) = [
        r[0]
        for r in conn.execute("SELECT body FROM comments WHERE task_id = %s", (gate_id,)).fetchall()
    ]
    assert body == (
        f"Spec S-{spec_id} was approved but no implementation session could start: no preset."
        " Start one by hand."
    )
    assert events(conn, spec_id, "implementation_trigger_failed") == [
        {"start_id": a, "error": "no_preset"}
    ]
    assert not created(fake)


async def test_no_preset_without_gate_task_still_fails(pool, conn, fake, client, presets):
    spec_id, a, _ = await queued(pool, "a", None)
    await configure(pool, "a", 2, 90)
    await run(pool, client, presets)
    assert row(conn, a)[0] == "failed"
    assert conn.execute("SELECT count(*) FROM comments").fetchone() == (0,)


async def test_project_default_preset_is_the_fallback(pool, conn, fake, client, presets):
    default = str(fake.add_preset("dflt", is_default=True, project="a").id)
    await queued(pool, "a", None)
    await configure(pool, "a", 2, 90)
    await run(pool, client, presets)
    (session,) = created(fake)
    assert session["preset_id"] == default


async def test_spec_preset_used_before_project_default(pool, conn, fake, client, presets, preset):
    fake.add_preset("dflt", is_default=True, project="a")
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 2, 90)
    await run(pool, client, presets)
    (session,) = created(fake)
    assert session["preset_id"] == preset
    assert conn.execute("SELECT preset_id FROM spec_starts WHERE id = %s", (a,)).fetchone() == (
        preset,
    )


async def test_gone_preset_fails_the_start(pool, conn, fake, client, presets):
    spec_id, a, _ = await queued(pool, "a", str(uuid.uuid4()))
    await configure(pool, "a", 2, 90)
    assert await run(pool, client, presets) is True
    assert row(conn, a)[0] == "failed"
    assert row(conn, a)[4] == "preset_not_found"
    assert events(conn, spec_id, "implementation_trigger_failed") == [
        {"start_id": a, "error": "preset_not_found"}
    ]


@pytest.mark.parametrize("status", [400, 403, 422])
async def test_permanent_4xx_on_create_fails_the_start(
    pool, conn, fake, client, presets, preset, status
):
    spec_id, a, _ = await queued(pool, "a", preset)
    _, b, _ = await queued(pool, "a2", preset)
    await configure(pool, "a", 2, 90)
    fake.fail_next("create_session", status)
    assert await run(pool, client, presets) is True
    assert row(conn, a)[0] == "failed"
    assert row(conn, a)[4] is not None
    assert row(conn, a)[4] != "orchestrator_unavailable"
    (detail,) = events(conn, spec_id, "implementation_trigger_failed")
    assert detail["start_id"] == a
    await run(pool, client, presets)
    assert row(conn, b)[0] != "queued"


async def test_usage_refusal_requeues_at_head(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    _, b, _ = await queued(pool, "b", preset)
    await configure(pool, "a", 2, 90)
    resets = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    fake.refuse_usage(resets)
    await run(pool, client, presets)
    assert row(conn, a)[:4] == ("queued", None, 1, "usage_refused")
    assert row(conn, b)[:3] == ("queued", None, 2)
    (due,) = conn.execute("SELECT next_attempt_at FROM spec_starts WHERE id = %s", (a,)).fetchone()
    assert due == resets + timedelta(seconds=60)
    assert conn.execute("SELECT count(*) FROM spec_starts WHERE status = 'failed'").fetchone() == (
        0,
    )
    # the head waits for the reset and nothing behind it jumps the queue
    await run(pool, client, presets)
    assert row(conn, a)[0] == "queued"
    assert row(conn, b)[0] == "queued"
    assert not created(fake)


async def test_crash_between_create_and_post_resumes_once(
    pool, conn, fake, client, presets, preset
):
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 2, 90)
    fake.fail_next("post_message", 503)
    await run(pool, client, presets)
    assert row(conn, a)[0] in ("in_progress", "queued")
    assert len(created(fake)) == 1
    assert not fake.messages
    await run(pool, client, presets)
    await run(pool, client, presets)
    assert len(created(fake)) == 1
    (msg,) = fake.messages.values()
    assert msg["text"].endswith(marker(MARKER_KIND, a))
    assert row(conn, a)[:2] == ("handled", "started")


async def test_in_progress_leftover_resumes_and_adopts(pool, conn, fake, client, presets, preset):
    spec_id, a, _ = await queued(pool, "a", preset)
    slug = start_slug(spec_id, 1)
    async with pool.connection() as c:
        assert await repo.begin_start(c, a, slug, preset)
    # the crash came after create_session but before the session id was stored
    await client.create_session(preset, slug, project="a")
    assert await run(pool, client, presets) is True
    assert len(created(fake)) == 1
    assert len(fake.messages) == 1
    assert row(conn, a)[:2] == ("handled", "started")
    assert len(events(conn, spec_id, "implementation_triggered")) == 1


async def test_queued_rows_never_expire(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    conn.execute(
        "UPDATE spec_starts SET created_at = now() - interval '30 days',"
        " updated_at = now() - interval '30 days' WHERE id = %s",
        (a,),
    )
    await configure(pool, "a", 2, 90)
    await run(pool, client, presets)
    assert row(conn, a)[:2] == ("handled", "started")


async def test_orchestrator_list_down_holds(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    _, b, _ = await queued(pool, "b", preset, project="b")
    await configure(pool, "a", 2, 90)
    fake.fail_next("list_sessions", 503)
    assert await run(pool, client, presets) is False
    assert row(conn, a)[:4] == ("queued", None, 1, "orchestrator_unavailable")
    assert row(conn, b)[:4] == ("queued", None, 1, "orchestrator_unavailable")
    assert not created(fake)


async def test_orchestrator_down_on_create_requeues(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 2, 90)
    fake.fail_next("create_session", 503)
    await run(pool, client, presets)
    assert row(conn, a)[:4] == ("queued", None, 1, "orchestrator_unavailable")
    assert conn.execute("SELECT attempts FROM spec_starts WHERE id = %s", (a,)).fetchone() == (1,)
    await run(pool, client, presets)
    assert row(conn, a)[:2] == ("handled", "started")


async def test_clear_head_drops_old_hold(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    _, b, _ = await queued(pool, "b", preset)
    await configure(pool, "a", 1, 90)
    await run(pool, client, presets)
    await run(pool, client, presets)
    assert row(conn, b)[3] == "at_limit"
    await configure(pool, "a", 3, 90)
    await run(pool, client, presets)
    assert row(conn, b)[:4] == ("handled", "started", None, None)


async def test_remove_during_pass_wins(pool, conn, fake, client, presets, monkeypatch):
    spec_id, a, _ = await queued(pool, "a", None)
    default = str(fake.add_preset("dflt", is_default=True, project="a").id)
    await configure(pool, "a", 2, 90)

    async def remove_then_answer(project):
        async with pool.connection() as c:
            await repo.remove(c, a)
        return default

    monkeypatch.setattr(presets, "project_default", remove_then_answer)
    assert await run(pool, client, presets) is False
    assert row(conn, a)[0] == "removed"
    assert not created(fake)


async def test_move_during_pass_starts_nothing_then_new_head(
    pool, conn, fake, client, presets, monkeypatch
):
    default = str(fake.add_preset("dflt", is_default=True, project="a").id)
    _, a, _ = await queued(pool, "a", None)
    _, b, _ = await queued(pool, "b", None)
    await configure(pool, "a", 2, 90)
    calls = []

    async def move_then_answer(project):
        if not calls:
            async with pool.connection() as c:
                await repo.move(c, b, position=1, before=None)
        calls.append(project)
        return default

    monkeypatch.setattr(presets, "project_default", move_then_answer)
    await run(pool, client, presets)
    assert row(conn, a)[0] == "queued"
    assert row(conn, b)[0] == "queued"
    assert not created(fake)
    await run(pool, client, presets)
    assert row(conn, b)[0] == "handled"
    assert row(conn, a)[0] == "queued"


async def test_requeued_start_holds_its_own_slot(pool, conn, fake, client, presets, preset):
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 1, 90)
    fake.fail_next("post_message", 503)
    await run(pool, client, presets)
    assert row(conn, a)[0::3] == ("queued", "orchestrator_unavailable")
    # its own session fills the only slot, yet it must still get its prompt
    assert await run(pool, client, presets) is True
    assert len(created(fake)) == 1
    assert len(fake.messages) == 1
    assert row(conn, a)[:2] == ("handled", "started")


async def test_notifier_loop_starts_a_queued_spec(pool, conn, fake, client, preset, settings):
    fast = settings.model_copy(
        update={"notifier_poll_seconds": 0.05, "notifier_backoff_max_seconds": 0.1}
    )
    _, a, _ = await queued(pool, "a", preset)
    await configure(pool, "a", 1, 90)
    stop = asyncio.Event()
    task = asyncio.create_task(run_notifier(fast, pool, client, stop, usage=StubUsage(LOW)))
    try:
        for _ in range(100):
            if row(conn, a)[0] == "handled":
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert row(conn, a)[:2] == ("handled", "started")
    assert len(created(fake)) == 1


async def test_unapproved_spec_row_is_removed_not_started(
    pool, conn, fake, client, presets, preset
):
    spec_id, a, _ = await queued(pool, "a", preset)
    _, b, _ = await queued(pool, "b2", preset)
    await configure(pool, "a", 5, 90)
    conn.execute("UPDATE specs SET status = 'review' WHERE id = %s", (spec_id,))
    assert await run(pool, client, presets) is True
    assert row(conn, a) == ("removed", None, None, None, "spec_not_approved")
    assert not created(fake)
    assert events(conn, spec_id, "implementation_trigger_removed") == [
        {"start_id": a, "reason": "spec_not_approved"}
    ]
    assert row(conn, b)[0] == "queued"
