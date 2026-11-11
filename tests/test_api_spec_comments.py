"""Spec comments, resolve, and the user-only accept and dismiss of a suggestion (task-plans A3,
base design 3.5 "Accept or dismiss a suggestion")."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from test_api_specs import SPECS, doc_url, make_spec
from test_api_tasks import S1, agent
from test_scope import ORCH, orchestrator

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.errors import ApiError, error_response
from agents_taskboard.api.specs import build_spec_router
from agents_taskboard.projects import ProjectLookup

KIB = 1024
ATTACHMENT = {"kind": "page", "ref": "pages/1.png"}


@pytest.fixture(autouse=True)
def fake_projects(service_client, web_client, delegate_client):
    for client in (service_client, web_client, delegate_client):
        client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())


def comments_url(key: str) -> str:
    return f"{SPECS}/{key}/comments"


async def comment(client, key: str, headers=None, **body) -> dict:
    body.setdefault("body", "looks off")
    r = await client.post(comments_url(key), json=body, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


def spec_events(conn) -> list[tuple]:
    return conn.execute(
        "SELECT kind, detail FROM events WHERE spec_id IS NOT NULL ORDER BY id"
    ).fetchall()


async def test_create_with_anchor_suggestion_and_attachments(service_client, web_client, conn):
    spec = await make_spec(service_client)
    created = await comment(
        service_client,
        spec["key"],
        agent(),
        path="plan.md",
        version=1,
        anchor="L1-L2",
        suggestion="# Better\n",
        attachments=[ATTACHMENT],
    )
    assert created["path"] == "plan.md" and created["version"] == 1
    assert created["anchor"] == "L1-L2"
    assert created["suggestion"] == "# Better\n" and created["suggestion_status"] == "open"
    assert created["attachments"] == [ATTACHMENT]
    assert created["created_by"] == {"kind": "agent", "session": S1, "slug": "mx-1"}
    assert created["resolved_at"] is None
    r = await web_client.get(comments_url(spec["key"]))
    assert [c["id"] for c in r.json()["items"]] == [created["id"]]
    r = await web_client.get(f"{SPECS}/{spec['key']}")
    assert r.json()["open_comments"] == 1
    assert spec_events(conn)[-1] == ("comment_added", {"comment_id": created["id"]})


async def test_caps(service_client):
    spec = await make_spec(service_client)
    url = comments_url(spec["key"])
    ok = await service_client.post(url, json={"body": "x" * (16 * KIB)}, headers=agent())
    assert ok.status_code == 201
    r = await service_client.post(url, json={"body": "x" * (16 * KIB + 1)}, headers=agent())
    assert (r.status_code, r.json()["error"]["code"]) == (413, "payload_too_large")
    r = await service_client.post(
        url,
        json={"body": "b", "path": "plan.md", "suggestion": "x" * (192 * KIB + 1)},
        headers=agent(),
    )
    assert r.status_code == 413
    r = await service_client.post(
        url,
        json={"body": "b", "path": "plan.md", "suggestion": "x" * (192 * KIB)},
        headers=agent(),
    )
    assert r.status_code == 201
    r = await service_client.post(
        url, json={"body": "b", "attachments": [ATTACHMENT] * 11}, headers=agent()
    )
    assert r.status_code == 422
    r = await service_client.post(
        url, json={"body": "b", "attachments": [ATTACHMENT] * 10}, headers=agent()
    )
    assert r.status_code == 201
    for bad in ({"body": ""}, {"body": "b", "path": "../x.md"}, {"body": "b", "suggestion": "s"}):
        r = await service_client.post(url, json=bad, headers=agent())
        assert r.status_code == 422, bad


async def test_replay_by_client_key(service_client, web_client, conn):
    spec = await make_spec(service_client)
    url = comments_url(spec["key"])
    first = await service_client.post(url, json={"body": "a", "client_key": "c"}, headers=agent())
    again = await service_client.post(url, json={"body": "b", "client_key": "c"}, headers=agent())
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"] and again.json()["body"] == "a"
    u1 = await web_client.post(url, json={"body": "a", "client_key": "u"})
    u2 = await web_client.post(url, json={"body": "a", "client_key": "u"})
    assert (u1.status_code, u2.status_code) == (201, 200)
    assert conn.execute("SELECT count(*) FROM spec_comments").fetchone()[0] == 2


async def test_list_filters(service_client, web_client):
    spec = await make_spec(service_client, docs=[{"path": "a.md", "body": "a"}])
    one = await comment(service_client, spec["key"], agent(), path="a.md")
    two = await comment(service_client, spec["key"], agent())
    await service_client.post(f"{comments_url(spec['key'])}/{two['id']}/resolve", headers=agent())
    r = await web_client.get(comments_url(spec["key"]), params={"path": "a.md"})
    assert [c["id"] for c in r.json()["items"]] == [one["id"]]
    r = await web_client.get(comments_url(spec["key"]), params={"open": "true"})
    assert [c["id"] for c in r.json()["items"]] == [one["id"]]
    r = await web_client.get(comments_url(spec["key"]), params={"open": "false"})
    assert [c["id"] for c in r.json()["items"]] == [two["id"]]


async def test_resolve_then_409(service_client, conn):
    spec = await make_spec(service_client)
    c = await comment(service_client, spec["key"], agent())
    url = f"{comments_url(spec['key'])}/{c['id']}/resolve"
    r = await service_client.post(url, headers=agent())
    assert r.status_code == 200
    assert r.json()["resolved_at"] is not None
    assert r.json()["resolved_by"] == {"kind": "agent", "session": S1, "slug": "mx-1"}
    r = await service_client.post(url, headers=agent())
    assert (r.status_code, r.json()["error"]["code"]) == (409, "already_resolved")
    r = await service_client.post(f"{comments_url(spec['key'])}/999999/resolve", headers=agent())
    assert r.status_code == 404
    assert spec_events(conn)[-1] == ("comment_resolved", {"comment_id": c["id"]})


async def test_comment_of_other_spec_404(service_client):
    a = await make_spec(service_client, name="a")
    b = await make_spec(service_client, name="b")
    c = await comment(service_client, a["key"], agent())
    r = await service_client.post(f"{comments_url(b['key'])}/{c['id']}/resolve", headers=agent())
    assert r.status_code == 404


async def test_accept_on_relay_404(service_client):
    spec = await make_spec(service_client)
    c = await comment(service_client, spec["key"], agent(), path="plan.md", suggestion="s")
    for action, body in (("accept", {"version": 1}), ("dismiss", None)):
        r = await service_client.post(
            f"{comments_url(spec['key'])}/{c['id']}/{action}", json=body, headers=agent()
        )
        assert r.status_code == 404


async def test_accept_by_user_writes_version(service_client, web_client, conn):
    spec = await make_spec(service_client)
    c = await comment(
        service_client,
        spec["key"],
        agent(),
        path="plan.md",
        suggestion="# Accepted\n",
        attachments=[ATTACHMENT],
    )
    r = await web_client.post(f"{comments_url(spec['key'])}/{c['id']}/accept", json={"version": 1})
    assert r.status_code == 201, r.text
    version, accepted = r.json()["version"], r.json()["comment"]
    assert version["version"] == 2
    assert version["note"] == f"Accepted proposed edit from comment {c['id']}"
    assert version["sources"] == [ATTACHMENT]
    assert version["created_by"]["kind"] == "user"
    assert accepted["suggestion_status"] == "accepted" and accepted["accepted_version"] == 2
    assert accepted["resolved_at"] is not None and accepted["decided_at"] is not None
    doc = (await web_client.get(doc_url(spec["key"]))).json()
    assert doc["body"] == "# Accepted\n"
    kind, detail = spec_events(conn)[-1]
    assert kind == "suggestion_accepted"
    assert detail == {"path": "plan.md", "version": 2, "comment_id": c["id"]}
    assert len([k for k, _ in spec_events(conn) if k != "spec_created"]) == 2


async def test_accept_on_approved_spec_returns_to_review(service_client, web_client):
    spec = await make_spec(service_client)
    await web_client.patch(f"{SPECS}/{spec['key']}", json={"version": 1, "status": "approved"})
    c = await comment(service_client, spec["key"], agent(), path="plan.md", suggestion="s")
    r = await web_client.post(f"{comments_url(spec['key'])}/{c['id']}/accept", json={"version": 1})
    assert r.status_code == 201
    assert (await web_client.get(f"{SPECS}/{spec['key']}")).json()["status"] == "review"


async def test_accept_racing_push(service_client, web_client, conn):
    spec = await make_spec(service_client)
    c = await comment(service_client, spec["key"], agent(), path="plan.md", suggestion="s")
    await service_client.put(
        doc_url(spec["key"]), json={"body": "pushed", "base_version": 1}, headers=agent()
    )
    r = await web_client.post(f"{comments_url(spec['key'])}/{c['id']}/accept", json={"version": 1})
    assert r.status_code == 412
    assert r.json()["error"]["current_version"] == 2
    row = conn.execute(
        "SELECT suggestion_status, resolved_at FROM spec_comments WHERE id = %s", (c["id"],)
    ).fetchone()
    assert row == ("open", None)
    assert (await web_client.get(doc_url(spec["key"]))).json()["body"] == "pushed"


async def test_concurrent_accept_and_push_one_wins(service_client, web_client, conn):
    spec = await make_spec(service_client)
    c = await comment(service_client, spec["key"], agent(), path="plan.md", suggestion="s")
    accept, push = await asyncio.gather(
        web_client.post(f"{comments_url(spec['key'])}/{c['id']}/accept", json={"version": 1}),
        service_client.put(
            doc_url(spec["key"]), json={"body": "p", "base_version": 1}, headers=agent()
        ),
    )
    assert sorted((accept.status_code, push.status_code)) == [201, 412]
    count = conn.execute("SELECT count(*) FROM spec_versions").fetchone()[0]
    assert count == 2


async def test_second_decision_409_and_dismiss(service_client, web_client, conn):
    spec = await make_spec(service_client)
    c = await comment(service_client, spec["key"], agent(), path="plan.md", suggestion="s")
    base = f"{comments_url(spec['key'])}/{c['id']}"
    r = await web_client.post(f"{base}/dismiss")
    assert r.status_code == 200
    assert r.json()["suggestion_status"] == "dismissed" and r.json()["resolved_at"] is not None
    assert spec_events(conn)[-1] == ("suggestion_dismissed", {"comment_id": c["id"]})
    for action, body in (("accept", {"version": 1}), ("dismiss", None)):
        r = await web_client.post(f"{base}/{action}", json=body)
        assert (r.status_code, r.json()["error"]["code"]) == (409, "already_decided")
    accepted = await comment(service_client, spec["key"], agent(), path="plan.md", suggestion="t")
    url = f"{comments_url(spec['key'])}/{accepted['id']}"
    assert (await web_client.post(f"{url}/accept", json={"version": 1})).status_code == 201
    r = await web_client.post(f"{url}/accept", json={"version": 2})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "already_decided")


async def test_no_suggestion_422(service_client, web_client):
    spec = await make_spec(service_client)
    c = await comment(service_client, spec["key"], agent())
    for action, body in (("accept", {"version": 1}), ("dismiss", None)):
        r = await web_client.post(f"{comments_url(spec['key'])}/{c['id']}/{action}", json=body)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "no_suggestion")


async def test_delegate_can_accept(service_client, delegate_client):
    spec = await make_spec(service_client)
    c = await comment(service_client, spec["key"], agent(), path="plan.md", suggestion="s")
    r = await delegate_client.post(
        f"{comments_url(spec['key'])}/{c['id']}/accept", json={"version": 1}
    )
    assert r.status_code == 201


async def test_agent_actor_on_user_router_403():
    """Defensive: an agent actor never reaches accept on a user listener, but if it did the
    route answers 403 user_only."""
    app = FastAPI()
    app.exception_handler(ApiError)(
        lambda _, exc: error_response(exc.status, exc.code, exc.message)
    )
    app.include_router(
        build_spec_router(lambda: Actor(kind="agent", session_id=S1), user_routes=True)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        for action, body in (("accept", {"version": 1}), ("dismiss", None)):
            r = await client.post(f"/specs/S-1/comments/1/{action}", json=body)
            assert (r.status_code, r.json()["error"]["code"]) == (403, "user_only")
