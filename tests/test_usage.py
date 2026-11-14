"""UsageClient and usage_gate against a mock agents-usage."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agents_taskboard.usage import UsageClient, UsageReading, usage_gate

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
TOKEN = "".join(["usage", "-", "fixture", "-", "value"])


def window(percent, resets_at=None, stale=False):
    return {
        "percent": percent,
        "resets_at": (resets_at or NOW + timedelta(hours=2)).isoformat(),
        "stale": stale,
    }


def client(handler, clock=None, cache_seconds=30.0):
    return UsageClient(
        "http://usage.test/",
        TOKEN,
        cache_seconds=cache_seconds,
        transport=httpx.MockTransport(handler),
        clock=clock or (lambda: 0.0),
        now=lambda: NOW,
    )


def ok(body):
    return lambda request: httpx.Response(200, json=body)


async def test_current_parses_both_windows():
    c = client(ok({"windows": {"five_hour": window(40.0), "seven_day": window(70.0)}}))
    assert await c.current() == UsageReading(five_hour=40.0, seven_day=70.0)
    await c.aclose()


async def test_reset_window_counts_zero():
    past = NOW - timedelta(minutes=1)
    c = client(ok({"windows": {"five_hour": window(99.0, past), "seven_day": window(70.0)}}))
    assert await c.current() == UsageReading(five_hour=0.0, seven_day=70.0)
    await c.aclose()


async def test_stale_reading_still_counts():
    c = client(ok({"windows": {"five_hour": window(90.0, stale=True), "seven_day": window(5)}}))
    assert await c.current() == UsageReading(five_hour=90.0, seven_day=5.0)
    await c.aclose()


async def test_missing_metric_is_none():
    c = client(ok({"windows": {"five_hour": window(40.0)}}))
    assert await c.current() == UsageReading(five_hour=40.0, seven_day=None)
    await c.aclose()


async def test_unreachable_is_none_and_cached():
    calls = []
    now = [0.0]

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("boom http://usage.test/secret-path")

    c = client(handler, clock=lambda: now[0])
    assert await c.current() is None
    assert await c.current() is None
    assert len(calls) == 1
    now[0] = 31.0
    assert await c.current() is None
    assert len(calls) == 2
    await c.aclose()


async def test_success_is_cached_within_window():
    calls = []

    def handler(request):
        calls.append(request)
        body = {"windows": {"five_hour": window(1), "seven_day": window(2)}}
        return httpx.Response(200, json=body)

    c = client(handler)
    await c.current()
    await c.current()
    assert len(calls) == 1
    await c.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={}),
        httpx.Response(401, json={}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"windows": "nope"}),
        httpx.Response(200, json={"windows": {"five_hour": {"percent": "x"}}}),
        httpx.Response(
            200,
            content=(
                b'{"windows": {"five_hour": {"percent": NaN, "resets_at": "2099-01-01T00:00:00Z"}}}'
            ),
        ),
        httpx.Response(
            200,
            content=(
                b'{"windows": {"five_hour": {"percent": Infinity, '
                b'"resets_at": "2099-01-01T00:00:00Z"}}}'
            ),
        ),
        httpx.Response(200, json={"windows": {"five_hour": window(-1.0)}}),
        httpx.Response(200, json={"windows": {"five_hour": window(100.5)}}),
    ],
)
async def test_non_200_and_malformed_are_none(response):
    c = client(lambda request: response)
    assert await c.current() is None
    await c.aclose()


async def test_sends_bearer_token_and_path():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"windows": {}})

    c = client(handler)
    await c.current()
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert str(seen[0].url) == "http://usage.test/usage/v1/current"
    await c.aclose()


async def test_failure_log_has_no_url_or_token(caplog):
    def handler(request):
        raise httpx.ConnectError(f"boom http://usage.test/secret-path {TOKEN}")

    c = client(handler)
    with caplog.at_level("DEBUG"):
        await c.current()
    assert "secret-path" not in caplog.text and TOKEN not in caplog.text
    await c.aclose()


async def test_token_is_stripped_in_bearer():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"windows": {}})

    c = UsageClient(
        "http://usage.test", TOKEN + "\n", transport=httpx.MockTransport(handler), now=lambda: NOW
    )
    await c.current()
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    await c.aclose()


async def test_timeout_is_none(caplog):
    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"windows": {}})

    c = UsageClient(
        "http://usage.test",
        TOKEN,
        timeout=0.05,
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
    )
    with caplog.at_level("DEBUG"):
        assert await c.current() is None
    assert TOKEN not in caplog.text
    await c.aclose()


async def test_concurrent_current_sends_one_request():
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.05)
        return httpx.Response(
            200, json={"windows": {"five_hour": window(1), "seven_day": window(2)}}
        )

    c = client(handler)
    results = await asyncio.gather(*(c.current() for _ in range(5)))
    assert len(calls) == 1
    assert all(r == UsageReading(1.0, 2.0) for r in results)
    await c.aclose()


@pytest.mark.parametrize(
    ("reading", "limit", "expected"),
    [
        (None, 80, "usage_unknown"),
        (UsageReading(10, None), 80, "usage_unknown"),
        (UsageReading(None, 10), 80, "usage_unknown"),
        (UsageReading(79.9, 10), 80, "ok"),
        (UsageReading(80, 10), 80, "usage_high"),
        (UsageReading(10, 95), 80, "usage_high"),
    ],
)
def test_usage_gate_table(reading, limit, expected):
    assert usage_gate(reading, limit) == expected
