from typing import Annotated

import httpx
from fastapi import Depends, FastAPI

from agents_taskboard.api.actors import Actor, actor_from_service, actor_from_web
from agents_taskboard.api.app import PREFIX

AUTH = {"Authorization": "Bearer relay-secret"}
AGENT = {"X-Orch-Session": "11111111-1111-1111-1111-111111111111", "X-Orch-Slug": "mx-99"}


def mount_probe(client: httpx.AsyncClient, dependency) -> None:
    app: FastAPI = client.app  # type: ignore[attr-defined]

    @app.get(f"{PREFIX}/_probe")
    async def probe(actor: Annotated[Actor, Depends(dependency)]) -> dict:
        return {"kind": actor.kind, "session_id": actor.session_id, "slug": actor.slug}


async def test_service_call_without_relay_bearer_is_401(service_client):
    mount_probe(service_client, actor_from_service)
    for headers in ({}, AGENT, {"Authorization": "Bearer wrong", **AGENT}):
        r = await service_client.get(f"{PREFIX}/_probe", headers=headers)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"


async def test_session_header_without_bearer_ignored_on_web(web_client):
    mount_probe(web_client, actor_from_web)
    r = await web_client.get(f"{PREFIX}/_probe", headers=AGENT)
    assert r.status_code == 200
    assert r.json() == {"kind": "user", "session_id": None, "slug": None}


async def test_service_call_missing_session_header_is_400(service_client):
    mount_probe(service_client, actor_from_service)
    r = await service_client.get(f"{PREFIX}/_probe", headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "actor_required"


async def test_service_call_with_relay_bearer_reads_agent_identity(service_client):
    mount_probe(service_client, actor_from_service)
    r = await service_client.get(f"{PREFIX}/_probe", headers={**AUTH, **AGENT})
    assert r.status_code == 200
    assert r.json() == {
        "kind": "agent",
        "session_id": AGENT["X-Orch-Session"],
        "slug": "mx-99",
    }


async def test_probes_are_open_on_both_apps(web_client, service_client):
    for client in (web_client, service_client):
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200


async def test_web_app_refuses_foreign_source(settings, pool):
    from agents_taskboard.api.app import create_web_app

    app = create_web_app(settings, pool)
    transport = httpx.ASGITransport(app=app, client=("10.9.9.9", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get(f"{PREFIX}/anything")).status_code == 403
        assert (await client.get("/healthz")).status_code == 200
