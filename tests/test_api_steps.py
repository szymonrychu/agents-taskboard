"""Plan, steps, deps, next, gate and section routes (task-plans L2, base design 3.17 and 4.2,
gate per T-116 Q2 A and Q3 A)."""

import pytest
from test_api_tasks import S1, S2, TASKS, agent
from test_scope import ORCH, orchestrator

from agents_taskboard.api.app import PREFIX
from agents_taskboard.projects import ProjectLookup

SPECS = f"{PREFIX}/specs"
STEP_CAP = 32 * 1024


@pytest.fixture(autouse=True)
def fake_projects(service_client, web_client, delegate_client):
    for client in (service_client, web_client, delegate_client):
        client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())


async def make_spec(client, body: str = "# Plan\n", name: str = "plan-a") -> str:
    r = await client.post(
        SPECS,
        json={"name": name, "title": "A plan", "docs": [{"path": "plan.md", "body": body}]},
        headers=agent(),
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


def step(n, deps=(), **extra):
    return {"n": n, "title": f"step {n}", "body": f"body {n}", "depends_on": list(deps), **extra}


async def put_steps(client, key, steps, version, headers=None):
    return await client.put(
        f"{SPECS}/{key}/steps",
        json={"steps_version": version, "steps": steps},
        headers=agent() if headers is None else headers,
    )


async def seed_steps(client, key, steps=None) -> list[str]:
    r = await put_steps(client, key, steps or [step(1), step(2, [1]), step(3)], 0)
    assert r.status_code == 200, r.text
    return r.json()["keys"]


def err(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


# -- plan header -----------------------------------------------------------------------------

PLAN = {
    "goal": "g",
    "architecture": "a",
    "tech_stack": "py",
    "constraints": ["c1", "c2"],
    "review_focus": ["r1"],
}


async def test_plan_get_empty_put_and_stale(service_client, web_client):
    key = await make_spec(service_client)
    url = f"{SPECS}/{key}/plan"
    r = await service_client.get(url, headers=agent())
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 0 and r.json()["goal"] == "" and r.json()["constraints"] == []
    r = await service_client.put(url, json={"version": 0, **PLAN}, headers=agent())
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1 and r.json()["constraints"] == ["c1", "c2"]
    assert r.json()["updated_by"] == {"kind": "agent", "session": S1, "slug": "mx-1"}
    r = await service_client.put(url, json={"version": 0, **PLAN}, headers=agent())
    assert err(r) == (412, "stale_version")
    assert r.json()["error"]["current_version"] == 1
    r = await web_client.get(url)
    assert r.status_code == 200 and r.json()["goal"] == "g" and r.json()["version"] == 1


async def test_plan_caps_422(service_client):
    key = await make_spec(service_client)
    url = f"{SPECS}/{key}/plan"
    for bad in (
        {"goal": "x" * 501},
        {"architecture": "x" * 4001},
        {"tech_stack": "x" * 1001},
        {"constraints": ["c"] * 51},
        {"review_focus": ["x" * 1001]},
    ):
        r = await service_client.put(url, json={"version": 0, **PLAN, **bad}, headers=agent())
        assert err(r) == (422, "invalid_request"), bad
    ok = {"constraints": ["x" * 1000] * 50, "goal": "x" * 500}
    r = await service_client.put(url, json={"version": 0, **PLAN, **ok}, headers=agent())
    assert r.status_code == 200, r.text


async def test_plan_unknown_spec_404(service_client):
    for method, body in (("GET", None), ("PUT", {"version": 0, **PLAN})):
        r = await service_client.request(
            method, f"{SPECS}/S-999999/plan", json=body, headers=agent()
        )
        assert err(r) == (404, "not_found")
        r = await service_client.request(method, f"{SPECS}/bogus/plan", json=body, headers=agent())
        assert r.status_code == 404


# -- steps -----------------------------------------------------------------------------------


async def test_steps_empty_list(service_client):
    key = await make_spec(service_client)
    r = await service_client.get(f"{SPECS}/{key}/steps", headers=agent())
    assert r.status_code == 200 and r.json() == {"items": [], "steps_version": 0}


async def test_steps_put_list_and_idempotent(service_client):
    key = await make_spec(service_client)
    keys = await seed_steps(service_client, key)
    assert len(keys) == 3 and all(k.startswith("T-") for k in keys)
    r = await service_client.get(f"{SPECS}/{key}/steps", headers=agent())
    body = r.json()
    assert body["steps_version"] == 1
    items = body["items"]
    assert [i["key"] for i in items] == keys and [i["step"] for i in items] == [1, 2, 3]
    assert [i["ready"] for i in items] == [True, False, True]
    assert items[1]["deps"] == [keys[0]]
    assert "body" not in items[0]
    assert set(items[0]) >= {
        "key",
        "step",
        "title",
        "status",
        "assignee_session",
        "model",
        "deps",
        "refs",
        "ready",
        "todo_total",
        "todo_done",
        "version",
    }
    r = await put_steps(service_client, key, [step(1), step(2, [1]), step(3)], 1)
    assert r.status_code == 200 and r.json()["keys"] == keys
    r = await service_client.get(f"{SPECS}/{key}/steps", headers=agent())
    assert r.json()["steps_version"] == 1


async def test_steps_put_model_labels_todo(service_client):
    key = await make_spec(service_client)
    (k1,) = await seed_steps(
        service_client, key, [step(1, model="opus", labels=["Impl"], todo=["a", "b"])]
    )
    r = await service_client.get(f"{TASKS}/{k1}", headers=agent())
    task = r.json()
    assert (task["model"], task["labels"], task["step"]) == ("opus", ["impl"], 1)
    assert [t["text"] for t in task["todo"]] == ["a", "b"]
    assert task["spec"] == key and task["body"] == "body 1"
    r = await service_client.get(f"{SPECS}/{key}/steps", headers=agent())
    assert (r.json()["items"][0]["todo_total"], r.json()["items"][0]["todo_done"]) == (2, 0)


async def test_steps_put_errors(service_client, conn):
    key = await make_spec(service_client)
    keys = await seed_steps(service_client, key)
    r = await put_steps(service_client, key, [step(1)], 0)
    assert err(r) == (412, "stale_version")
    assert r.json()["error"]["current_steps_version"] == 1
    r = await put_steps(service_client, key, [step(1, [2]), step(2, [1])], 1)
    assert err(r) == (422, "dependency_cycle")
    r = await put_steps(service_client, key, [step(2)], 1)
    assert err(r) == (422, "invalid_request")
    r = await put_steps(service_client, key, [step(1, model="gpt")], 1)
    assert err(r) == (422, "invalid_request")
    r = await put_steps(service_client, key, [{**step(1), "body": "x" * (STEP_CAP + 1)}], 1)
    assert err(r) == (413, "payload_too_large")
    conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = %s", (int(keys[2][2:]),))
    r = await put_steps(service_client, key, [step(1), step(2, [1])], 1)
    assert err(r) == (409, "step_in_progress")
    r = await put_steps(service_client, "S-999999", [step(1)], 0)
    assert err(r) == (404, "not_found")


async def test_steps_put_drops_removed_unstarted(service_client):
    key = await make_spec(service_client)
    keys = await seed_steps(service_client, key)
    r = await put_steps(service_client, key, [step(1), step(2, [1])], 1)
    assert r.status_code == 200 and r.json()["keys"] == keys[:2]
    r = await service_client.get(f"{TASKS}/{keys[2]}", headers=agent())
    assert (r.json()["status"], r.json()["step"]) == ("dropped", None)


async def test_step_insert(service_client):
    key = await make_spec(service_client)
    keys = await seed_steps(service_client, key)
    url = f"{SPECS}/{key}/steps"
    r = await service_client.post(
        url,
        json={"steps_version": 1, "after": 1, "title": "new", "body": "b", "depends_on": [1]},
        headers=agent(),
    )
    assert r.status_code == 201, r.text
    new = r.json()
    assert (new["step"], new["title"], new["deps"]) == (2, "new", [keys[0]])
    r = await service_client.get(url, headers=agent())
    assert [i["key"] for i in r.json()["items"]] == [keys[0], new["key"], keys[1], keys[2]]
    assert r.json()["steps_version"] == 2
    r = await service_client.post(
        url, json={"steps_version": 1, "title": "x", "body": ""}, headers=agent()
    )
    assert err(r) == (412, "stale_version")
    assert r.json()["error"]["current_steps_version"] == 2
    r = await service_client.post(
        url, json={"steps_version": 2, "title": "last", "model": "haiku"}, headers=agent()
    )
    assert r.status_code == 201 and (r.json()["step"], r.json()["model"]) == (5, "haiku")
    r = await service_client.post(
        url, json={"steps_version": 3, "after": 9, "title": "x"}, headers=agent()
    )
    assert err(r) == (422, "invalid_request")
    r = await service_client.post(
        url, json={"steps_version": 3, "title": "x", "body": "x" * (STEP_CAP + 1)}, headers=agent()
    )
    assert err(r) == (413, "payload_too_large")


async def test_steps_order(service_client):
    key = await make_spec(service_client)
    keys = await seed_steps(service_client, key)
    url = f"{SPECS}/{key}/steps/order"
    r = await service_client.put(
        url, json={"steps_version": 1, "order": keys[::-1]}, headers=agent()
    )
    assert r.status_code == 200 and r.json() == {"steps_version": 2}
    r = await service_client.get(f"{SPECS}/{key}/steps", headers=agent())
    assert [i["key"] for i in r.json()["items"]] == keys[::-1]
    r = await service_client.put(url, json={"steps_version": 2, "order": keys[:2]}, headers=agent())
    assert err(r) == (422, "invalid_request")
    r = await service_client.put(
        url, json={"steps_version": 2, "order": [*keys, keys[0]]}, headers=agent()
    )
    assert err(r) == (422, "invalid_request")
    r = await service_client.put(url, json={"steps_version": 1, "order": keys}, headers=agent())
    assert err(r) == (412, "stale_version")
    assert r.json()["error"]["current_steps_version"] == 2


# -- deps ------------------------------------------------------------------------------------


async def test_task_deps(service_client):
    key = await make_spec(service_client)
    k1, k2, k3 = await seed_steps(service_client, key)
    r = await service_client.put(f"{TASKS}/{k3}/deps", json={"depends_on": [k2]}, headers=agent())
    assert r.status_code == 200, r.text
    assert r.json()["key"] == k3 and r.json()["depends_on"] == [k2]
    r = await service_client.put(f"{TASKS}/{k1}/deps", json={"depends_on": [k3]}, headers=agent())
    assert err(r) == (422, "dependency_cycle")
    r = await service_client.put(f"{TASKS}/{k1}/deps", json={"depends_on": [k1]}, headers=agent())
    assert err(r) == (422, "dependency_cycle")
    r = await service_client.put(
        f"{TASKS}/{k1}/deps", json={"depends_on": ["S-1"]}, headers=agent()
    )
    assert err(r) == (422, "invalid_request")
    r = await service_client.put(
        f"{TASKS}/{k1}/deps", json={"depends_on": ["T-999999"]}, headers=agent()
    )
    assert err(r) == (422, "invalid_request")
    other = await service_client.post(TASKS, json={"title": "plain"}, headers=agent())
    r = await service_client.put(
        f"{TASKS}/{other.json()['key']}/deps", json={"depends_on": [k1]}, headers=agent()
    )
    assert err(r) == (422, "invalid_request")
    r = await service_client.put(f"{TASKS}/T-999999/deps", json={"depends_on": []}, headers=agent())
    assert err(r) == (404, "not_found")
    r = await service_client.put(f"{TASKS}/{k3}/deps", json={"depends_on": []}, headers=agent())
    assert r.status_code == 200 and r.json()["depends_on"] == []


# -- next ------------------------------------------------------------------------------------


async def test_next_without_and_with_claim(service_client):
    key = await make_spec(service_client)
    k1, k2, k3 = await seed_steps(service_client, key)
    url = f"{SPECS}/{key}/steps/next"
    r = await service_client.post(url, json={}, headers=agent())
    assert r.status_code == 200 and r.json()["key"] == k1 and r.json()["status"] == "open"
    r = await service_client.post(url, json={"claim": True}, headers=agent())
    assert r.status_code == 200, r.text
    assert (r.json()["key"], r.json()["status"], r.json()["assignee_session"]) == (
        k1,
        "in_progress",
        S1,
    )
    r = await service_client.post(url, json={"claim": True}, headers=agent(S2))
    assert r.status_code == 200 and r.json()["key"] == k3
    r = await service_client.post(url, json={"claim": True}, headers=agent())
    assert r.status_code == 204 and r.content == b""
    r = await service_client.post(f"{SPECS}/S-999999/steps/next", json={}, headers=agent())
    assert err(r) == (404, "not_found")


# -- PATCH /tasks refs and model -------------------------------------------------------------


async def test_patch_task_refs_and_model(service_client):
    key = await make_spec(service_client)
    (k1,) = await seed_steps(service_client, key, [step(1)])
    url = f"{TASKS}/{k1}"
    refs = [{"kind": "pr", "value": "org/repo#1"}, {"kind": "branch", "value": "b"}]
    r = await service_client.patch(
        url, json={"version": 1, "refs": refs, "model": "sonnet"}, headers=agent()
    )
    assert r.status_code == 200, r.text
    assert (r.json()["refs"], r.json()["model"], r.json()["version"]) == (refs, "sonnet", 2)
    updated = [e for e in r.json()["events"] if e["kind"] == "updated"]
    assert updated[-1]["detail"] == {"fields": ["refs", "model"]}
    r = await service_client.patch(url, json={"version": 2, "model": None}, headers=agent())
    assert r.status_code == 200 and r.json()["model"] is None
    for bad in (
        {"refs": [{"kind": "wiki", "value": "x"}]},
        {"refs": [{"kind": "pr", "value": ""}]},
        {"refs": [{"kind": "pr", "value": "x"}] * 21},
        {"refs": None},
        {"model": "gpt"},
    ):
        r = await service_client.patch(url, json={"version": 3, **bad}, headers=agent())
        assert err(r) == (422, "invalid_request"), bad


async def test_patch_step_body_cap_413(service_client):
    key = await make_spec(service_client)
    (k1,) = await seed_steps(service_client, key, [step(1)])
    r = await service_client.patch(
        f"{TASKS}/{k1}", json={"version": 1, "body": "x" * (STEP_CAP + 1)}, headers=agent()
    )
    assert err(r) == (413, "payload_too_large")
    r = await service_client.patch(
        f"{TASKS}/{k1}", json={"version": 1, "body": "x" * STEP_CAP}, headers=agent()
    )
    assert r.status_code == 200
    plain = await service_client.post(TASKS, json={"title": "plain"}, headers=agent())
    r = await service_client.patch(
        f"{TASKS}/{plain.json()['key']}",
        json={"version": 1, "body": "x" * (STEP_CAP + 1)},
        headers=agent(),
    )
    assert r.status_code == 200


async def test_task_json_carries_step_fields(service_client):
    plain = await service_client.post(TASKS, json={"title": "plain"}, headers=agent())
    task = plain.json()
    assert (task["step"], task["refs"], task["model"], task["depends_on"]) == (None, [], None, [])
    key = await make_spec(service_client)
    k1, k2, _ = await seed_steps(service_client, key)
    r = await service_client.get(f"{TASKS}/{k2}", headers=agent())
    assert (r.json()["step"], r.json()["depends_on"]) == (2, [k1])


# -- gate ------------------------------------------------------------------------------------


async def ask(client, task_key: str, question: str = "Q?") -> int:
    r = await client.post(
        f"{TASKS}/{task_key}/decisions",
        json={
            "question": question,
            "options": [{"label": "A", "recommended": True}, {"label": "B"}],
        },
        headers=agent(),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def gate(client, key: str) -> dict:
    r = await client.get(f"{SPECS}/{key}/gate", headers=agent())
    assert r.status_code == 200, r.text
    return r.json()


async def test_gate_states(service_client, web_client):
    key = await make_spec(service_client)
    await seed_steps(service_client, key)
    g = await gate(service_client, key)
    assert g == {
        "key": key,
        "status": "draft",
        "approved_versions": None,
        "gate_task": None,
        "decisions": [],
        "gate": "none",
        "approved": False,
    }
    r = await service_client.post(TASKS, json={"title": "own", "spec": key}, headers=agent())
    own = r.json()["key"]
    g = await gate(service_client, key)
    assert (g["gate_task"], g["gate"], g["decisions"]) == (own, "none", [])
    d1 = await ask(service_client, own, "Q1?")
    d2 = await ask(service_client, own, "Q2?")
    g = await gate(service_client, key)
    assert g["gate"] == "waiting"
    assert [(d["id"], d["question"], d["status"], d["answer"]) for d in g["decisions"]] == [
        (d1, "Q1?", "open", None),
        (d2, "Q2?", "open", None),
    ]
    r = await web_client.post(f"{TASKS}/{own}/decisions/{d1}/answer", json={"choice": 0})
    assert r.status_code == 200, r.text
    assert (await gate(service_client, key))["gate"] == "waiting"
    r = await service_client.post(
        f"{TASKS}/{own}/decisions/{d2}/withdraw",
        json={"reason": "r", "answered_elsewhere": "x"},
        headers=agent(),
    )
    assert r.status_code == 200, r.text
    g = await gate(service_client, key)
    assert g["gate"] == "open" and g["approved"] is False
    assert [d["id"] for d in g["decisions"]] == [d1]
    assert g["decisions"][0]["answer"] == {"choice": 0, "label": "A"}


async def test_gate_only_withdrawn_is_none(service_client):
    key = await make_spec(service_client)
    r = await service_client.post(TASKS, json={"title": "own", "spec": key}, headers=agent())
    own = r.json()["key"]
    d1 = await ask(service_client, own)
    await service_client.post(
        f"{TASKS}/{own}/decisions/{d1}/withdraw",
        json={"reason": "r", "answered_elsewhere": "x"},
        headers=agent(),
    )
    g = await gate(service_client, key)
    assert (g["gate"], g["decisions"]) == ("none", [])


async def test_gate_approved_independent_of_gate(service_client, web_client):
    key = await make_spec(service_client)
    r = await service_client.post(TASKS, json={"title": "own", "spec": key}, headers=agent())
    await ask(service_client, r.json()["key"])
    r = await web_client.patch(f"{SPECS}/{key}", json={"version": 1, "status": "approved"})
    assert r.status_code == 200, r.text
    g = await gate(service_client, key)
    assert (g["status"], g["approved"], g["gate"]) == ("approved", True, "waiting")
    assert g["approved_versions"] == {"plan.md": 1}
    r = await service_client.put(
        f"{SPECS}/{key}/docs/plan.md", json={"body": "v2", "base_version": 1}, headers=agent()
    )
    assert r.status_code == 201
    g = await gate(service_client, key)
    assert (g["status"], g["approved"], g["approved_versions"]) == ("review", False, None)


async def test_gate_task_is_lowest_non_step_not_dropped(service_client):
    key = await make_spec(service_client)
    r1 = await service_client.post(TASKS, json={"title": "first", "spec": key}, headers=agent())
    r2 = await service_client.post(TASKS, json={"title": "second", "spec": key}, headers=agent())
    assert (await gate(service_client, key))["gate_task"] == r1.json()["key"]
    await service_client.patch(
        f"{TASKS}/{r1.json()['key']}", json={"version": 1, "status": "dropped"}, headers=agent()
    )
    assert (await gate(service_client, key))["gate_task"] == r2.json()["key"]


async def test_gate_unknown_spec_404(service_client):
    r = await service_client.get(f"{SPECS}/S-999999/gate", headers=agent())
    assert err(r) == (404, "not_found")


# -- section ---------------------------------------------------------------------------------

DOC = (
    "# Intro\n"
    "text\n"
    "```\n"
    "# not a heading\n"
    "```\n"
    "## Setup\n"
    "a\n"
    "## Setup\n"
    "b\n"
    "### Deep\n"
    "c\n"
    "## The `api` **routes** [docs](http://x)\n"
    "d\n"
    "# Other #\n"
    "e\n"
)


async def section(client, key, heading, **params):
    return await client.get(
        f"{SPECS}/{key}/docs/plan.md/section",
        params={"heading": heading, **params},
        headers=agent(),
    )


async def test_section_slug_match_and_duplicates(service_client):
    key = await make_spec(service_client, DOC)
    r = await section(service_client, key, "intro")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["path"], body["version"], body["heading"], body["truncated"]) == (
        "plan.md",
        1,
        "intro",
        False,
    )
    assert body["body"] == DOC.split("# Other #\n")[0]
    r = await section(service_client, key, "setup")
    assert r.json()["body"] == "## Setup\na\n"
    r = await section(service_client, key, "setup-2")
    assert r.json()["body"] == "## Setup\nb\n### Deep\nc\n"
    r = await section(service_client, key, "the-api-routes-docs")
    assert r.json()["body"] == "## The `api` **routes** [docs](http://x)\nd\n"
    r = await section(service_client, key, "other")
    assert r.json()["body"] == "# Other #\ne\n"
    r = await section(service_client, key, "not-a-heading")
    assert err(r) == (404, "not_found")
    r = await section(service_client, key, "setup-3")
    assert err(r) == (404, "not_found")


async def test_section_version_and_missing(service_client):
    key = await make_spec(service_client, "# One\nold\n")
    r = await service_client.put(
        f"{SPECS}/{key}/docs/plan.md",
        json={"body": "# Two\nnew\n", "base_version": 1},
        headers=agent(),
    )
    assert r.status_code == 201
    r = await section(service_client, key, "one", version=1)
    assert r.status_code == 200 and r.json()["version"] == 1 and r.json()["body"] == "# One\nold\n"
    r = await section(service_client, key, "one")
    assert err(r) == (404, "not_found")
    assert (await section(service_client, key, "two")).json()["version"] == 2
    r = await service_client.get(
        f"{SPECS}/{key}/docs/missing.md/section", params={"heading": "two"}, headers=agent()
    )
    assert err(r) == (404, "not_found")
    r = await service_client.get(f"{SPECS}/{key}/docs/plan.md/section", headers=agent())
    assert err(r) == (422, "invalid_request")


async def test_section_truncated_at_line_boundary(service_client):
    line = "x" * 99 + "\n"
    doc = "# Big\n" + line * 600 + "# Next\n"
    key = await make_spec(service_client, doc)
    r = await section(service_client, key, "big")
    body = r.json()
    assert body["truncated"] is True
    assert len(body["body"]) < 40000 and body["body"].endswith("\n")
    assert body["body"].startswith("# Big\n") and doc.startswith(body["body"])
    assert len(body["body"]) > 40000 - len(line)


async def test_section_on_web_and_delegate(web_client, delegate_client, service_client):
    key = await make_spec(service_client, DOC)
    for client in (web_client, delegate_client):
        r = await client.get(f"{SPECS}/{key}/docs/plan.md/section", params={"heading": "intro"})
        assert r.status_code == 200, r.text


async def test_web_user_edits_steps(web_client, service_client):
    key = await make_spec(service_client)
    keys = await seed_steps(service_client, key)
    r = await web_client.put(
        f"{SPECS}/{key}/steps/order", json={"steps_version": 1, "order": keys[::-1]}
    )
    assert r.status_code == 200
    r = await web_client.get(f"{SPECS}/{key}/steps")
    assert r.json()["steps_version"] == 2


async def test_steps_put_keeps_model_when_absent(service_client):
    key = await make_spec(service_client)
    (k1,) = await seed_steps(service_client, key, [step(1, model="opus", labels=["impl"])])
    r = await put_steps(service_client, key, [step(1, title="renamed")], 1)
    assert r.status_code == 200, r.text
    task = (await service_client.get(f"{TASKS}/{k1}", headers=agent())).json()
    assert (task["title"], task["model"], task["labels"]) == ("renamed", "opus", ["impl"])
    r = await put_steps(service_client, key, [step(1, title="renamed", model=None)], 2)
    assert r.status_code == 200, r.text
    task = (await service_client.get(f"{TASKS}/{k1}", headers=agent())).json()
    assert task["model"] is None


def test_section_multiline_setext_and_entities():
    from agents_taskboard.sections import find_section

    doc = "intro\n\nFirst line\nsecond line\n===\nbody\n\n## A &amp; B\nx\n"
    assert find_section(doc, "first-line-second-line") == (
        "First line\nsecond line\n===\nbody\n\n## A &amp; B\nx\n",
        False,
    )
    assert find_section(doc, "a-b") == ("## A &amp; B\nx\n", False)
