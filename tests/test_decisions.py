import asyncio

import httpx
import psycopg
from test_api_tasks import S1, S2, TASKS, agent, create

OPTS = [
    {"label": "yes", "description": "do it", "recommended": True},
    {"label": "no"},
]


def dec_url(key: str, decision_id: int | None = None, action: str | None = None) -> str:
    url = f"{TASKS}/{key}/decisions"
    if decision_id is not None:
        url += f"/{decision_id}"
    if action:
        url += f"/{action}"
    return url


async def ask(client, key: str, headers=None, **body) -> httpx.Response:
    body.setdefault("question", "which way?")
    body.setdefault("options", OPTS)
    return await client.post(
        dec_url(key), json=body, headers=headers if headers is not None else agent()
    )


async def make(service_client, **body) -> tuple[str, int]:
    task = await create(service_client)
    r = await ask(service_client, task["key"], **body)
    assert r.status_code == 201, r.text
    return task["key"], r.json()["id"]


def notices(conn, key: str | None = None):
    return conn.execute("SELECT session_id, reason, status FROM notices ORDER BY id").fetchall()


async def test_ask_creates_decision_and_blocks(service_client, web_client, conn):
    key, did = await make(service_client)
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    assert task["blocked"] is True
    assert task["open_decisions"] == 1
    assert task["waiting_sessions"] == [{"session_id": S1, "slug": "mx-1"}]
    d = task["decisions"][0]
    assert d["id"] == did and d["status"] == "open" and d["asking_session"] == S1
    assert d["options"][0] == {"label": "yes", "description": "do it", "recommended": True}
    assert d["options"][1] == {"label": "no", "description": None, "recommended": False}
    assert "decision_asked" in [e["kind"] for e in task["events"]]
    assert notices(conn) == []


async def test_user_cannot_ask(web_client, service_client):
    task = await create(service_client)
    r = await ask(web_client, task["key"], headers={})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "agent_required"


async def test_ask_unknown_task_404(service_client):
    r = await ask(service_client, "T-999999")
    assert r.status_code == 404


async def test_option_validation_one_recommended(service_client):
    task = await create(service_client)
    key = task["key"]
    a, b = {"label": "a"}, {"label": "b"}
    ra, rb = {**a, "recommended": True}, {**b, "recommended": True}
    for options in (
        [a, b],  # none recommended
        [ra, rb],  # two recommended
        [ra],  # too few
        [ra] + [{"label": f"o{i}"} for i in range(8)],  # nine
        [ra, {"label": ""}],  # empty label
    ):
        r = await ask(service_client, key, options=options)
        assert r.status_code == 422, options
    ok = [ra] + [{"label": f"o{i}"} for i in range(7)]
    assert (await ask(service_client, key, options=ok)).status_code == 201
    assert (await ask(service_client, key, options=[ra, b])).status_code == 201
    assert (await ask(service_client, key, question="")).status_code == 422


async def test_ask_replayed_client_key_returns_same_decision(service_client, conn):
    task = await create(service_client)
    key = task["key"]
    first = await ask(service_client, key, client_key="k1")
    again = await ask(service_client, key, client_key="k1")
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    other = await ask(service_client, key, client_key="k1", headers=agent(S2, "mx-2"))
    assert other.status_code == 201 and other.json()["id"] != first.json()["id"]
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 2
    asked = conn.execute("SELECT count(*) FROM events WHERE kind = 'decision_asked'").fetchone()
    assert asked[0] == 2


async def test_answer_by_option_and_own_text(service_client, web_client, conn):
    key, did = await make(service_client)
    r = await web_client.post(dec_url(key, did, "answer"), json={"choice": 1, "note": "n"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "answered" and d["answer"] == {"choice": 1, "label": "no"}
    assert d["note"] == "n" and d["answered_at"] is not None
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    assert task["blocked"] is False and task["open_decisions"] == 0
    assert task["waiting_sessions"] == []
    assert "decision_answered" in [e["kind"] for e in task["events"]]

    key2, did2 = await make(service_client)
    r = await web_client.post(dec_url(key2, did2, "answer"), json={"text": "something else"})
    assert r.status_code == 200
    assert r.json()["answer"] == {"text": "something else"}


async def test_answer_body_validation(service_client, web_client):
    key, did = await make(service_client)
    url = dec_url(key, did, "answer")
    for body in (
        {},
        {"choice": 2},
        {"choice": -1},
        {"text": ""},
        {"text": "   "},
        {"choice": 0, "text": "x"},
        {"choice": None, "text": None},
        {"choice": "a"},
        {"note": "n"},
    ):
        r = await web_client.post(url, json=body)
        assert r.status_code == 422, body
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    assert task["decisions"][0]["status"] == "open"


async def test_answer_wrong_task_or_missing_404(service_client, web_client):
    key, did = await make(service_client)
    other = await create(service_client)
    r = await web_client.post(dec_url(other["key"], did, "answer"), json={"choice": 0})
    assert r.status_code == 404
    r = await web_client.post(dec_url(key, did + 1000, "answer"), json={"choice": 0})
    assert r.status_code == 404


async def test_answer_does_not_bump_version(service_client, web_client):
    key, did = await make(service_client)
    before = (await web_client.get(f"{TASKS}/{key}")).json()["version"]
    await web_client.post(dec_url(key, did, "answer"), json={"choice": 0})
    assert (await web_client.get(f"{TASKS}/{key}")).json()["version"] == before


async def test_decision_notice_has_reason_decision(service_client, web_client, conn):
    key, did = await make(service_client)
    await web_client.post(dec_url(key, did, "answer"), json={"choice": 0})
    assert notices(conn) == [(S1, "decision", "pending")]


async def test_notice_row_only_for_asking_session(service_client, web_client, conn):
    task = await create(service_client, headers=agent(S2, "mx-2"))
    key = task["key"]
    did = (await ask(service_client, key)).json()["id"]  # asked by S1, task owned by S2
    await web_client.post(dec_url(key, did, "answer"), json={"choice": 0})
    assert notices(conn) == [(S1, "decision", "pending")]


async def test_second_answer_same_asker_pending_notice_one_row(service_client, web_client, conn):
    task = await create(service_client)
    key = task["key"]
    d1 = (await ask(service_client, key)).json()["id"]
    d2 = (await ask(service_client, key)).json()["id"]
    assert (
        await web_client.post(dec_url(key, d1, "answer"), json={"choice": 0})
    ).status_code == 200
    assert (
        await web_client.post(dec_url(key, d2, "answer"), json={"choice": 1})
    ).status_code == 200
    assert notices(conn) == [(S1, "decision", "pending")]
    # once sent, a later answer queues a fresh notice
    conn.execute("UPDATE notices SET status = 'sent', sent_at = now()")
    d3 = (await ask(service_client, key)).json()["id"]
    await web_client.post(dec_url(key, d3, "answer"), json={"choice": 0})
    assert notices(conn) == [(S1, "decision", "sent"), (S1, "decision", "pending")]


async def test_answer_withdrawn_is_409_no_notice(service_client, web_client, conn):
    key, did = await make(service_client)
    r = await service_client.post(dec_url(key, did, "withdraw"), headers=agent())
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    r = await web_client.post(dec_url(key, did, "answer"), json={"choice": 0})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_answered"
    assert notices(conn) == []


async def test_answer_twice_is_409(service_client, web_client):
    key, did = await make(service_client)
    assert (
        await web_client.post(dec_url(key, did, "answer"), json={"choice": 0})
    ).status_code == 200
    r = await web_client.post(dec_url(key, did, "answer"), json={"choice": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_answered"


async def test_withdraw_by_other_session_403(service_client, web_client):
    key, did = await make(service_client)
    r = await service_client.post(dec_url(key, did, "withdraw"), headers=agent(S2, "mx-2"))
    assert r.status_code == 403
    r = await web_client.post(dec_url(key, did, "withdraw"))
    assert r.status_code == 403
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    assert task["decisions"][0]["status"] == "open"


async def test_withdraw_only_while_open(service_client, web_client):
    key, did = await make(service_client)
    await web_client.post(dec_url(key, did, "answer"), json={"choice": 0})
    r = await service_client.post(dec_url(key, did, "withdraw"), headers=agent())
    assert r.status_code == 409
    key2, did2 = await make(service_client)
    assert (
        await service_client.post(dec_url(key2, did2, "withdraw"), headers=agent())
    ).status_code == 200
    assert (
        await service_client.post(dec_url(key2, did2, "withdraw"), headers=agent())
    ).status_code == 409
    assert (
        await service_client.post(dec_url(key, 99999, "withdraw"), headers=agent())
    ).status_code == 404


async def test_withdraw_clears_blocked_and_logs(service_client, web_client, conn):
    key, did = await make(service_client)
    await service_client.post(dec_url(key, did, "withdraw"), headers=agent())
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    assert task["blocked"] is False
    assert "decision_withdrawn" in [e["kind"] for e in task["events"]]
    assert notices(conn) == []


async def test_withdraw_then_ask_again_creates_new_decision(service_client):
    key, did = await make(service_client, client_key="k")
    await service_client.post(dec_url(key, did, "withdraw"), headers=agent())
    r = await ask(service_client, key)
    assert r.status_code == 201 and r.json()["id"] != did


async def test_task_blocked_flag_follows_open_decisions(service_client, web_client):
    task = await create(service_client)
    key = task["key"]
    assert task["blocked"] is False
    d1 = (await ask(service_client, key)).json()["id"]
    d2 = (await ask(service_client, key, headers=agent(S2, "mx-2"))).json()["id"]
    t = (await web_client.get(f"{TASKS}/{key}")).json()
    assert t["blocked"] and t["open_decisions"] == 2 and len(t["waiting_sessions"]) == 2
    await web_client.post(dec_url(key, d1, "answer"), json={"choice": 0})
    t = (await web_client.get(f"{TASKS}/{key}")).json()
    assert t["blocked"] and t["open_decisions"] == 1
    await service_client.post(dec_url(key, d2, "withdraw"), headers=agent(S2, "mx-2"))
    t = (await web_client.get(f"{TASKS}/{key}")).json()
    assert not t["blocked"] and t["open_decisions"] == 0


async def test_service_app_has_no_answer_route(service_client):
    key, did = await make(service_client)
    r = await service_client.post(dec_url(key, did, "answer"), json={"choice": 0}, headers=agent())
    assert r.status_code == 404
    paths = [route.path for route in service_client.app.routes if hasattr(route, "path")]
    assert not any(p.endswith("/answer") for p in paths)
    schema = service_client.app.openapi()
    assert not any(p.endswith("/answer") for p in schema["paths"])


async def test_closing_task_keeps_open_decisions_answerable(service_client, web_client):
    key, did = await make(service_client)
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    r = await service_client.patch(
        f"{TASKS}/{key}", json={"version": task["version"], "status": "dropped"}, headers=agent()
    )
    assert r.status_code == 200
    t = (await web_client.get(f"{TASKS}/{key}")).json()
    assert t["decisions"][0]["status"] == "open" and t["blocked"] is True


async def test_answer_on_done_task_with_open_decision_succeeds(service_client, web_client, conn):
    key, did = await make(service_client)
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    await service_client.patch(
        f"{TASKS}/{key}", json={"version": task["version"], "status": "done"}, headers=agent()
    )
    r = await web_client.post(dec_url(key, did, "answer"), json={"choice": 0})
    assert r.status_code == 200
    assert notices(conn) == [(S1, "decision", "pending")]


async def test_answer_notifies(service_client, web_client, db_url):
    key, did = await make(service_client)
    received: list[str] = []
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as listener:
        await listener.execute("LISTEN task_changed")
        await web_client.post(dec_url(key, did, "answer"), json={"choice": 0})
        async with asyncio.timeout(5):
            async for n in listener.notifies(timeout=2, stop_after=1):
                received.append(n.payload)
    assert received == [key[2:]]


async def test_decision_responses_carry_task_url(service_client, web_client):
    task = await create(service_client)
    key = task["key"]
    expected = f"https://tasks.example.test/tasks/{key}"
    asked = await ask(service_client, key)
    assert asked.json()["url"] == expected and asked.json()["task_key"] == key
    replay = await ask(service_client, key)
    assert replay.json()["url"] == expected
    did = asked.json()["id"]
    answered = await web_client.post(dec_url(key, did, "answer"), json={"choice": 0})
    assert answered.json()["url"] == expected
    other = await ask(service_client, key, question="again?")
    withdrawn = await service_client.post(
        dec_url(key, other.json()["id"], "withdraw"), headers=agent()
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["url"] == expected
