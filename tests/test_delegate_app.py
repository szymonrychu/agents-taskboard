"""The delegate listener: the web app's routes behind a bearer gate, acting as a user."""

import json

import httpx
from conftest import BASE_URL, DELEGATE_TOKEN
from test_api_tasks import S1, TASKS, agent
from test_decisions import OPTS, ask, dec_url
from test_stream import open_stream, serve

from agents_taskboard.api.app import PREFIX, create_delegate_app


async def test_missing_bearer_is_401(settings, pool):
    app = create_delegate_app(settings, pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as client:
        r = await client.get(TASKS)
    assert r.status_code == 401


async def test_relay_token_is_401(delegate_client):
    r = await delegate_client.get(TASKS, headers={"Authorization": "Bearer relay-secret"})
    assert r.status_code == 401


async def test_delegate_token_lists_tasks(delegate_client):
    r = await delegate_client.get(TASKS)
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}


async def test_create_patch_comment_todo_act_as_user(delegate_client, conn):
    r = await delegate_client.post(TASKS, json={"title": "t", "client_key": "k"})
    assert r.status_code == 201
    task = r.json()
    key = task["key"]
    r = await delegate_client.patch(
        f"{TASKS}/{key}", json={"version": task["version"], "body": "new body"}
    )
    assert r.status_code == 200, r.text
    r = await delegate_client.post(f"{TASKS}/{key}/comments", json={"body": "hello"})
    assert r.status_code == 201, r.text
    r = await delegate_client.put(f"{TASKS}/{key}/todo", json={"items": [{"text": "x"}]})
    assert r.status_code == 200, r.text
    tid = int(key[2:])
    kinds = conn.execute(
        "SELECT DISTINCT actor_kind, actor_session FROM events WHERE task_id = %s", (tid,)
    ).fetchall()
    assert kinds == [("user", None)]
    comment = conn.execute(
        "SELECT author_kind, author_session FROM comments WHERE task_id = %s", (tid,)
    ).fetchall()
    assert comment == [("user", None)]
    created_by = conn.execute(
        "SELECT created_by_kind, created_by_session FROM tasks WHERE id = %s", (tid,)
    ).fetchall()
    assert created_by == [("user", None)]


async def test_answer_works_enqueues_asking_session_notice_second_answer_409(
    delegate_client, service_client, conn
):
    task = (await service_client.post(TASKS, json={"title": "t"}, headers=agent())).json()
    key = task["key"]
    r = await ask(service_client, key, options=OPTS)
    assert r.status_code == 201
    did = r.json()["id"]
    first = await delegate_client.post(dec_url(key, did, "answer"), json={"choice": 0})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "answered"
    assert conn.execute("SELECT session_id, reason, status FROM notices").fetchall() == [
        (S1, "decision", "pending")
    ]
    second = await delegate_client.post(dec_url(key, did, "answer"), json={"choice": 1})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_answered"
    assert conn.execute("SELECT count(*) FROM notices").fetchone()[0] == 1


async def test_stream_emits_task_changed(settings, pool, delegate_client):
    app = create_delegate_app(settings, pool, stream_poll_seconds=0.2)
    key = (await delegate_client.post(TASKS, json={"title": "t"})).json()["key"]
    async with serve(app) as base, open_stream(app, base, DELEGATE_TOKEN) as sse:
        r = await delegate_client.post(f"{TASKS}/{key}/comments", json={"body": "hi"})
        assert r.status_code == 201
        frame = await sse.next()
    assert frame["event"] == "task_changed"
    assert json.loads(frame["data"]) == {"key": key}


async def test_stream_without_bearer_is_401(settings, pool):
    app = create_delegate_app(settings, pool)
    async with serve(app) as base, httpx.AsyncClient(base_url=base) as client:
        r = await client.get(f"{PREFIX}/stream")
    assert r.status_code == 401


async def test_ask_and_withdraw_refuse_user_actor(delegate_client, web_client, service_client):
    key = (await service_client.post(TASKS, json={"title": "t"}, headers=agent())).json()["key"]
    did = (await ask(service_client, key)).json()["id"]
    for client in (delegate_client, web_client):
        r = await ask(client, key, headers={})
        assert (r.status_code, r.json()["error"]["code"]) == (403, "agent_required")
        r = await client.post(dec_url(key, did, "withdraw"))
        assert r.status_code == 403
    web = await web_client.post(dec_url(key, did, "withdraw"))
    delegate = await delegate_client.post(dec_url(key, did, "withdraw"))
    assert web.json() == delegate.json()


async def test_healthz_is_exempt_from_the_gate(settings, pool):
    app = create_delegate_app(settings, pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200
