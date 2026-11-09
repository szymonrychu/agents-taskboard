"""GET /projects/summary: counts per project within the caller's scope (projects plan 3.6)."""

import pytest
from test_api_tasks import S1, S2, TASKS, add_decision, agent, create
from test_scope import ORCH, orchestrator

from agents_taskboard.api.app import PREFIX
from agents_taskboard.projects import ProjectLookup

SUMMARY = f"{PREFIX}/projects/summary"


@pytest.fixture(autouse=True)
def fake_projects(service_client, web_client):
    for client in (service_client, web_client):
        client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())


async def seed(service_client, conn) -> None:
    await create(service_client, agent(projects="a"), project="a")
    done = await create(service_client, agent(projects="a"), project="a")
    await service_client.patch(
        f"{TASKS}/{done['key']}", json={"version": 1, "status": "done"}, headers=agent()
    )
    b = await create(service_client, agent(projects="b"), project="b")
    add_decision(conn, b["key"], S1)
    add_decision(conn, b["key"], S2, status="answered")
    await create(service_client)


def by_project(rows: list[dict]) -> dict[str, dict]:
    return {r["project"]: r for r in rows}


async def test_summary_counts_per_project(web_client, service_client, conn):
    await seed(service_client, conn)
    r = await web_client.get(SUMMARY)
    assert r.status_code == 200, r.text
    rows = by_project(r.json())
    zero = {"draft": 0, "review": 0, "approved": 0}
    assert rows == {
        "a": {"project": "a", "open_tasks": 1, "waiting_decisions": 0, "specs": zero},
        "b": {"project": "b", "open_tasks": 1, "waiting_decisions": 1, "specs": zero},
        "default": {"project": "default", "open_tasks": 1, "waiting_decisions": 0, "specs": zero},
    }
    r = await web_client.get(SUMMARY, params={"project": "b"})
    assert list(by_project(r.json())) == ["b"]


async def test_summary_relay_caller_sees_own_project_only(service_client, conn):
    await seed(service_client, conn)
    r = await service_client.get(SUMMARY, headers=agent(projects="b"))
    assert [x["project"] for x in r.json()] == ["b"]
    r = await service_client.get(SUMMARY, params={"project": "a"}, headers=agent(projects="b"))
    assert r.json() == []
    r = await service_client.get(SUMMARY, headers=agent(projects="*"))
    assert sorted(x["project"] for x in r.json()) == ["a", "b", "default"]
    r = await service_client.get(
        SUMMARY, headers={k: v for k, v in agent().items() if k != "X-Orch-Projects"}
    )
    assert r.status_code == 400
