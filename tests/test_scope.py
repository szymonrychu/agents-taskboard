"""Project scope of the relay listener (projects plan 3.6, 4.2, 8): a key outside the caller's
`X-Orch-Projects` is the same 404 as an unknown key, a create or move outside it is 403, lists
and cursors never cross it."""

import json

import httpx
import pytest
from test_api_tasks import S1, TASKS, agent

from agents_taskboard.projects import ProjectLookup

ORCH = "http://orch.test"
PROJECTS = {"default": False, "a": False, "b": False, "old": True}
UNKNOWN = "T-999999"


def orchestrator(calls: list[str] | None = None) -> httpx.MockTransport:
    """A fake orchestrator service port answering `GET /api/v1/projects/{slug}`."""

    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer t"
        slug = request.url.path.rsplit("/", 1)[1]
        if request.url.path != f"/api/v1/projects/{slug}" or slug not in PROJECTS:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}})
        return httpx.Response(
            200,
            json={
                "slug": slug,
                "name": slug,
                "description": "",
                "archived": PROJECTS[slug],
                "created_at": "2026-11-01T00:00:00Z",
                "updated_at": "2026-11-01T00:00:00Z",
            },
        )

    return httpx.MockTransport(handle)


@pytest.fixture(autouse=True)
def fake_projects(service_client, web_client):
    for client in (service_client, web_client):
        client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())


def scope(projects: str) -> dict[str, str]:
    return agent(S1, projects=projects)


async def make_task(client, project: str, **body) -> dict:
    r = await client.post(TASKS, json={"title": f"in {project}", **body}, headers=scope(project))
    assert r.status_code == 201, r.text
    return r.json()


async def seed(client) -> tuple[str, int, int]:
    """A task of project a with one todo item and one open decision asked by S1."""
    task = await make_task(client, "a")
    key = task["key"]
    r = await client.put(f"{TASKS}/{key}/todo", json={"items": [{"text": "x"}]}, headers=scope("a"))
    item_id = r.json()["todo"][0]["id"]
    r = await client.post(
        f"{TASKS}/{key}/decisions",
        json={
            "question": "q?",
            "options": [{"label": "y", "recommended": True}, {"label": "n"}],
        },
        headers=scope("a"),
    )
    assert r.status_code == 201, r.text
    return key, item_id, r.json()["id"]


ROUTES = {
    "get": lambda key, item, dec: ("GET", f"{TASKS}/{key}", None),
    "patch": lambda key, item, dec: ("PATCH", f"{TASKS}/{key}", {"version": 1, "title": "new"}),
    "comment": lambda key, item, dec: ("POST", f"{TASKS}/{key}/comments", {"body": "hi"}),
    "todo_replace": lambda key, item, dec: (
        "PUT",
        f"{TASKS}/{key}/todo",
        {"items": [{"text": "y"}]},
    ),
    "todo_patch": lambda key, item, dec: (
        "PATCH",
        f"{TASKS}/{key}/todo/{item}",
        {"status": "done"},
    ),
    "decision_ask": lambda key, item, dec: (
        "POST",
        f"{TASKS}/{key}/decisions",
        {"question": "q2?", "options": [{"label": "y", "recommended": True}, {"label": "n"}]},
    ),
    "decision_withdraw": lambda key, item, dec: (
        "POST",
        f"{TASKS}/{key}/decisions/{dec}/withdraw",
        None,
    ),
}


async def call(client, route: str, key: str, item: int, dec: int, projects: str):
    method, url, body = ROUTES[route](key, item, dec)
    return await client.request(method, url, json=body, headers=scope(projects))


def as_unknown(body: dict, key: str) -> dict:
    """The body the route gives for UNKNOWN, with the key swapped back in."""
    return json.loads(json.dumps(body).replace(UNKNOWN, key))


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("caller", ["a", "b", "*"])
async def test_relay_route_scope_matrix(service_client, conn, route, caller):
    key, item, dec = await seed(service_client)
    before = conn.execute(
        "SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM comments),"
        " (SELECT count(*) FROM decisions), (SELECT version FROM tasks)"
    ).fetchone()
    r = await call(service_client, route, key, item, dec, caller)
    if caller == "b":
        unknown = await call(service_client, route, UNKNOWN, item, dec, caller)
        assert unknown.status_code == 404
        assert r.status_code == 404, r.text
        assert r.json() == as_unknown(unknown.json(), key)
        after = conn.execute(
            "SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM comments),"
            " (SELECT count(*) FROM decisions), (SELECT version FROM tasks)"
        ).fetchone()
        assert after == before
    else:
        assert r.status_code in (200, 201), r.text
        assert r.json().get("task_key", r.json().get("key")) == key


async def test_decision_of_another_projects_task_is_404_like_unknown(service_client):
    _, _, dec_a = await seed(service_client)
    task_b = await make_task(service_client, "b")
    key_b = task_b["key"]
    r = await service_client.post(f"{TASKS}/{key_b}/decisions/{dec_a}/withdraw", headers=scope("b"))
    unknown = await service_client.post(
        f"{TASKS}/{key_b}/decisions/999999/withdraw", headers=scope("b")
    )
    assert (r.status_code, unknown.status_code) == (404, 404)
    assert r.json() == json.loads(json.dumps(unknown.json()).replace("999999", str(dec_a)))


async def test_create_into_another_project_403(service_client, conn):
    r = await service_client.post(TASKS, json={"title": "t", "project": "a"}, headers=scope("b"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "project_forbidden"
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


async def test_move_into_another_project_403(service_client, conn):
    task = await make_task(service_client, "a")
    r = await service_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 1, "project": "b"}, headers=scope("a")
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "project_forbidden"
    assert conn.execute("SELECT project, version FROM tasks").fetchone() == ("a", 1)


async def test_star_agent_moves_task_and_logs_event(service_client, conn):
    task = await make_task(service_client, "a")
    r = await service_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 1, "project": "b"}, headers=scope("*")
    )
    assert r.status_code == 200, r.text
    assert (r.json()["project"], r.json()["version"]) == ("b", 2)
    moved = [e for e in r.json()["events"] if e["kind"] == "project_changed"]
    assert moved and moved[0]["detail"] == {"from": "a", "to": "b"}
    assert conn.execute("SELECT project FROM events ORDER BY id DESC LIMIT 1").fetchone() == ("b",)


async def test_create_defaults(service_client, web_client):
    assert (await make_task(service_client, "a"))["project"] == "a"
    star = await service_client.post(TASKS, json={"title": "t"}, headers=scope("*"))
    assert star.json()["project"] == "default"
    user = await web_client.post(TASKS, json={"title": "t"})
    assert user.json()["project"] == "default"
    named = await web_client.post(TASKS, json={"title": "t", "project": "b"})
    assert (named.status_code, named.json()["project"]) == (201, "b")


async def test_lists_never_return_another_projects_rows(service_client):
    for project in ("a", "b", "default", "a"):
        await make_task(service_client, project)

    async def projects(caller: str, **params) -> list[str]:
        r = await service_client.get(TASKS, params=params, headers=scope(caller))
        assert r.status_code == 200, r.text
        return [t["project"] for t in r.json()["items"]]

    assert await projects("a") == ["a", "a"]
    assert await projects("b") == ["b"]
    assert sorted(await projects("*")) == ["a", "a", "b", "default"]
    assert await projects("a", project="b") == []
    assert await projects("*", project="b") == ["b"]


async def test_cursor_from_star_list_used_by_b_returns_only_b(service_client):
    for project in ("b", "a", "b", "a"):
        await make_task(service_client, project)
    first = await service_client.get(TASKS, params={"limit": 1}, headers=scope("*"))
    assert [t["project"] for t in first.json()["items"]] == ["a"]
    cursor = first.json()["next_cursor"]
    r = await service_client.get(TASKS, params={"cursor": cursor}, headers=scope("b"))
    assert r.status_code == 200
    assert [t["project"] for t in r.json()["items"]] == ["b", "b"]
    assert r.json()["next_cursor"] is None


async def test_missing_projects_header_400(service_client):
    base = {"Authorization": "Bearer relay-secret", "X-Orch-Session": S1}
    for headers in (base, {**base, "X-Orch-Projects": " "}, {**base, "X-Orch-Projects": "A,b"}):
        r = await service_client.get(TASKS, headers=headers)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "actor_required"


async def test_web_actor_sees_all(service_client, web_client):
    for project in ("a", "b"):
        await make_task(service_client, project)
    r = await web_client.get(TASKS)
    assert sorted(t["project"] for t in r.json()["items"]) == ["a", "b"]
    for project in ("a", "b"):
        r = await web_client.get(TASKS, params={"project": project})
        assert [t["project"] for t in r.json()["items"]] == [project]


async def test_unknown_project_422_and_archived_422(service_client, web_client, conn):
    for client, headers in ((service_client, scope("*")), (web_client, {})):
        r = await client.post(TASKS, json={"title": "t", "project": "nope"}, headers=headers)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "unknown_project")
        r = await client.post(TASKS, json={"title": "t", "project": "old"}, headers=headers)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "project_archived")
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    task = await make_task(service_client, "a")
    r = await web_client.patch(f"{TASKS}/{task['key']}", json={"version": 1, "project": "old"})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "project_archived")


async def test_invalid_project_slug_422(web_client):
    r = await web_client.post(TASKS, json={"title": "t", "project": "Not A Slug"})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_request")


async def test_project_cache_60s():
    calls: list[str] = []
    now = [1000.0]
    lookup = ProjectLookup(ORCH, "t", transport=orchestrator(calls), clock=lambda: now[0])
    await lookup.require_active("a")
    now[0] += 59
    await lookup.require_active("a")
    assert calls == ["/api/v1/projects/a"]
    now[0] += 2
    await lookup.require_active("a")
    assert len(calls) == 2
    await lookup.aclose()


async def test_archived_and_unknown_are_not_cached():
    calls: list[str] = []
    lookup = ProjectLookup(ORCH, "t", transport=orchestrator(calls))
    for slug in ("old", "old", "nope", "nope"):
        with pytest.raises(Exception):  # noqa: B017
            await lookup.require_active(slug)
    assert len(calls) == 4
    await lookup.aclose()


async def test_unreachable_orchestrator_503(web_client):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    web_client.app.state.projects = ProjectLookup(ORCH, "t", transport=httpx.MockTransport(down))
    r = await web_client.post(TASKS, json={"title": "t", "project": "a"})
    assert (r.status_code, r.json()["error"]["code"]) == (503, "upstream_unavailable")


async def test_patch_unchanged_archived_project_ok_move_into_archived_422(service_client, conn):
    task = await make_task(service_client, "a")
    conn.execute("UPDATE tasks SET project = 'old' WHERE id = %s", (int(task["key"][2:]),))
    url = f"{TASKS}/{task['key']}"
    r = await service_client.patch(
        url, json={"version": 1, "project": "old", "title": "renamed"}, headers=scope("*")
    )
    assert r.status_code == 200, r.text
    assert (r.json()["title"], r.json()["project"]) == ("renamed", "old")
    other = await make_task(service_client, "a")
    r = await service_client.patch(
        f"{TASKS}/{other['key']}", json={"version": 1, "project": "old"}, headers=scope("*")
    )
    assert (r.status_code, r.json()["error"]["code"]) == (422, "project_archived")


async def test_agent_create_without_project_checks_its_own_project(service_client):
    r = await service_client.post(TASKS, json={"title": "t"}, headers=scope("a"))
    assert (r.status_code, r.json()["project"]) == (201, "a")
    r = await service_client.post(TASKS, json={"title": "t"}, headers=scope("old"))
    assert (r.status_code, r.json()["error"]["code"]) == (422, "project_archived")
