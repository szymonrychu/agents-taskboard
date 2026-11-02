import asyncio

import psycopg
from psycopg_pool import AsyncConnectionPool
from test_api_tasks import TASKS
from test_decisions import dec_url, make

from agents_taskboard.db import decisions as repo


async def test_two_simultaneous_answers_one_200_one_409(service_client, web_client, conn):
    key, did = await make(service_client)
    url = dec_url(key, did, "answer")
    results = await asyncio.gather(
        web_client.post(url, json={"choice": 0}),
        web_client.post(url, json={"text": "other"}),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["error"]["code"] == "already_answered"
    winner = next(r for r in results if r.status_code == 200)
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    assert task["decisions"][0]["answer"] == winner.json()["answer"]
    assert conn.execute("SELECT count(*) FROM notices").fetchone()[0] == 1
    answered = conn.execute("SELECT count(*) FROM events WHERE kind = 'decision_answered'")
    assert answered.fetchone()[0] == 1


async def test_second_transaction_waits_then_loses(service_client, db_url):
    """Two real connections: the second guarded UPDATE blocks on the first's row lock and
    matches zero rows once the first commits."""
    key, did = await make(service_client)
    async with (
        await psycopg.AsyncConnection.connect(db_url, autocommit=True) as a,
        await psycopg.AsyncConnection.connect(db_url, autocommit=True) as b,
    ):
        async with a.transaction():
            first = await repo.answer_decision(a, did, {"choice": 0, "label": "yes"}, None)
            assert first is not None and first.status == "answered"
            second = asyncio.create_task(repo.answer_decision(b, did, {"text": "late"}, None))
            await asyncio.sleep(0.5)
            assert not second.done()  # blocked on the row lock held by a's open transaction
        assert await asyncio.wait_for(second, 5) is None
    async with await psycopg.AsyncConnection.connect(db_url) as c:
        row = await (
            await c.execute("SELECT answer FROM decisions WHERE id = %s", (did,))
        ).fetchone()
        assert row[0] == {"choice": 0, "label": "yes"}
        n = await (await c.execute("SELECT count(*) FROM notices")).fetchone()
        assert n[0] == 1


async def test_answer_races_withdraw(service_client, web_client, pool: AsyncConnectionPool):
    from test_api_tasks import agent

    key, did = await make(service_client)
    results = await asyncio.gather(
        web_client.post(dec_url(key, did, "answer"), json={"choice": 0}),
        service_client.post(dec_url(key, did, "withdraw"), headers=agent()),
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409]
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    assert task["decisions"][0]["status"] in ("answered", "withdrawn")
    async with pool.connection() as c:
        n = await (await c.execute("SELECT count(*) FROM notices")).fetchone()
    answered = task["decisions"][0]["status"] == "answered"
    assert n[0] == (1 if answered else 0)
