"""The listeners really serve over a socket and stop on the stop event."""

import asyncio
import socket

import httpx
import pytest

from agents_taskboard import runtime
from agents_taskboard.api.app import create_service_app, create_web_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("factory", [create_web_app, create_service_app])
async def test_serves_readyz_then_stops(settings, factory):
    port = free_port()
    stop = asyncio.Event()
    task = asyncio.create_task(runtime._serve(settings, stop, factory, port))
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            for _ in range(100):
                try:
                    response = await client.get("/readyz")
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("listener did not come up")
        assert response.status_code == 200
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
