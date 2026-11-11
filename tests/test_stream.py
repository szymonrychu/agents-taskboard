import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn
from agents_common.db import listen
from test_api_tasks import PREFIX, TASKS, agent, create
from test_scope import ORCH, orchestrator

from agents_taskboard.api import stream
from agents_taskboard.api.app import create_service_app, create_web_app
from agents_taskboard.projects import ProjectLookup

STREAM = f"{PREFIX}/stream"


async def never_listen(url: str, channel: str) -> AsyncIterator[str]:
    """A LISTEN connection that is down: it never yields a notification."""
    await asyncio.Event().wait()
    yield ""


@contextlib.asynccontextmanager
async def serve(app) -> AsyncIterator[str]:
    """Real uvicorn on an ephemeral port: ASGITransport cannot stream incrementally."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


class SSE:
    def __init__(self, resp: httpx.Response):
        self._lines = resp.aiter_lines()

    async def next(self, timeout: float = 10) -> dict:
        async def read() -> dict:
            frame: dict = {}
            async for line in self._lines:
                if line == "":
                    if frame:
                        return frame
                    continue
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(":")
                frame[key] = value.removeprefix(" ")
            raise AssertionError("stream ended")

        return await asyncio.wait_for(read(), timeout)


@contextlib.asynccontextmanager
async def open_stream(app, base: str, token: str | None = None) -> AsyncIterator[SSE]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx.AsyncClient(base_url=base, timeout=15, headers=headers) as c,
        c.stream("GET", STREAM) as resp,
    ):
        assert resp.status_code == 200
        for _ in range(200):
            if app.state.hub.subscriber_count() == 1:
                break
            await asyncio.sleep(0.02)
        assert app.state.hub.subscriber_count() == 1
        yield SSE(resp)


@pytest.fixture
async def web_app(settings, pool):
    return create_web_app(settings, pool, stream_poll_seconds=0.2)


async def test_stream_emits_task_changed_after_comment(web_app, service_client):
    key = (await create(service_client))["key"]
    async with serve(web_app) as base, open_stream(web_app, base) as sse:
        r = await service_client.post(
            f"{TASKS}/{key}/comments", json={"body": "hello"}, headers=agent()
        )
        assert r.status_code == 201
        frame = await sse.next()
        assert frame["event"] == "task_changed"
        assert json.loads(frame["data"]) == {"key": key, "project": "default"}
        # the shared listener reported it once; the poll must not repeat it
        with pytest.raises(TimeoutError):
            await sse.next(timeout=0.8)


async def test_task_changed_carries_project(web_app, service_client):
    service_client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())
    key = (await create(service_client, agent(projects="a"), project="a"))["key"]
    async with serve(web_app) as base, open_stream(web_app, base) as sse:
        await service_client.post(
            f"{TASKS}/{key}/comments", json={"body": "hi"}, headers=agent(projects="a")
        )
        frame = await sse.next()
        assert json.loads(frame["data"]) == {"key": key, "project": "a"}


async def test_stream_project_filter(web_app, service_client):
    service_client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())
    a = (await create(service_client, agent(projects="a"), project="a"))["key"]
    b = (await create(service_client, agent(projects="b"), project="b"))["key"]
    async with (
        serve(web_app) as base,
        httpx.AsyncClient(base_url=base, timeout=15) as c,
        c.stream("GET", STREAM, params={"project": "b"}) as resp,
    ):
        for _ in range(200):
            if web_app.state.hub.subscriber_count() == 1:
                break
            await asyncio.sleep(0.02)
        sse = SSE(resp)
        for key, proj in ((a, "a"), (b, "b")):
            await service_client.post(
                f"{TASKS}/{key}/comments", json={"body": "x"}, headers=agent(projects=proj)
            )
        frame = await sse.next()
        assert json.loads(frame["data"]) == {"key": b, "project": "b"}
        with pytest.raises(TimeoutError):
            await sse.next(timeout=0.8)


async def test_stream_payload_is_key_and_project_only(web_app, service_client):
    key = (await create(service_client, title="secret title", body="secret body"))["key"]
    async with serve(web_app) as base, open_stream(web_app, base) as sse:
        await service_client.post(
            f"{TASKS}/{key}/comments", json={"body": "secret comment"}, headers=agent()
        )
        frame = await sse.next()
        assert set(frame) == {"event", "data", "id"}
        assert json.loads(frame["data"]) == {"key": key, "project": "default"}
        assert "secret" not in frame["data"]


async def test_polling_fallback_when_listener_down(settings, pool, service_client):
    app = create_web_app(settings, pool, stream_poll_seconds=0.2, listen_fn=never_listen)
    key = (await create(service_client))["key"]
    async with serve(app) as base, open_stream(app, base) as sse:
        await service_client.post(
            f"{TASKS}/{key}/comments", json={"body": "missed notify"}, headers=agent()
        )
        frame = await sse.next()
        assert frame["event"] == "task_changed"
        assert json.loads(frame["data"]) == {"key": key, "project": "default"}


async def test_hub_stops_with_last_subscriber_and_on_shutdown(web_app, service_client):
    hub = web_app.state.hub
    assert not hub.running
    async with serve(web_app) as base:
        async with open_stream(web_app, base):
            assert hub.running
        for _ in range(200):
            if not hub.running:
                break
            await asyncio.sleep(0.02)
        assert not hub.running
        async with open_stream(web_app, base):
            assert hub.running
    assert not hub.running


async def test_service_app_does_not_serve_stream(settings, pool):
    app = create_service_app(settings, pool)
    assert not hasattr(app.state, "hub")
    assert all(not getattr(r, "path", "").endswith("/stream") for r in app.routes)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(STREAM, headers=agent())
        assert r.status_code == 404


async def test_listener_restarts_after_unexpected_error(
    settings, pool, service_client, monkeypatch
):
    monkeypatch.setattr(stream, "LISTEN_RETRY_SECONDS", 0.05)
    calls = []

    async def flaky_listen(url: str, channel: str) -> AsyncIterator[str]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        async for payload in listen(url, channel):
            yield payload

    # poll far slower than the test waits, so only a live listener can deliver
    app = create_web_app(settings, pool, stream_poll_seconds=30, listen_fn=flaky_listen)
    key = (await create(service_client))["key"]
    async with serve(app) as base, open_stream(app, base) as sse:
        await asyncio.sleep(0.5)
        assert len(calls) >= 2
        await service_client.post(
            f"{TASKS}/{key}/comments", json={"body": "after restart"}, headers=agent()
        )
        frame = await sse.next()
        assert json.loads(frame["data"]) == {"key": key, "project": "default"}


SPECS = f"{PREFIX}/specs"


def last_event_id(conn) -> int:
    return conn.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0]


@contextlib.asynccontextmanager
async def open_stream_at(app, base: str, headers=None, params=None) -> AsyncIterator[SSE]:
    async with (
        httpx.AsyncClient(base_url=base, timeout=15) as c,
        c.stream("GET", STREAM, headers=headers or {}, params=params or {}) as resp,
    ):
        assert resp.status_code == 200
        yield SSE(resp)


async def make_spec(service_client, headers=None, name="s") -> str:
    r = await service_client.post(
        SPECS,
        json={"name": name, "title": "t", "docs": [{"path": "plan.md", "body": "b"}]},
        headers=headers or agent(),
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


async def comment(service_client, key: str, headers=None) -> None:
    r = await service_client.post(
        f"{TASKS}/{key}/comments", json={"body": "x"}, headers=headers or agent()
    )
    assert r.status_code == 201


async def no_more(sse: SSE) -> None:
    """Nothing else arrives; it ends the reader, so it is the last read of a stream."""
    with pytest.raises(TimeoutError):
        await sse.next(timeout=0.8)


async def test_every_sse_event_has_id(web_app, service_client, conn):
    key = (await create(service_client))["key"]
    spec = await make_spec(service_client)
    async with serve(web_app) as base, open_stream(web_app, base) as sse:
        await comment(service_client, key)
        frame = await sse.next()
        assert frame["event"] == "task_changed"
        assert int(frame["id"]) == last_event_id(conn)
        r = await service_client.put(
            f"{SPECS}/{spec}/docs/plan.md", json={"body": "v2", "base_version": 1}, headers=agent()
        )
        assert r.status_code == 201
        frame = await sse.next()
        assert frame["event"] == "spec_changed"
        assert int(frame["id"]) == last_event_id(conn)
        assert json.loads(frame["data"]) == {
            "key": spec,
            "project": "default",
            "path": "plan.md",
            "version": 2,
        }


async def test_spec_comment_event_carries_comment_id(web_app, service_client):
    spec = await make_spec(service_client)
    async with serve(web_app) as base, open_stream(web_app, base) as sse:
        r = await service_client.post(
            f"{SPECS}/{spec}/comments", json={"body": "secret"}, headers=agent()
        )
        frame = await sse.next()
        assert json.loads(frame["data"]) == {
            "key": spec,
            "project": "default",
            "comment_id": r.json()["id"],
        }


async def test_last_event_id_replays_later_changes_once_per_key(web_app, service_client, conn):
    one = (await create(service_client))["key"]
    two = (await create(service_client))["key"]
    spec = await make_spec(service_client)
    mark = last_event_id(conn)
    await comment(service_client, one)
    await comment(service_client, two)
    await comment(service_client, one)
    await service_client.put(
        f"{SPECS}/{spec}/docs/plan.md", json={"body": "v2", "base_version": 1}, headers=agent()
    )
    async with (
        serve(web_app) as base,
        open_stream_at(web_app, base, headers={"Last-Event-ID": str(mark)}) as sse,
    ):
        frames = [await sse.next() for _ in range(3)]
        assert [json.loads(f["data"])["key"] for f in frames] == [two, one, spec]
        ids = [int(f["id"]) for f in frames]
        assert ids == sorted(ids) and ids[-1] == last_event_id(conn)
        # the next frame is the new change, not a repeat of a replayed one
        await comment(service_client, two)
        frame = await sse.next()
        assert json.loads(frame["data"])["key"] == two
        assert int(frame["id"]) == last_event_id(conn)
        await no_more(sse)


async def test_after_id_query_replays(web_app, service_client, conn):
    key = (await create(service_client))["key"]
    mark = last_event_id(conn)
    await comment(service_client, key)
    async with (
        serve(web_app) as base,
        open_stream_at(web_app, base, params={"after_id": mark}) as sse,
    ):
        frame = await sse.next()
        assert json.loads(frame["data"]) == {"key": key, "project": "default"}
        assert int(frame["id"]) == last_event_id(conn)
        await no_more(sse)


async def test_both_cursors_larger_wins(web_app, service_client, conn):
    one = (await create(service_client))["key"]
    two = (await create(service_client))["key"]
    low = last_event_id(conn)
    await comment(service_client, one)
    high = last_event_id(conn)
    await comment(service_client, two)
    for headers, params in (
        ({"Last-Event-ID": str(low)}, {"after_id": high}),
        ({"Last-Event-ID": str(high)}, {"after_id": low}),
    ):
        async with (
            serve(web_app) as base,
            open_stream_at(web_app, base, headers=headers, params=params) as sse,
        ):
            frame = await sse.next()
            assert json.loads(frame["data"])["key"] == two
            await no_more(sse)


async def test_reset_when_id_below_oldest(web_app, service_client, conn):
    key = (await create(service_client))["key"]
    await comment(service_client, key)
    old = last_event_id(conn)
    await comment(service_client, key)
    conn.execute("DELETE FROM events WHERE id <= %s", (old,))
    async with (
        serve(web_app) as base,
        open_stream_at(web_app, base, headers={"Last-Event-ID": "0"}) as sse,
    ):
        frame = await sse.next()
        assert frame["event"] == "reset"
        assert int(frame["id"]) == last_event_id(conn)
        await no_more(sse)
    # an id at or past the oldest kept row replays instead
    async with (
        serve(web_app) as base,
        open_stream_at(web_app, base, headers={"Last-Event-ID": str(old)}) as sse,
    ):
        frame = await sse.next()
        assert frame["event"] == "task_changed"


async def test_replay_respects_project_filter(web_app, service_client, conn):
    service_client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())
    a = (await create(service_client, agent(projects="a"), project="a"))["key"]
    b = (await create(service_client, agent(projects="b"), project="b"))["key"]
    await make_spec(service_client, agent(projects="a"), name="sa")
    mark = 0
    await comment(service_client, a, agent(projects="a"))
    await comment(service_client, b, agent(projects="b"))
    async with (
        serve(web_app) as base,
        open_stream_at(
            web_app, base, headers={"Last-Event-ID": str(mark)}, params={"project": "b"}
        ) as sse,
    ):
        frame = await sse.next()
        assert json.loads(frame["data"]) == {"key": b, "project": "b"}
        await no_more(sse)


async def test_deleted_spec_event_has_deleted_true(web_app, service_client, web_client):
    spec = await make_spec(service_client)
    async with serve(web_app) as base, open_stream(web_app, base) as sse:
        r = await web_client.delete(f"{SPECS}/{spec}")
        assert r.status_code == 204
        frame = await sse.next()
        assert frame["event"] == "spec_changed"
        assert json.loads(frame["data"]) == {"key": spec, "project": "default", "deleted": True}


async def test_malformed_last_event_id_is_ignored(web_app, service_client):
    key = (await create(service_client))["key"]
    async with (
        serve(web_app) as base,
        open_stream_at(web_app, base, headers={"Last-Event-ID": "nope"}) as sse,
    ):
        for _ in range(200):
            if web_app.state.hub.subscriber_count() == 1:
                break
            await asyncio.sleep(0.02)
        await comment(service_client, key)
        frame = await sse.next()
        assert json.loads(frame["data"])["key"] == key
