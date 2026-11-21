"""Approval queues one spec_starts row (S-51 Q2, Q5)."""

import pytest
from test_api_steps import ask, make_spec
from test_api_tasks import TASKS, agent
from test_scope import ORCH, orchestrator

from agents_taskboard.api.app import PREFIX
from agents_taskboard.projects import ProjectLookup

SPECS = f"{PREFIX}/specs"
OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
GATE = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def fake_projects(service_client, web_client, delegate_client):
    for client in (service_client, web_client, delegate_client):
        client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())


def starts(conn) -> list[tuple]:
    return conn.execute("SELECT spec_id, status, ordinal FROM spec_starts ORDER BY id").fetchall()


async def approve(client, key, headers, version=1):
    return await client.patch(
        f"{SPECS}/{key}", json={"version": version, "status": "approved"}, headers=headers
    )


async def test_user_approval_inserts_one_row(service_client, web_client, conn):
    key = await make_spec(service_client)
    r = await web_client.patch(f"{SPECS}/{key}", json={"version": 1, "status": "approved"})
    assert r.status_code == 200, r.text
    assert starts(conn) == [(int(key[2:]), "new", 1)]
    project = conn.execute("SELECT project FROM spec_starts").fetchone()[0]
    assert project == r.json()["project"]


async def test_repeat_patch_without_status_change_inserts_nothing(service_client, web_client, conn):
    key = await make_spec(service_client)
    await web_client.patch(f"{SPECS}/{key}", json={"version": 1, "status": "approved"})
    r = await web_client.patch(f"{SPECS}/{key}", json={"version": 2, "status": "approved"})
    assert r.status_code == 200, r.text
    assert len(starts(conn)) == 1


async def test_reapproval_after_document_write_inserts_second(service_client, web_client, conn):
    key = await make_spec(service_client)
    await web_client.patch(f"{SPECS}/{key}", json={"version": 1, "status": "approved"})
    conn.execute("UPDATE spec_starts SET status = 'handled', mode = 'started'")
    r = await web_client.put(
        f"{SPECS}/{key}/docs/plan.md", json={"body": "edited", "base_version": 1}
    )
    assert r.status_code == 201, r.text
    spec = (await web_client.get(f"{SPECS}/{key}")).json()
    assert spec["status"] == "review"
    r = await web_client.patch(
        f"{SPECS}/{key}", json={"version": spec["version"], "status": "approved"}
    )
    assert r.status_code == 200, r.text
    rows = conn.execute("SELECT ordinal, approval_event_id FROM spec_starts ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert rows[0][1] != rows[1][1]


async def test_owner_agent_self_approval_inserts_nothing(service_client, conn):
    key = await make_spec(service_client)
    r = await approve(service_client, key, agent(OWNER))
    assert r.status_code == 200, r.text
    assert starts(conn) == []


async def test_other_agent_approval_inserts_row(service_client, conn):
    key = await make_spec(service_client)
    r = await approve(service_client, key, agent(OTHER))
    assert r.status_code == 200, r.text
    assert len(starts(conn)) == 1


async def test_superleader_non_owner_inserts_row(service_client, conn):
    key = await make_spec(service_client)
    headers = {**agent(OTHER), "X-Orch-Projects": "*"}
    r = await approve(service_client, key, headers)
    assert r.status_code == 200, r.text
    assert len(starts(conn)) == 1


async def test_decisions_open_inserts_nothing(service_client, conn):
    key = await make_spec(service_client)
    r = await service_client.post(TASKS, json={"title": "own", "spec": key}, headers=agent(OWNER))
    await ask(service_client, r.json()["key"])
    headers = agent(OWNER, projects=conn.execute("SELECT project FROM specs").fetchone()[0])
    r = await approve(service_client, key, headers)
    assert r.status_code == 409, r.text
    assert starts(conn) == []


async def test_owner_falls_back_to_gate_task_assignee(web_client, service_client, conn):
    r = await web_client.post(SPECS, json={"name": "u", "title": "t"})
    key = r.json()["key"]
    r = await web_client.post(TASKS, json={"title": "gate", "spec": key})
    assert r.status_code == 201, r.text
    conn.execute("UPDATE tasks SET assignee_session = %s", (GATE,))
    spec = (await web_client.get(f"{SPECS}/{key}")).json()
    r = await approve(service_client, key, agent(GATE), spec["version"])
    assert r.status_code == 200, r.text
    assert starts(conn) == []
    r = await web_client.patch(
        f"{SPECS}/{key}", json={"version": spec["version"] + 1, "status": "review"}
    )
    spec = (await web_client.get(f"{SPECS}/{key}")).json()
    r = await approve(service_client, key, agent("s-x"), spec["version"])
    assert r.status_code == 200, r.text
    assert len(starts(conn)) == 1


async def test_approval_row_commits_with_status(service_client, web_client, conn, monkeypatch):
    key = await make_spec(service_client)
    r = await web_client.patch(f"{SPECS}/{key}", json={"version": 1, "status": "approved"})
    assert r.status_code == 200
    assert conn.execute("SELECT status FROM specs").fetchone()[0] == "approved"
    assert len(starts(conn)) == 1

    key2 = await make_spec(service_client, name="plan-b")

    async def boom(*a, **k):
        raise RuntimeError("forced")

    monkeypatch.setattr("agents_taskboard.db.specs.notify_spec_changed", boom)
    with pytest.raises(RuntimeError):
        await web_client.patch(f"{SPECS}/{key2}", json={"version": 1, "status": "approved"})
    row = conn.execute("SELECT status FROM specs WHERE id = %s", (int(key2[2:]),)).fetchone()
    assert row[0] != "approved"
    assert len(starts(conn)) == 1


async def test_reapproval_with_active_row_inserts_nothing(service_client, web_client, conn):
    key = await make_spec(service_client)
    await approve(web_client, key, {})
    await web_client.put(f"{SPECS}/{key}/docs/plan.md", json={"body": "edited", "base_version": 1})
    spec = (await web_client.get(f"{SPECS}/{key}")).json()
    r = await approve(web_client, key, {}, spec["version"])
    assert r.status_code == 200, r.text
    assert starts(conn) == [(int(key[2:]), "new", 1)]


async def test_project_move_takes_new_and_queued_starts_along(service_client, web_client, conn):
    key = await make_spec(service_client)
    spec_id = int(key[2:])
    project = (await web_client.get(f"{SPECS}/{key}")).json()["project"]
    other = "b" if project != "b" else "a"
    # a queued row of another spec already waits in the target project
    conn.execute(
        "INSERT INTO specs (name, title, project, created_by_kind, status)"
        " VALUES ('x', 't', %s, 'user', 'approved')",
        (other,),
    )
    xid = conn.execute("SELECT id FROM specs WHERE name = 'x'").fetchone()[0]
    conn.execute(
        "INSERT INTO spec_starts (spec_id, approval_event_id, ordinal, project, status, position)"
        " VALUES (%s, 9001, 1, %s, 'queued', 1)",
        (xid, other),
    )
    await approve(web_client, key, {})
    conn.execute(
        "UPDATE spec_starts SET status = 'queued', position = 1 WHERE spec_id = %s", (spec_id,)
    )
    spec = (await web_client.get(f"{SPECS}/{key}")).json()
    r = await web_client.patch(
        f"{SPECS}/{key}", json={"version": spec["version"], "project": other}
    )
    assert r.status_code == 200, r.text
    rows = conn.execute(
        "SELECT spec_id, project, position FROM spec_starts ORDER BY position"
    ).fetchall()
    assert rows == [(xid, other, 1), (spec_id, other, 2)]
    assert conn.execute(
        "SELECT count(*) FROM spec_starts WHERE project = %s AND status = 'queued'", (project,)
    ).fetchone() == (0,)


async def _gate_spec(service_client, web_client, status="draft", questions=1):
    """A spec in `status` whose gate task has `questions` open questions: (key, task, ids)."""
    key = await make_spec(service_client)
    r = await service_client.post(TASKS, json={"title": "own", "spec": key}, headers=agent())
    task = r.json()["key"]
    ids = [await ask(service_client, task, f"Q{i}?") for i in range(questions)]
    if status != "draft":
        r = await web_client.patch(f"{SPECS}/{key}", json={"version": 1, "status": status})
        assert r.status_code == 200, r.text
    return key, task, ids


def _answer(client, task, did):
    return client.post(f"{TASKS}/{task}/decisions/{did}/answer", json={"choice": 0})


def _answer_approvals(conn, key):
    return conn.execute(
        "SELECT detail FROM events WHERE spec_id = %s AND kind = 'spec_updated'"
        " AND detail->>'by' = 'decisions_answered'",
        (int(key[2:]),),
    ).fetchall()


@pytest.mark.parametrize("status", ["draft", "review"])
async def test_last_answer_on_gate_task_approves_and_starts(
    service_client, web_client, conn, status
):
    key, task, (d1, d2) = await _gate_spec(service_client, web_client, status, questions=2)
    assert (await _answer(web_client, task, d1)).status_code == 200
    assert _answer_approvals(conn, key) == [] and starts(conn) == []
    assert (await _answer(web_client, task, d2)).status_code == 200
    assert (await web_client.get(f"{SPECS}/{key}")).json()["status"] == "approved"
    events = _answer_approvals(conn, key)
    assert len(events) == 1
    assert events[0][0]["decision_id"] == d2 and events[0][0]["status"]["to"] == "approved"
    assert starts(conn) == [(int(key[2:]), "new", 1)]


@pytest.mark.parametrize("case", ["other_open", "approved", "archived", "withdrawn", "step_task"])
async def test_answer_without_trigger_approves_nothing(service_client, web_client, conn, case):
    key, task, ids = await _gate_spec(
        service_client, web_client, questions=2 if case == "other_open" else 1
    )
    sid = int(key[2:])
    if case in ("approved", "archived"):
        r = await web_client.patch(f"{SPECS}/{key}", json={"version": 1, "status": case})
        assert r.status_code == 200, r.text
        conn.execute("UPDATE spec_starts SET status = 'handled', mode = 'started'")
    before = len(starts(conn))
    if case == "withdrawn":
        r = await service_client.post(
            f"{TASKS}/{task}/decisions/{ids[0]}/withdraw",
            json={"reason": "r", "answered_elsewhere": "x"},
            headers=agent(),
        )
        assert r.status_code == 200, r.text
    elif case == "step_task":
        assert (await _answer(web_client, task, ids[0])).status_code == 200
        conn.execute("UPDATE specs SET status = 'draft' WHERE id = %s", (sid,))
        conn.execute("DELETE FROM events WHERE spec_id = %s AND detail->>'by' IS NOT NULL", (sid,))
        conn.execute("DELETE FROM spec_starts")
        body = {"steps_version": 0, "steps": [{"n": 1, "title": "s", "body": "b"}]}
        r = await service_client.put(f"{SPECS}/{key}/steps", json=body, headers=agent())
        assert r.status_code == 200, r.text
        step_key = r.json()["keys"][0]
        step_dec = await ask(service_client, step_key, "step?")
        before = len(starts(conn))
        assert (await _answer(web_client, step_key, step_dec)).status_code == 200
    else:
        assert (await _answer(web_client, task, ids[0])).status_code == 200
    expected = {"approved": "approved", "archived": "archived"}.get(case, "draft")
    spec = conn.execute("SELECT status FROM specs WHERE id = %s", (sid,)).fetchone()[0]
    assert spec == expected
    assert len(starts(conn)) == before
    if case != "step_task":
        assert _answer_approvals(conn, key) == []
