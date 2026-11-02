import uuid
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import psycopg
import pytest
from agents_common.db.pool import open_pool
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from agents_taskboard.api.app import create_service_app, create_web_app
from agents_taskboard.settings import Settings

PACKAGE = "agents_taskboard"
RELAY_TOKEN = "relay-secret"
BASE_URL = "http://test"


@pytest.fixture
def db_url(migrated_db: Callable[[str], str]) -> str:
    """A fresh database cloned from the migrated template."""
    return migrated_db(PACKAGE)


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        yield connection


@pytest.fixture
def empty_db_url(pg_url: str) -> Iterator[str]:
    """A fresh database with no schema at all."""
    name = f"e_{uuid.uuid4().hex}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        yield f"{pg_url.rsplit('/', 1)[0]}/{name}"
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings(
        database_url=db_url,
        orchestrator_url="http://orchestrator.invalid",
        orchestrator_token="t",
        public_base_url="https://tasks.example.test",
        relay_token=RELAY_TOKEN,
    )


@pytest.fixture
async def pool(db_url: str) -> AsyncIterator[AsyncConnectionPool]:
    opened = await open_pool(db_url)
    yield opened
    await opened.close()


@pytest.fixture
async def web_client(
    settings: Settings, pool: AsyncConnectionPool
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_web_app(settings, pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


@pytest.fixture
async def service_client(
    settings: Settings, pool: AsyncConnectionPool
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_service_app(settings, pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client
