from datetime import timedelta

import httpx
from test_api_tasks import S1, TASKS, agent, create
from test_decisions import WITHDRAW, ask, make

from agents_taskboard.api.app import PREFIX


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


async def test_ask_approval_replay_of_question_key_refused(service_client):
    key = (await create(service_client))["key"]
    q = await ask(service_client, key, client_key="k1")
    assert q.status_code == 201
    r = await ask_approval(service_client, key, client_key="k1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "client_key_conflict"


async def answer(web_client, key, did, **body):
    return await web_client.post(f"{TASKS}/{key}/decisions/{did}/answer", json=body)


async def test_single_answer_go_records_via_and_approval_notice(service_client, web_client, conn):
    key = (await create(service_client))["key"]
    did = (await ask_approval(service_client, key)).json()["id"]
    r = await answer(web_client, key, did, choice=0, note="ship it")
    assert r.status_code == 200, r.text
    assert (r.json()["answer"], r.json()["answered_via"]) == ({"choice": 0, "label": "Go"}, "web")
    rows = conn.execute("SELECT session_id, reason, status, decision_id FROM notices").fetchall()
    assert rows == [(S1, "approval", "pending", did)]


async def test_single_answer_text_refused_for_approval(service_client, web_client):
    key = (await create(service_client))["key"]
    did = (await ask_approval(service_client, key)).json()["id"]
    r = await answer(web_client, key, did, text="sure")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


async def test_answer_after_expiry_is_409_expired(service_client, web_client, conn):
    key = (await create(service_client))["key"]
    did = (await ask_approval(service_client, key)).json()["id"]
    conn.execute(
        "UPDATE decisions SET expires_at = now() - interval '1 second' WHERE id = %s", (did,)
    )
    r = await answer(web_client, key, did, choice=0)
    assert r.status_code == 409 and r.json()["error"]["code"] == "expired"
    assert conn.execute("SELECT count(*) FROM notices").fetchone()[0] == 0


async def test_question_answer_records_via_web(service_client, web_client):
    key, did = await make(service_client)
    r = await answer(web_client, key, did, text="whatever")
    assert r.status_code == 200 and r.json()["answered_via"] == "web"


async def test_batch_answer_partial_results(service_client, web_client, conn):
    key = (await create(service_client))["key"]
    a1, a2, a3 = [
        (await ask_approval(service_client, key, client_key=f"k{i}")).json()["id"] for i in range(3)
    ]
    await answer(web_client, key, a2, choice=1)
    await service_client.post(
        f"{TASKS}/{key}/decisions/{a3}/withdraw", json=WITHDRAW, headers=agent()
    )
    q = (await ask(service_client, key)).json()["id"]  # a question decision
    r = await web_client.post(
        f"{PREFIX}/approvals/answer",
        json={
            "items": [
                {"id": a1, "verdict": "go"},
                {"id": a2, "verdict": "go"},
                {"id": a3, "verdict": "no_go"},
                {"id": q, "verdict": "go"},
                {"id": 999999, "verdict": "go"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert [i["result"] for i in r.json()["items"]] == [
        "answered",
        "already_answered",
        "withdrawn",
        "not_found",
        "not_found",
    ]
    assert r.json()["items"][0]["task_key"] == key and r.json()["items"][4]["task_key"] is None


async def test_batch_answer_expired_item(service_client, web_client, conn):
    key = (await create(service_client))["key"]
    did = (await ask_approval(service_client, key)).json()["id"]
    conn.execute(
        "UPDATE decisions SET expires_at = now() - interval '1 second' WHERE id = %s", (did,)
    )
    r = await web_client.post(
        f"{PREFIX}/approvals/answer", json={"items": [{"id": did, "verdict": "go"}]}
    )
    assert r.json()["items"] == [{"id": did, "result": "expired", "task_key": key}]
    d = (await web_client.get(f"{TASKS}/{key}")).json()["decisions"][0]
    assert (d["status"], d["answered_via"]) == ("open", None)


async def test_batch_answer_rejects_empty_and_duplicates(web_client):
    for items in ([], [{"id": 1, "verdict": "go"}, {"id": 1, "verdict": "no_go"}]):
        r = await web_client.post(f"{PREFIX}/approvals/answer", json={"items": items})
        assert r.status_code == 422


async def test_service_listener_has_no_batch_answer(service_client):
    r = await service_client.post(
        f"{PREFIX}/approvals/answer", json={"items": [{"id": 1, "verdict": "go"}]}, headers=agent()
    )
    assert r.status_code == 404


async def test_ask_question_replay_of_approval_key_refused(service_client):
    key = (await create(service_client))["key"]
    a = await ask_approval(service_client, key, client_key="k1")
    assert a.status_code == 201
    r = await ask(service_client, key, client_key="k1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "client_key_conflict"
