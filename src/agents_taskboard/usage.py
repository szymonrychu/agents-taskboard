"""Client of agents-usage for the autostart gate.

`UsageClient.current` answers the two gated windows as percentages, or None when agents-usage
has no usable answer (the gate then reads usage_unknown and never starts a session). The window
rule mirrors the orchestrator usage gate: a window whose `resets_at` has passed counts as 0.0, a
stale reading still counts, an absent window is None.
"""

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
)

__all__ = ["CURRENT_PATH", "GATED", "UsageClient", "UsageReading", "usage_gate"]

log = logging.getLogger(__name__)

CURRENT_PATH = "/usage/v1/current"
GATED = ("five_hour", "seven_day")


@dataclass(frozen=True)
class UsageReading:
    five_hour: float | None
    seven_day: float | None


class _Window(BaseModel):
    model_config = ConfigDict(extra="ignore")

    percent: StrictFloat | StrictInt
    resets_at: datetime
    stale: StrictBool = False

    @field_validator("percent")
    @classmethod
    def _sane_percent(cls, value: float) -> float:
        # NaN would read "ok" in the gate (NaN >= limit is False): a corrupt body fails closed.
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("percent must be finite and within 0..100")
        return value


class _Current(BaseModel):
    model_config = ConfigDict(extra="ignore")

    windows: dict[str, _Window]


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def usage_gate(
    reading: UsageReading | None, max_usage_pct: int
) -> Literal["ok", "usage_high", "usage_unknown"]:
    """unknown when there is no reading or either metric is missing; high when either metric is
    at or above the project's ceiling; else ok."""
    if reading is None or reading.five_hour is None or reading.seven_day is None:
        return "usage_unknown"
    if reading.five_hour >= max_usage_pct or reading.seven_day >= max_usage_pct:
        return "usage_high"
    return "ok"


class UsageClient:
    """Reads agents-usage, caching the answer and a failure just as long for `cache_seconds`.
    One refresh at a time: a burst at expiry sends one request."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        cache_seconds: float = 30.0,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._url = url.rstrip("/") + CURRENT_PATH
        self._timeout = timeout
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._now = now
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token.strip()}"},
            timeout=timeout,
            transport=transport,
        )
        self._lock = asyncio.Lock()
        self._fetched_at: float | None = None
        self._reading: UsageReading | None = None

    def _fresh(self) -> bool:
        return (
            self._fetched_at is not None and self._clock() - self._fetched_at < self._cache_seconds
        )

    async def current(self) -> UsageReading | None:
        if self._fresh():
            return self._reading
        async with self._lock:
            if not self._fresh():
                self._reading = await self._fetch()
                self._fetched_at = self._clock()
            return self._reading

    async def _fetch(self) -> UsageReading | None:
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.get(self._url)
            response.raise_for_status()
            current = _Current.model_validate_json(response.content)
        except (httpx.HTTPError, TimeoutError, ValidationError) as exc:
            # Never the exception text: an httpx message can carry the request URL.
            log.warning(
                "usage unavailable, the gate reads unknown for %ss: %s",
                self._cache_seconds,
                type(exc).__name__,
            )
            return None
        now = self._now()
        values: dict[str, float | None] = {}
        for metric in GATED:
            window = current.windows.get(metric)
            if window is None:
                values[metric] = None
            else:
                values[metric] = 0.0 if _utc(window.resets_at) <= now else float(window.percent)
        return UsageReading(five_hour=values["five_hour"], seven_day=values["seven_day"])

    async def aclose(self) -> None:
        await self._client.aclose()
