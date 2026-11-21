import asyncio

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from test_api_tasks import TASKS, create
from test_approvals import ask_approval
from test_decisions import WITHDRAW, dec_url, make

from agents_taskboard.api.app import PREFIX
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
        service_client.post(dec_url(key, did, "withdraw"), json=WITHDRAW, headers=agent()),
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409]
    task = (await web_client.get(f"{TASKS}/{key}")).json()
    assert task["decisions"][0]["status"] in ("answered", "withdrawn")
    async with pool.connection() as c:
        n = await (await c.execute("SELECT count(*) FROM notices")).fetchone()
    answered = task["decisions"][0]["status"] == "answered"
    assert n[0] == (1 if answered else 0)


async def test_two_batch_answers_one_wins(service_client, web_client, conn):
    key = (await create(service_client))["key"]
    did = (await ask_approval(service_client, key)).json()["id"]
    url = f"{PREFIX}/approvals/answer"
    rs = await asyncio.gather(
        web_client.post(url, json={"items": [{"id": did, "verdict": "go"}]}),
        web_client.post(url, json={"items": [{"id": did, "verdict": "no_go"}]}),
    )
    assert sorted(r.json()["items"][0]["result"] for r in rs) == ["already_answered", "answered"]
    assert conn.execute("SELECT count(*) FROM notices").fetchone()[0] == 1


async def _approval_row(db_url, did) -> tuple[str, int]:
    async with await psycopg.AsyncConnection.connect(db_url) as c:
        status = await (
            await c.execute("SELECT status FROM decisions WHERE id = %s", (did,))
        ).fetchone()
        n = await (
            await c.execute(
                "SELECT count(*) FROM notices WHERE reason = 'approval' AND decision_id = %s",
                (did,),
            )
        ).fetchone()
    return status[0], n[0]


async def test_sweep_and_answer_race_sweep_first(service_client, db_url):
    """The sweep holds the row lock; the answer's expiry guard already excludes the due row, so
    it does not wait on the lock and loses as expired; the sweep's single notice stands."""
    key = (await create(service_client))["key"]
    did = (await ask_approval(service_client, key)).json()["id"]
    async with (
        await psycopg.AsyncConnection.connect(db_url, autocommit=True) as a,
        await psycopg.AsyncConnection.connect(db_url, autocommit=True) as b,
    ):
        await a.execute(
            "UPDATE decisions SET expires_at = now() - interval '1 second' WHERE id = %s", (did,)
        )
        async with a.transaction():
            assert await repo.expire_approvals(a) == 1
            answer = repo.answer_approval(b, did, "go", None, via="web", answer_ref=None)
            result, _ = await asyncio.wait_for(answer, 5)
            assert result == "expired"
        late, decision = await repo.answer_approval(b, did, "go", None, via="web", answer_ref=None)
        assert late == "expired" and decision is not None and decision.status == "expired"
    assert await _approval_row(db_url, did) == ("expired", 1)


async def test_sweep_and_answer_race_answer_first(service_client, db_url):
    """The answer holds the row lock; the sweep skips it, and once committed it is not open."""
    key = (await create(service_client))["key"]
    did = (await ask_approval(service_client, key)).json()["id"]
    async with (
        await psycopg.AsyncConnection.connect(db_url, autocommit=True) as a,
        await psycopg.AsyncConnection.connect(db_url, autocommit=True) as b,
    ):
        await a.execute(
            "UPDATE decisions SET expires_at = now() + interval '2 second' WHERE id = %s",
            (did,),
        )
        async with a.transaction():
            result, _ = await repo.answer_approval(a, did, "go", None, via="web", answer_ref=None)
            assert result == "answered"
            await asyncio.sleep(2.3)
            assert await repo.expire_approvals(b) == 0  # SKIP LOCKED
        assert await repo.expire_approvals(b) == 0
    assert await _approval_row(db_url, did) == ("answered", 1)


async def test_batch_failure_mid_way_keeps_earlier_items(
    service_client, web_client, conn, monkeypatch
):
    """Each item commits on its own: a DB error on a later item leaves earlier ones answered."""
    key = (await create(service_client))["key"]
    a1, a2 = [
        (await ask_approval(service_client, key, client_key=f"k{i}")).json()["id"] for i in range(2)
    ]
    real = repo.answer_approval

    async def failing(conn, decision_id, *args, **kwargs):
        if decision_id == a2:
            raise psycopg.errors.QueryCanceled("boom")
        return await real(conn, decision_id, *args, **kwargs)

    monkeypatch.setattr(repo, "answer_approval", failing)
    with pytest.raises(psycopg.errors.QueryCanceled):
        await web_client.post(
            f"{PREFIX}/approvals/answer",
            json={"items": [{"id": a2, "verdict": "go"}, {"id": a1, "verdict": "go"}]},
        )
    rows = conn.execute("SELECT id, status FROM decisions ORDER BY id").fetchall()
    assert rows == [(a1, "answered"), (a2, "open")]


async def test_overlapping_batches_in_opposite_order_both_complete(
    service_client, web_client, conn, monkeypatch
):
    """Items run sorted by id and commit one by one, so two batches naming the same approvals in
    opposite order cannot deadlock."""
    key = (await create(service_client))["key"]
    a1, a2 = [
        (await ask_approval(service_client, key, client_key=f"k{i}")).json()["id"] for i in range(2)
    ]
    real = repo.answer_approval

    async def slow(*args, **kwargs):
        result = await real(*args, **kwargs)
        await asyncio.sleep(0.3)  # interleave the two batches
        return result

    monkeypatch.setattr(repo, "answer_approval", slow)
    url = f"{PREFIX}/approvals/answer"
    rs = await asyncio.wait_for(
        asyncio.gather(
            web_client.post(
                url, json={"items": [{"id": a1, "verdict": "go"}, {"id": a2, "verdict": "go"}]}
            ),
            web_client.post(
                url,
                json={"items": [{"id": a2, "verdict": "no_go"}, {"id": a1, "verdict": "no_go"}]},
            ),
        ),
        15,
    )
    assert [r.status_code for r in rs] == [200, 200]
    for did in (a1, a2):
        results = sorted(i["result"] for r in rs for i in r.json()["items"] if i["id"] == did)
        assert results == ["already_answered", "answered"]
    assert [i["id"] for i in rs[1].json()["items"]] == [a2, a1]  # response keeps request order


async def test_two_last_answers_concurrently_approve_once(service_client, web_client, conn):
    from test_api_steps import SPECS, ask, make_spec
    from test_api_tasks import agent

    spec = await make_spec(service_client)
    r = await service_client.post(TASKS, json={"title": "own", "spec": spec}, headers=agent())
    task = r.json()["key"]
    d1, d2 = await ask(service_client, task, "Q1?"), await ask(service_client, task, "Q2?")
    rs = await asyncio.gather(
        *(web_client.post(dec_url(task, d, "answer"), json={"choice": 0}) for d in (d1, d2))
    )
    assert [r.status_code for r in rs] == [200, 200]
    assert (await web_client.get(f"{SPECS}/{spec}")).json()["status"] == "approved"
    n = conn.execute(
        "SELECT count(*) FROM events WHERE spec_id = %s AND kind = 'spec_updated'"
        " AND detail->>'by' = 'decisions_answered'",
        (int(spec[2:]),),
    ).fetchone()[0]
    assert n == 1
    assert conn.execute("SELECT count(*) FROM spec_starts").fetchone()[0] == 1
