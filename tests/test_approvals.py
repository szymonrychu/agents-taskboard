from datetime import timedelta

import httpx
from test_api_tasks import TASKS, agent, create
from test_decisions import make


async def ask_approval(client, key, headers=None, **body) -> httpx.Response:
    body.setdefault("title", "merge PR 12?")
    return await client.post(
        f"{TASKS}/{key}/approvals",
        json=body,
        headers=headers if headers is not None else agent(),
    )


async def test_ask_approval_fixed_options_and_default_expiry(service_client, conn):
    task = await create(service_client)
    r = await ask_approval(service_client, task["key"], subject="https://github.com/o/r/pull/12")
    assert r.status_code == 201, r.text
    j = r.json()
    assert (j["kind"], j["status"], j["question"]) == ("approval", "open", "merge PR 12?")
    assert j["subject"] == "https://github.com/o/r/pull/12"
    assert j["options"] == [
        {"label": "Go", "description": None, "recommended": True},
        {"label": "No-go", "description": None, "recommended": False},
    ]
    ttl = conn.execute("SELECT expires_at - created_at FROM decisions WHERE id = %s", (j["id"],))
    assert ttl.fetchone()[0] == timedelta(hours=72)


async def test_ask_approval_replay_by_client_key_returns_same(service_client):
    key = (await create(service_client))["key"]
    first = await ask_approval(service_client, key, client_key="k1")
    again = await ask_approval(service_client, key, client_key="k1")
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]


async def test_ask_approval_expiry_bounds(service_client):
    key = (await create(service_client))["key"]
    assert (await ask_approval(service_client, key, expires_in_seconds=604800)).status_code == 201
    for bad in (604801, 59):
        r = await ask_approval(service_client, key, expires_in_seconds=bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


async def test_user_cannot_ask_approval(web_client, service_client):
    key = (await create(service_client))["key"]
    r = await ask_approval(web_client, key, headers={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "agent_required"


async def test_ask_approval_unknown_task_404(service_client):
    assert (await ask_approval(service_client, "T-999999")).status_code == 404


async def test_question_decisions_report_kind_question(service_client, web_client):
    key, _ = await make(service_client)
    d = (await web_client.get(f"{TASKS}/{key}")).json()["decisions"][0]
    assert (d["kind"], d["expires_at"], d["answered_via"]) == ("question", None, None)
