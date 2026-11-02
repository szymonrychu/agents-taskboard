import asyncio

import httpx
import psycopg
import pytest
from test_api_tasks import S1, TASKS, agent, create


def todo_url(key: str, item_id: int | None = None) -> str:
    base = f"{TASKS}/{key}/todo"
    return base if item_id is None else f"{base}/{item_id}"


async def test_put_replaces_and_orders(service_client: httpx.AsyncClient, conn):
    task = await create(service_client)
    key = task["key"]
    r = await service_client.put(
        todo_url(key),
        json={"items": [{"text": "one"}, {"text": "two", "status": "done"}, {"text": "three"}]},
        headers=agent(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"] == key
    assert [(i["text"], i["status"], i["position"]) for i in body["todo"]] == [
        ("one", "todo", 0),
        ("two", "done", 1),
        ("three", "todo", 2),
    ]
    old_ids = {i["id"] for i in body["todo"]}

    r = await service_client.put(
        todo_url(key),
        json={"items": [{"text": "b", "status": "inprogress"}, {"text": "a"}]},
        headers=agent(),
    )
    todo = r.json()["todo"]
    assert [(i["text"], i["status"], i["position"]) for i in todo] == [
        ("b", "inprogress", 0),
        ("a", "todo", 1),
    ]
    assert not old_ids & {i["id"] for i in todo}
    assert r.json()["version"] == task["version"]

    r = await service_client.put(todo_url(key), json={"items": []}, headers=agent())
    assert r.json()["todo"] == []
    kinds = [e["kind"] for e in r.json()["events"]]
    assert kinds.count("todo_replaced") == 3
    assert conn.execute("SELECT count(*) FROM todo_items").fetchone()[0] == 0


async def test_put_on_web_app(web_client: httpx.AsyncClient):
    r = await web_client.post(TASKS, json={"title": "t"})
    key = r.json()["key"]
    r = await web_client.put(todo_url(key), json={"items": [{"text": "x"}]})
    assert r.status_code == 200
    assert r.json()["todo"][0]["text"] == "x"


async def test_put_limits(service_client: httpx.AsyncClient):
    key = (await create(service_client))["key"]
    ok = await service_client.put(
        todo_url(key), json={"items": [{"text": "x"}] * 100}, headers=agent()
    )
    assert ok.status_code == 200
    assert len(ok.json()["todo"]) == 100
    too_many = await service_client.put(
        todo_url(key), json={"items": [{"text": "x"}] * 101}, headers=agent()
    )
    assert too_many.status_code == 422
    long = await service_client.put(
        todo_url(key), json={"items": [{"text": "x" * 201}]}, headers=agent()
    )
    assert long.status_code == 422
    edge = await service_client.put(
        todo_url(key), json={"items": [{"text": "x" * 200}]}, headers=agent()
    )
    assert edge.status_code == 200
    empty = await service_client.put(todo_url(key), json={"items": [{"text": ""}]}, headers=agent())
    assert empty.status_code == 422
    bad_status = await service_client.put(
        todo_url(key), json={"items": [{"text": "a", "status": "nope"}]}, headers=agent()
    )
    assert bad_status.status_code == 422
    # a rejected replace leaves the list as it was
    assert (await service_client.get(f"{TASKS}/{key}", headers=agent())).json()["todo"][0][
        "text"
    ] == "x" * 200


@pytest.mark.parametrize("text", ["a\nb", "a\rb", "a\r\nb"])
async def test_text_with_newline_422(service_client: httpx.AsyncClient, text):
    key = (await create(service_client))["key"]
    r = await service_client.put(todo_url(key), json={"items": [{"text": text}]}, headers=agent())
    assert r.status_code == 422
    item = (
        await service_client.put(todo_url(key), json={"items": [{"text": "ok"}]}, headers=agent())
    ).json()["todo"][0]
    r = await service_client.patch(todo_url(key, item["id"]), json={"text": text}, headers=agent())
    assert r.status_code == 422


async def test_patch_item_status_cycle(service_client: httpx.AsyncClient):
    key = (await create(service_client))["key"]
    put = await service_client.put(
        todo_url(key), json={"items": [{"text": "one"}, {"text": "two"}]}, headers=agent()
    )
    first, second = put.json()["todo"]
    for status in ("inprogress", "done", "todo"):
        r = await service_client.patch(
            todo_url(key, first["id"]), json={"status": status}, headers=agent()
        )
        assert r.status_code == 200, r.text
        assert r.json()["todo"][0]["status"] == status
        assert r.json()["todo"][1] == second
    r = await service_client.patch(
        todo_url(key, second["id"]), json={"text": "renamed", "status": "done"}, headers=agent()
    )
    assert r.json()["todo"][1]["text"] == "renamed"
    assert r.json()["todo"][1]["position"] == 1
    events = [e for e in r.json()["events"] if e["kind"] == "todo_updated"]
    assert len(events) == 4
    assert events[-1]["detail"] == {"item_id": second["id"], "fields": ["text", "status"]}
    assert events[-1]["actor_session"] == S1


async def test_patch_item_rejects_bad_bodies(service_client: httpx.AsyncClient):
    key = (await create(service_client))["key"]
    item = (
        await service_client.put(todo_url(key), json={"items": [{"text": "a"}]}, headers=agent())
    ).json()["todo"][0]
    url = todo_url(key, item["id"])
    for body in ({}, {"status": None}, {"text": None}, {"status": "nope"}, {"text": ""}):
        r = await service_client.patch(url, json=body, headers=agent())
        assert r.status_code == 422, body


async def test_patch_unknown_or_foreign_item_404(service_client: httpx.AsyncClient):
    a = (await create(service_client))["key"]
    b = (await create(service_client))["key"]
    item = (
        await service_client.put(todo_url(a), json={"items": [{"text": "a"}]}, headers=agent())
    ).json()["todo"][0]
    for url in (todo_url(a, item["id"] + 999), todo_url(b, item["id"])):
        r = await service_client.patch(url, json={"status": "done"}, headers=agent())
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"
    r = await service_client.patch(todo_url(a, 0), json={"status": "done"}, headers=agent())
    assert r.status_code == 404


async def test_todo_edit_creates_no_notice(service_client: httpx.AsyncClient, conn):
    key = (await create(service_client, labels=["a"]))["key"]
    before = conn.execute("SELECT count(*) FROM notices").fetchone()[0]
    put = await service_client.put(todo_url(key), json={"items": [{"text": "a"}]}, headers=agent())
    await service_client.patch(
        todo_url(key, put.json()["todo"][0]["id"]), json={"status": "done"}, headers=agent()
    )
    assert conn.execute("SELECT count(*) FROM notices").fetchone()[0] == before


async def test_todo_edit_notifies_and_keeps_version(service_client: httpx.AsyncClient, db_url: str):
    task = await create(service_client)
    task_id = task["key"][2:]
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as listener:
        await listener.execute("LISTEN task_changed")
        put = await service_client.put(
            todo_url(task["key"]), json={"items": [{"text": "a"}]}, headers=agent()
        )
        await service_client.patch(
            todo_url(task["key"], put.json()["todo"][0]["id"]),
            json={"text": "b"},
            headers=agent(),
        )
        got = []
        async with asyncio.timeout(5):
            async for n in listener.notifies():
                got.append(n.payload)
                if len(got) == 2:
                    break
    assert got == [task_id, task_id]
    fresh = (await service_client.get(f"{TASKS}/{task['key']}", headers=agent())).json()
    assert fresh["version"] == task["version"]
    assert fresh["updated_at"] > task["updated_at"]


async def test_todo_on_unknown_task_404(service_client: httpx.AsyncClient):
    for key in ("T-999999", "nonsense"):
        r = await service_client.put(todo_url(key), json={"items": []}, headers=agent())
        assert r.status_code == 404
        r = await service_client.patch(todo_url(key, 1), json={"status": "done"}, headers=agent())
        assert r.status_code == 404


async def test_todo_requires_actor_on_service_app(service_client: httpx.AsyncClient):
    key = (await create(service_client))["key"]
    r = await service_client.put(
        todo_url(key), json={"items": []}, headers={"Authorization": "Bearer relay-secret"}
    )
    assert r.status_code == 400
