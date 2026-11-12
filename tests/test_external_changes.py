"""POST /tasks/{key}/external-changes: delegate listener only, mindmap token only; the linked
notices it queues for the task's audience."""

import logging

import httpx
import pytest
from agents_common.fake_orchestrator import FakeOrchestrator
from conftest import BASE_URL, DELEGATE_TOKEN, MINDMAP_TOKEN
from test_api_tasks import S1, S2, S3, TASKS, agent, create
from test_label_notify import live

from agents_taskboard.api.app import create_delegate_app
from agents_taskboard.notifier import SessionLookup, notice_text, scan_external_events

MINDMAP = {"Authorization": f"Bearer {MINDMAP_TOKEN}"}
BODY = {"source": "mindmap", "ref": "M-3", "actor_session": None}


def url(key: str) -> str:
    return f"{TASKS}/{key}/external-changes"


@pytest.fixture
def fake() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
async def lookup(fake: FakeOrchestrator):
    c = SessionLookup("http://orch.test", fake.token, transport=httpx.ASGITransport(app=fake.app))
    yield c
    await c.aclose()


async def scan_all(pool, lookup) -> int:
    scanned = 0
    while True:
        async with pool.connection() as conn:
            if await scan_external_events(conn, lookup) is None:
                return scanned
        scanned += 1


def linked(conn) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT task_id::text, session_id FROM notices WHERE reason = 'linked' ORDER BY id"
    ).fetchall()


async def test_route_only_on_delegate(web_client, service_client, delegate_client):
    key = (await create(service_client))["key"]
    assert (await web_client.post(url(key), json=BODY)).status_code == 404
    assert (await service_client.post(url(key), json=BODY, headers=agent())).status_code == 404
    r = await delegate_client.post(url(key), json=BODY, headers=MINDMAP)
    assert r.status_code == 202


async def test_mindmap_token_only(delegate_client, service_client):
    key = (await create(service_client))["key"]
    r = await delegate_client.post(url(key), json=BODY)  # fixture default: delegate token
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden_token"
    assert (await delegate_client.post(url(key), json=BODY, headers=MINDMAP)).status_code == 202
    bare = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delegate_client.app), base_url=BASE_URL
    )
    async with bare:
        assert (await bare.post(url(key), json=BODY)).status_code == 401
        wrong = {"Authorization": "Bearer nope"}
        assert (await bare.post(url(key), json=BODY, headers=wrong)).status_code == 401


async def test_mindmap_token_refused_elsewhere(delegate_client):
    r = await delegate_client.get(TASKS, headers=MINDMAP)
    assert r.status_code == 401
    r = await delegate_client.post(TASKS, json={"title": "t"}, headers=MINDMAP)
    assert r.status_code == 401


async def test_delegate_token_still_opens_other_routes(delegate_client):
    r = await delegate_client.get(TASKS, headers={"Authorization": f"Bearer {DELEGATE_TOKEN}"})
    assert r.status_code == 200


async def test_unknown_task_404(delegate_client):
    r = await delegate_client.post(url("T-999"), json=BODY, headers=MINDMAP)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    r = await delegate_client.post(url("nonsense"), json=BODY, headers=MINDMAP)
    assert r.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"source": "other", "ref": "M-1", "actor_session": None},
        {"source": "mindmap", "ref": "M-0", "actor_session": None},
        {"source": "mindmap", "ref": "x M-1", "actor_session": None},
        {"source": "mindmap", "ref": "M-1", "actor_session": None, "extra": 1},
        {"source": "mindmap", "ref": "M-1", "actor_session": "not-a-uuid"},
        {"source": "mindmap"},
    ],
)
async def test_strict_body(delegate_client, service_client, body):
    key = (await create(service_client))["key"]
    r = await delegate_client.post(url(key), json=body, headers=MINDMAP)
    assert r.status_code == 422


async def test_event_appended_detail_source_ref_only(delegate_client, service_client, conn):
    key = (await create(service_client))["key"]
    body = {"source": "mindmap", "ref": "M-12", "actor_session": S2}
    assert (await delegate_client.post(url(key), json=body, headers=MINDMAP)).status_code == 202
    (row,) = conn.execute(
        "SELECT detail, actor_session, scanned_at FROM events WHERE kind = 'external_change'"
    ).fetchall()
    assert row[0] == {"source": "mindmap", "ref": "M-12"}
    assert row[1] == S2
    assert row[2] is None


async def test_audience_assignee_plus_labels_minus_actor(
    delegate_client, service_client, pool, conn, fake, lookup
):
    a = live(fake, "backend")
    actor = live(fake, "backend")
    other = live(fake, "frontend")
    assignee = live(fake)  # no labels: reached through the assignment only
    key = (await create(service_client, labels=["backend"], assignee_session=assignee))["key"]
    body = {"source": "mindmap", "ref": "M-1", "actor_session": actor}
    await delegate_client.post(url(key), json=body, headers=MINDMAP)
    assert await scan_all(pool, lookup) == 1
    got = sorted(s for _, s in linked(conn))
    assert got == sorted([a, assignee])
    assert other not in got and actor not in got


async def test_assignee_who_is_the_actor_is_skipped(
    delegate_client, service_client, pool, conn, fake, lookup
):
    key = (await create(service_client, assignee_session=S1))["key"]
    body = {"source": "mindmap", "ref": "M-1", "actor_session": S1}
    await delegate_client.post(url(key), json=body, headers=MINDMAP)
    await scan_all(pool, lookup)
    assert linked(conn) == []


async def test_linked_dedup_pending(delegate_client, service_client, pool, conn, fake, lookup):
    live(fake, "backend")
    key = (await create(service_client, labels=["backend"], assignee_session=S3))["key"]
    for ref in ("M-1", "M-2"):
        body = {"source": "mindmap", "ref": ref, "actor_session": None}
        await delegate_client.post(url(key), json=body, headers=MINDMAP)
    assert await scan_all(pool, lookup) >= 2
    rows = conn.execute(
        "SELECT session_id, count(*) FROM notices WHERE reason = 'linked' AND status = 'pending'"
        " GROUP BY session_id"
    ).fetchall()
    assert len(rows) == 2 and all(n == 1 for _, n in rows)


async def test_nothing_scanned_when_no_event(pool, lookup):
    async with pool.connection() as conn:
        assert await scan_external_events(conn, lookup) is None


def test_notice_text_unchanged():
    assert (
        notice_text("T-5", "https://tasks.example.test/")
        == "Task T-5 was updated: https://tasks.example.test/tasks/T-5"
    )


async def test_canary_token_absent(settings, pool, service_client, conn, caplog):
    app = create_delegate_app(settings, pool)
    key = (await create(service_client))["key"]
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL, headers=MINDMAP
    ) as c:
        ok = await c.post(url(key), json=BODY)
        bad = await c.get(TASKS)
    assert MINDMAP_TOKEN not in ok.text + bad.text + str(dict(ok.headers))
    assert MINDMAP_TOKEN not in caplog.text
    assert MINDMAP_TOKEN not in repr(settings)
    for table in ("events", "notices", "tasks"):
        dump = conn.execute(f"SELECT t::text FROM {table} t").fetchall()
        assert MINDMAP_TOKEN not in str(dump)
