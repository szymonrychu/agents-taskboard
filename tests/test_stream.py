import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn
from agents_common.db import listen
from test_api_tasks import PREFIX, TASKS, agent, create

from agents_taskboard.api import stream
from agents_taskboard.api.app import create_service_app, create_web_app

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
async def open_stream(app, base: str) -> AsyncIterator[SSE]:
    async with (
        httpx.AsyncClient(base_url=base, timeout=15) as c,
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
        assert json.loads(frame["data"]) == {"key": key}
        # the shared listener reported it once; the poll must not repeat it
        with pytest.raises(TimeoutError):
            await sse.next(timeout=0.8)


async def test_stream_payload_is_key_only(web_app, service_client):
    key = (await create(service_client, title="secret title", body="secret body"))["key"]
    async with serve(web_app) as base, open_stream(web_app, base) as sse:
        await service_client.post(
            f"{TASKS}/{key}/comments", json={"body": "secret comment"}, headers=agent()
        )
        frame = await sse.next()
        assert set(frame) == {"event", "data"}
        assert json.loads(frame["data"]) == {"key": key}
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
        assert json.loads(frame["data"]) == {"key": key}


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
        assert json.loads(frame["data"]) == {"key": key}
