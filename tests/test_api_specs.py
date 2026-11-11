"""Spec and document routes (task-plans A2, base design 4.2)."""

import hashlib

import pytest
from test_api_tasks import S1, S2, agent
from test_scope import ORCH, orchestrator

from agents_taskboard.api.app import PREFIX
from agents_taskboard.projects import ProjectLookup

SPECS = f"{PREFIX}/specs"
CAP = 192 * 1024


@pytest.fixture(autouse=True)
def fake_projects(service_client, web_client, delegate_client):
    for client in (service_client, web_client, delegate_client):
        client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())


async def make_spec(client, headers=None, **body) -> dict:
    body.setdefault("name", "plan-a")
    body.setdefault("title", "A plan")
    body.setdefault("docs", [{"path": "plan.md", "body": "# Plan\n"}])
    r = await client.post(SPECS, json=body, headers=headers if headers is not None else agent())
    assert r.status_code == 201, r.text
    return r.json()


def doc_url(key: str, path: str = "plan.md") -> str:
    return f"{SPECS}/{key}/docs/{path}"


async def test_create_get_and_replay(service_client):
    spec = await make_spec(service_client, client_key="k1", labels=["Plan"])
    assert spec["key"].startswith("S-")
    assert spec["project"] == "default" and spec["status"] == "draft"
    assert spec["labels"] == ["plan"]
    assert spec["url"] == f"https://tasks.example.test/specs/{spec['key']}"
    assert spec["created_by"] == {"kind": "agent", "session": S1, "slug": "mx-1"}
    (doc,) = spec["documents"]
    assert doc["path"] == "plan.md" and doc["version"] == 1
    assert doc["sha256"] == hashlib.sha256(b"# Plan\n").hexdigest()
    assert spec["tasks"] == [] and spec["open_comments"] == 0
    r = await service_client.post(
        SPECS, json={"name": "plan-a", "title": "x", "client_key": "k1"}, headers=agent()
    )
    assert r.status_code == 200 and r.json()["key"] == spec["key"]
    r = await service_client.get(f"{SPECS}/{spec['key']}", headers=agent())
    assert r.status_code == 200 and r.json()["documents"] == spec["documents"]


async def test_create_errors(service_client, web_client):
    await make_spec(service_client)
    r = await service_client.post(SPECS, json={"name": "plan-a", "title": "x"}, headers=agent())
    assert r.status_code == 409 and r.json()["error"]["code"] == "name_taken"
    for bad in ({"name": "Bad", "title": "x"}, {"name": "ok", "title": ""}, {"title": "x"}):
        r = await service_client.post(SPECS, json=bad, headers=agent())
        assert r.status_code == 422, bad
    r = await service_client.post(
        SPECS,
        json={"name": "p", "title": "x", "docs": [{"path": "../a.md", "body": "x"}]},
        headers=agent(),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    r = await web_client.post(SPECS, json={"name": "p", "title": "x", "project": "nope"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_project"


async def test_archived_project_create_422(web_client):
    r = await web_client.post(SPECS, json={"name": "p", "title": "x", "project": "old"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "project_archived"


async def test_create_takes_agent_project_by_default(service_client):
    spec = await make_spec(service_client, agent(projects="a"))
    assert spec["project"] == "a"


async def test_list_filters_and_pages(service_client, web_client):
    await make_spec(service_client, name="one", labels=["x"])
    await make_spec(service_client, agent(projects="a"), name="two")
    await make_spec(service_client, name="three")
    r = await web_client.get(SPECS, params={"limit": 2})
    body = r.json()
    assert [s["name"] for s in body["items"]] == ["three", "two"]
    assert "documents" not in body["items"][0]
    r = await web_client.get(SPECS, params={"limit": 2, "cursor": body["next_cursor"]})
    assert [s["name"] for s in r.json()["items"]] == ["one"] and r.json()["next_cursor"] is None
    r = await web_client.get(SPECS, params={"project": "a"})
    assert [s["name"] for s in r.json()["items"]] == ["two"]
    r = await web_client.get(SPECS, params={"label": "x"})
    assert [s["name"] for s in r.json()["items"]] == ["one"]
    r = await web_client.get(SPECS, params={"name": "three", "status": "draft"})
    assert [s["name"] for s in r.json()["items"]] == ["three"]
    r = await web_client.get(SPECS, params={"cursor": "bogus"})
    assert r.status_code == 422
    r = await service_client.get(SPECS, headers=agent(projects="a"))
    assert [s["name"] for s in r.json()["items"]] == ["two"]


async def test_patch_fields_and_stale(service_client):
    spec = await make_spec(service_client)
    url = f"{SPECS}/{spec['key']}"
    r = await service_client.patch(
        url,
        json={"version": 1, "title": "New", "status": "review", "labels": ["a"]},
        headers=agent(),
    )
    assert r.status_code == 200, r.text
    assert (r.json()["title"], r.json()["status"], r.json()["version"]) == ("New", "review", 2)
    r = await service_client.patch(url, json={"version": 1, "title": "x"}, headers=agent())
    assert r.status_code == 412 and r.json()["error"]["code"] == "stale_version"
    r = await service_client.patch(url, json={"version": 2}, headers=agent())
    assert r.status_code == 422


async def test_agent_approve_403_user_only(service_client):
    spec = await make_spec(service_client)
    r = await service_client.patch(
        f"{SPECS}/{spec['key']}", json={"version": 1, "status": "approved"}, headers=agent()
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "user_only"


async def test_user_approve_200(service_client, web_client, conn):
    spec = await make_spec(service_client)
    r = await web_client.patch(f"{SPECS}/{spec['key']}", json={"version": 1, "status": "approved"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    detail = conn.execute(
        "SELECT detail FROM events WHERE kind = 'spec_updated' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert detail["versions"] == {"plan.md": 1}


async def test_agent_assignee_self_only(service_client, web_client):
    spec = await make_spec(service_client)
    url = f"{SPECS}/{spec['key']}"
    r = await service_client.patch(
        url, json={"version": 1, "assignee_session": S2}, headers=agent()
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "user_only"
    r = await service_client.patch(url, json={"version": 1, "assignee_self": True}, headers=agent())
    assert r.status_code == 200
    assert (r.json()["assignee_session"], r.json()["assignee_slug"]) == (S1, "mx-1")
    r = await service_client.patch(
        url, json={"version": 2, "assignee_session": None}, headers=agent(S2)
    )
    assert r.status_code == 403
    r = await web_client.patch(url, json={"version": 2, "assignee_session": S2})
    assert r.status_code == 200 and r.json()["assignee_session"] == S2
    r = await service_client.patch(
        url, json={"version": 3, "assignee_session": None}, headers=agent(S2)
    )
    assert r.status_code == 200 and r.json()["assignee_session"] is None


async def test_patch_project_move(web_client, service_client):
    spec = await make_spec(service_client)
    url = f"{SPECS}/{spec['key']}"
    r = await service_client.patch(
        url, json={"version": 1, "project": "b"}, headers=agent(projects="default")
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "project_forbidden"
    r = await web_client.patch(url, json={"version": 1, "project": "b"})
    assert r.status_code == 200 and r.json()["project"] == "b"


async def test_put_document_and_read(service_client, web_client):
    spec = await make_spec(service_client)
    url = doc_url(spec["key"])
    r = await service_client.put(
        url,
        json={"body": "two", "base_version": 1, "note": "n", "client_key": "c"},
        headers=agent(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2 and r.json()["note"] == "n"
    r2 = await service_client.put(
        url, json={"body": "two", "base_version": 1, "client_key": "c"}, headers=agent()
    )
    assert r2.status_code == 200 and r2.json()["version"] == 2
    r = await web_client.get(url)
    assert r.status_code == 200 and r.json()["body"] == "two" and r.json()["version"] == 2
    r = await web_client.get(url, params={"version": 1})
    assert r.json()["body"] == "# Plan\n"
    r = await web_client.get(url, params={"version": 9})
    assert r.status_code == 404
    r = await web_client.get(f"{SPECS}/{spec['key']}/docs")
    assert [(d["path"], d["version"]) for d in r.json()["items"]] == [("plan.md", 2)]


async def test_put_new_and_remove(service_client, web_client):
    spec = await make_spec(service_client)
    r = await service_client.put(
        doc_url(spec["key"], "research/a.md"),
        json={"body": "r", "base_version": 0},
        headers=agent(),
    )
    assert r.status_code == 201
    r = await service_client.put(
        doc_url(spec["key"], "research/a.md"),
        json={"body": None, "base_version": 1},
        headers=agent(),
    )
    assert r.status_code == 201 and r.json()["version"] == 2
    r = await web_client.get(doc_url(spec["key"], "research/a.md"))
    assert r.status_code == 404
    r = await web_client.get(doc_url(spec["key"], "research/a.md"), params={"version": 2})
    assert r.status_code == 404


async def test_put_412_body_has_current_version(service_client):
    spec = await make_spec(service_client)
    r = await service_client.put(
        doc_url(spec["key"]), json={"body": "x", "base_version": 0}, headers=agent()
    )
    assert r.status_code == 412
    error = r.json()["error"]
    assert error["code"] == "stale_version" and error["current_version"] == 1


async def test_put_413_over_cap(service_client):
    spec = await make_spec(service_client)
    r = await service_client.put(
        doc_url(spec["key"]), json={"body": "x" * (CAP + 1), "base_version": 1}, headers=agent()
    )
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    r = await service_client.put(
        doc_url(spec["key"]), json={"body": "x" * CAP, "base_version": 1}, headers=agent()
    )
    assert r.status_code == 201


async def test_put_422(service_client):
    spec = await make_spec(service_client)
    for path, body in (
        ("a.txt", {"body": "x", "base_version": 0}),
        ("plan.md", {"base_version": 1}),
        ("plan.md", {"body": "x", "base_version": 1, "note": "n" * 501}),
        ("plan.md", {"body": "x", "base_version": 1, "sources": [{}] * 21}),
    ):
        r = await service_client.put(doc_url(spec["key"], path), json=body, headers=agent())
        assert r.status_code == 422, (path, body, r.text)


async def test_raw_read_etag_304(service_client, web_client):
    spec = await make_spec(service_client)
    r = await web_client.get(doc_url(spec["key"]), headers={"Accept": "text/markdown"})
    assert r.status_code == 200
    assert r.text == "# Plan\n"
    assert r.headers["content-type"].startswith("text/markdown")
    etag = f'"{hashlib.sha256(b"# Plan\n").hexdigest()}"'
    assert r.headers["etag"] == etag and r.headers["x-spec-version"] == "1"
    r = await web_client.get(
        doc_url(spec["key"]), headers={"Accept": "text/markdown", "If-None-Match": etag}
    )
    assert r.status_code == 304 and r.content == b""


async def test_versions_path_with_slash(service_client, web_client):
    spec = await make_spec(service_client, docs=[{"path": "research/a.md", "body": "1"}])
    url = doc_url(spec["key"], "research/a.md")
    await service_client.put(url, json={"body": "2", "base_version": 1}, headers=agent())
    await service_client.put(url, json={"body": "3", "base_version": 2}, headers=agent())
    r = await web_client.get(f"{url}/versions", params={"limit": 2})
    assert r.status_code == 200, r.text
    assert [v["version"] for v in r.json()["items"]] == [3, 2]
    r = await web_client.get(f"{url}/versions", params={"cursor": r.json()["next_cursor"]})
    assert [v["version"] for v in r.json()["items"]] == [1]
    assert "body" not in r.json()["items"][0]


async def test_versions_of_a_path_that_never_existed_404(service_client, web_client):
    spec = await make_spec(service_client)
    r = await web_client.get(f"{doc_url(spec['key'], 'nope.md')}/versions")
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")


async def test_lone_surrogate_body_422(service_client):
    spec = await make_spec(service_client)
    r = await service_client.put(
        doc_url(spec["key"]),
        content=b'{"body": "a\\ud800", "base_version": 1}',
        headers={**agent(), "content-type": "application/json"},
    )
    assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_request")


async def test_delete_relay_404_user_204_then_404_everywhere(service_client, web_client):
    spec = await make_spec(service_client)
    url = f"{SPECS}/{spec['key']}"
    r = await service_client.delete(url, headers=agent())
    assert r.status_code == 404
    r = await web_client.delete(url)
    assert r.status_code == 204
    for client, headers in ((web_client, {}), (service_client, agent())):
        assert (await client.get(url, headers=headers)).status_code == 404
        assert (await client.get(doc_url(spec["key"]), headers=headers)).status_code == 404
        r = await client.put(
            doc_url(spec["key"]), json={"body": "x", "base_version": 1}, headers=headers
        )
        assert r.status_code == 404
    assert (await web_client.delete(url)).status_code == 404
    again = await make_spec(service_client)
    assert again["key"] != spec["key"]


async def test_delegate_serves_spec_routes(service_client, delegate_client):
    spec = await make_spec(service_client)
    r = await delegate_client.get(f"{SPECS}/{spec['key']}")
    assert r.status_code == 200
    r = await delegate_client.delete(f"{SPECS}/{spec['key']}")
    assert r.status_code == 204


async def test_unknown_and_malformed_keys_404(web_client):
    for key in ("S-999999", "S-x", "T-1"):
        r = await web_client.get(f"{SPECS}/{key}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
