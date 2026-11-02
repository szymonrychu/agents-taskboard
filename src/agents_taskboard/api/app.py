"""The FastAPI apps: web listener (users, behind the source allowlist) and service listener
(orchestrator relay, behind the bearer gate). Both share the error shape and probes."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agents_common.db import listen
from agents_common.service_gate import EXEMPT_PATHS, ServiceTokenGate
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_taskboard.api.actors import actor_from_service, actor_from_web
from agents_taskboard.api.decisions import build_router as build_decision_router
from agents_taskboard.api.errors import ApiError, error_response
from agents_taskboard.api.stream import DEFAULT_POLL_SECONDS, ListenFn, TaskChangeHub
from agents_taskboard.api.stream import build_router as build_stream_router
from agents_taskboard.api.tasks import build_router
from agents_taskboard.api.todos import build_router as build_todo_router
from agents_taskboard.settings import Settings

log = logging.getLogger(__name__)

PREFIX = "/taskboard/api/v1"


class SourceAllowlist:
    """Refuses peers outside the allowlist. Only the socket peer is read: Forwarded,
    X-Forwarded-For and X-Real-IP are never consulted."""

    def __init__(self, app: ASGIApp, networks: list) -> None:
        self.app = app
        self.networks = list(networks)

    def allowed(self, scope: Scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            addr = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return any(addr.version == n.version and addr in n for n in self.networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if (scope["type"] == "http" and scope.get("path") in EXEMPT_PATHS) or self.allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = error_response(403, "forbidden", "source address is not allowed")
        await response(scope, receive, send)


def _base_app(settings: Settings, pool: AsyncConnectionPool, title: str) -> FastAPI:
    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.pool = pool

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{where}: {first.get('msg', 'invalid request')}" if where else "invalid request"
        return error_response(422, "invalid_request", message)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception:
            log.exception("readiness check failed")
            return error_response(503, "not_ready", "database is not reachable")
        return JSONResponse({"status": "ok"})

    return app


def create_web_app(
    settings: Settings,
    pool: AsyncConnectionPool,
    stream_poll_seconds: float = DEFAULT_POLL_SECONDS,
    listen_fn: ListenFn = listen,
) -> FastAPI:
    """The user-facing app: source allowlist, no bearer, the answer route and the SSE stream
    live here."""
    app = _base_app(settings, pool, "agents-taskboard")
    app.state.hub = TaskChangeHub(
        settings.database_url.get_secret_value(), pool, stream_poll_seconds, listen_fn
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await app.state.hub.stop()

    app.router.lifespan_context = lifespan
    app.include_router(build_stream_router(), prefix=PREFIX)
    app.include_router(build_router(actor_from_web), prefix=PREFIX)
    app.include_router(build_todo_router(actor_from_web), prefix=PREFIX)
    app.include_router(build_decision_router(actor_from_web, answerable=True), prefix=PREFIX)
    app.add_middleware(SourceAllowlist, networks=settings.api_networks())
    return app


def create_service_app(settings: Settings, pool: AsyncConnectionPool) -> FastAPI:
    """The relay-facing app: bearer gate, agent identity from forwarded headers, no answer route."""
    app = _base_app(settings, pool, "agents-taskboard-service")
    app.include_router(build_router(actor_from_service), prefix=PREFIX)
    app.include_router(build_todo_router(actor_from_service), prefix=PREFIX)
    app.include_router(build_decision_router(actor_from_service), prefix=PREFIX)
    app.add_middleware(ServiceTokenGate, tokens=[settings.relay_token.get_secret_value()])
    return app
