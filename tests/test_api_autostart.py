"""Spec start queue, project autostart settings and trigger state routes (S-51 Q6, Q13, Q15)."""

import pytest
from test_api_specs import make_spec
from test_api_tasks import S1, S2, agent
from test_scope import ORCH, orchestrator

from agents_taskboard.api import schemas
from agents_taskboard.api.app import PREFIX
from agents_taskboard.projects import ProjectLookup

SPECS = f"{PREFIX}/specs"
QUEUE = f"{SPECS}/queue"


def autostart(project: str = "a") -> str:
    return f"{PREFIX}/projects/{project}/autostart"


@pytest.fixture(autouse=True)
def fake_projects(service_client, web_client, delegate_client):
    for client in (service_client, web_client, delegate_client):
        client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())


_event = iter(range(1000, 100000))


def queue_row(conn, spec_key: str, project: str, position: int | None, **cols) -> int:
    """Insert a spec_starts row (queued at `position` by default) for the spec."""
    status = cols.pop("status", "queued")
    spec_id = int(spec_key[2:])
    ordinal = conn.execute(
        "SELECT coalesce(max(ordinal), 0) + 1 FROM spec_starts WHERE spec_id = %s", (spec_id,)
    ).fetchone()[0]
    hold = cols.pop("hold_reason", None)
    row = conn.execute(
        "INSERT INTO spec_starts (spec_id, approval_event_id, ordinal, project, status, position,"
        " hold_reason) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (spec_id, next(_event), ordinal, project, status, position, hold),
    ).fetchone()
    return row[0]


def configure(conn, project: str, sessions=2, pct=80) -> None:
    conn.execute(
        "INSERT INTO project_settings (project, max_active_sessions, max_usage_pct)"
        " VALUES (%s, %s, %s)",
        (project, sessions, pct),
    )


async def spec_in(client, project: str, name: str) -> str:
    return (await make_spec(client, project=project, name=name))["key"]


async def test_settings_get_unconfigured_returns_nulls(web_client):
    r = await web_client.get(autostart())
    assert r.status_code == 200, r.text
    assert r.json() == {
        "project": "a",
        "max_active_sessions": None,
        "max_usage_pct": None,
        "configured": False,
        "updated_at": None,
    }


async def test_settings_put_by_user_stores(web_client, delegate_client, conn):
    r = await web_client.put(autostart(), json={"max_active_sessions": 3, "max_usage_pct": 70})
    assert r.status_code == 200, r.text
    body = r.json()
    schemas.AutostartView.model_validate(body)
    assert body["configured"] is True and body["updated_at"] is not None
    assert (await web_client.get(autostart())).json() == body
    assert (await delegate_client.get(autostart())).json() == body
    r = await web_client.put(autostart(), json={"max_active_sessions": None, "max_usage_pct": 70})
    assert r.json()["configured"] is False and r.json()["max_active_sessions"] is None
    row = conn.execute("SELECT max_active_sessions, max_usage_pct FROM project_settings").fetchone()
    assert row == (None, 70)


@pytest.mark.parametrize(
    "body",
    [
        {"max_active_sessions": 0, "max_usage_pct": 50},
        {"max_active_sessions": 1001, "max_usage_pct": 50},
        {"max_active_sessions": 1, "max_usage_pct": 0},
        {"max_active_sessions": 1, "max_usage_pct": 101},
        {"max_active_sessions": 1},
        {"max_active_sessions": 1, "max_usage_pct": 5, "extra": 1},
    ],
)
async def test_settings_put_rejects_bad_values(web_client, body):
    r = await web_client.put(autostart(), json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


async def test_settings_routes_absent_on_service_listener(service_client):
    for headers in (agent(), agent(projects="a")):
        r = await service_client.get(autostart(), headers=headers)
        assert r.status_code in (404, 405)
        r = await service_client.put(
            autostart(), json={"max_active_sessions": 1, "max_usage_pct": 1}, headers=headers
        )
        assert r.status_code in (404, 405)


async def test_settings_put_unknown_project_422(web_client):
    r = await web_client.put(
        autostart("nope"), json={"max_active_sessions": 1, "max_usage_pct": 10}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_project"


async def test_queue_list_any_agent_scoped(service_client, web_client, conn):
    a = await spec_in(service_client, "a", "plan-a")
    b = await spec_in(service_client, "b", "plan-b")
    queue_row(conn, a, "a", 1)
    queue_row(conn, b, "b", 1)
    configure(conn, "b")
    r = await service_client.get(QUEUE, headers=agent(S2, projects="a"))
    assert r.status_code == 200, r.text
    (only,) = r.json()["projects"]
    assert only["project"] == "a" and only["state"] == "off"
    assert only["max_active_sessions"] is None
    (item,) = only["items"]
    assert item["spec"] == a and item["position"] == 1 and item["ordinal"] == 1
    r = await service_client.get(QUEUE, headers=agent())
    schemas.SpecQueue.model_validate(r.json())
    states = {p["project"]: p["state"] for p in r.json()["projects"]}
    assert states == {"a": "off", "b": "ready"}
    r = await web_client.get(QUEUE, params={"project": "b"})
    assert [p["project"] for p in r.json()["projects"]] == ["b"]


async def test_queue_idle_and_dense_rank_over_gaps(service_client, web_client, conn):
    keys = [await spec_in(service_client, "a", f"plan-{n}") for n in range(3)]
    ids = [queue_row(conn, k, "a", pos) for k, pos in zip(keys, (3, 7, 9), strict=True)]
    configure(conn, "c")
    r = await web_client.get(QUEUE)
    by_project = {p["project"]: p for p in r.json()["projects"]}
    assert by_project["c"]["state"] == "idle" and by_project["c"]["items"] == []
    assert [(i["id"], i["position"]) for i in by_project["a"]["items"]] == list(
        zip(ids, (1, 2, 3), strict=True)
    )


async def test_queue_state_reflects_head_hold_reason(service_client, web_client, conn):
    a = await spec_in(service_client, "a", "plan-a")
    a2 = await spec_in(service_client, "a", "plan-a2")
    configure(conn, "a")
    queue_row(conn, a, "a", 1, hold_reason="usage_unknown")
    queue_row(conn, a2, "a", 2, hold_reason="at_limit")
    r = await web_client.get(QUEUE, params={"project": "a"})
    (project,) = r.json()["projects"]
    assert project["state"] == "usage_unknown"
    assert [i["hold_reason"] for i in project["items"]] == ["usage_unknown", "at_limit"]


async def test_queue_stale_autostart_off_hold_maps_to_ready(service_client, web_client, conn):
    a = await spec_in(service_client, "a", "plan-a")
    configure(conn, "a")
    queue_row(conn, a, "a", 1, hold_reason="autostart_off")
    r = await web_client.get(QUEUE, params={"project": "a"})
    assert r.status_code == 200, r.text
    (project,) = r.json()["projects"]
    assert project["state"] == "ready"


async def test_hold_reasons_all_valid_states(service_client, web_client, conn):
    from typing import get_args

    from agents_taskboard.db.spec_starts import HOLD_REASONS

    states = set(get_args(schemas.AutostartState))
    for hold in HOLD_REASONS:
        assert hold in states or hold == "autostart_off"


async def make_queue(service_client, conn, n=3, project="a") -> list[int]:
    ids = []
    for i in range(n):
        key = await spec_in(service_client, project, f"plan-{project}{i}")
        ids.append(queue_row(conn, key, project, (i + 1) * 2))
    return ids


def order(body) -> list[int]:
    (project,) = body.get("projects", (body,))
    return [i["id"] for i in project["items"]]


async def test_move_by_user_and_superleader(service_client, web_client, conn):
    one, two, three = await make_queue(service_client, conn)
    r = await web_client.post(f"{QUEUE}/{three}/move", json={"position": 1})
    assert r.status_code == 200, r.text
    schemas.ProjectQueue.model_validate(r.json())
    assert order(r.json()) == [three, one, two]
    assert [i["position"] for i in r.json()["items"]] == [1, 2, 3]
    r = await service_client.post(f"{QUEUE}/{three}/move", json={"before": two}, headers=agent())
    assert r.status_code == 200, r.text
    assert order(r.json()) == [one, three, two]
    events = conn.execute(
        "SELECT kind, detail FROM events WHERE kind = 'implementation_queue_moved' ORDER BY id"
    ).fetchall()
    assert [e[1] for e in events] == [
        {"start_id": three, "position": 1},
        {"start_id": three, "position": 2},
    ]


async def test_move_by_plain_agent_403(service_client, conn):
    (one, *_) = await make_queue(service_client, conn)
    r = await service_client.post(
        f"{QUEUE}/{one}/move", json={"position": 2}, headers=agent(projects="a")
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "superleader_only"


async def test_move_plain_agent_other_project_403_and_absent_404(service_client, web_client, conn):
    (one, *_) = await make_queue(service_client, conn)
    r = await service_client.post(
        f"{QUEUE}/{one}/move", json={"position": 1}, headers=agent(projects="b")
    )
    assert r.status_code == 403
    assert (await web_client.post(f"{QUEUE}/99999/move", json={"position": 1})).status_code == 404


async def test_move_non_queued_409(service_client, web_client, conn):
    key = await spec_in(service_client, "a", "plan-x")
    done = queue_row(conn, key, "a", None, status="handled")
    r = await web_client.post(f"{QUEUE}/{done}/move", json={"position": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_queued"


async def test_move_in_progress_409(service_client, web_client, conn):
    key = await spec_in(service_client, "a", "plan-y")
    busy = queue_row(conn, key, "a", None, status="in_progress")
    r = await web_client.post(f"{QUEUE}/{busy}/move", json={"position": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_queued"


async def test_move_response_with_stale_autostart_off_hold(service_client, web_client, conn):
    one, two = await make_queue(service_client, conn, n=2)
    configure(conn, "a")
    conn.execute("UPDATE spec_starts SET hold_reason = 'autostart_off' WHERE id = %s", (one,))
    r = await web_client.post(f"{QUEUE}/{one}/move", json={"position": 1})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ready"


async def test_move_needs_exactly_one_target_422(service_client, web_client, conn):
    (one, two, _) = await make_queue(service_client, conn)
    for body in ({}, {"position": 1, "before": two}, {"position": 0}, {"before": one}):
        r = await web_client.post(f"{QUEUE}/{one}/move", json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request", body


async def test_remove_by_user_204_and_event(service_client, web_client, conn):
    one, two, _ = await make_queue(service_client, conn)
    r = await web_client.delete(f"{QUEUE}/{one}")
    assert r.status_code == 204
    assert conn.execute("SELECT status FROM spec_starts WHERE id = %s", (one,)).fetchone() == (
        "removed",
    )
    detail = conn.execute(
        "SELECT detail FROM events WHERE kind = 'implementation_trigger_removed'"
    ).fetchone()[0]
    assert detail == {"start_id": one}
    r = await web_client.get(QUEUE)
    assert [i["position"] for i in r.json()["projects"][0]["items"]] == [1, 2]


async def test_remove_absent_on_service_listener(service_client, conn):
    (one, *_) = await make_queue(service_client, conn)
    r = await service_client.delete(f"{QUEUE}/{one}", headers=agent())
    assert r.status_code in (404, 405)


async def test_remove_twice_409(service_client, web_client, conn):
    (one, *_) = await make_queue(service_client, conn)
    assert (await web_client.delete(f"{QUEUE}/{one}")).status_code == 204
    r = await web_client.delete(f"{QUEUE}/{one}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_queued"


async def test_spec_detail_lists_starts(service_client, web_client):
    key = (await make_spec(service_client))["key"]
    r = await web_client.patch(f"{SPECS}/{key}", json={"version": 1, "status": "approved"})
    assert r.status_code == 200, r.text
    detail = (await web_client.get(f"{SPECS}/{key}")).json()
    schemas.SpecDetail.model_validate(detail)
    assert detail["starts"][0]["status"] == "new" and detail["starts"][0]["ordinal"] == 1
    assert detail["starts"][0]["position"] is None


async def test_queue_route_not_captured_by_spec_key(service_client, web_client):
    r = await service_client.get(QUEUE, headers=agent(S1))
    assert r.status_code == 200 and r.json() == {"projects": []}
    r = await web_client.get(QUEUE)
    assert r.status_code == 200
