"""GET /tasks/{key} windows: newest-N events and comments with id cursors, plus totals."""

from agents_taskboard.api.app import PREFIX

AUTH = {"Authorization": "Bearer relay-secret", "X-Orch-Session": "s-1", "X-Orch-Slug": "mx-1"}
TASKS = f"{PREFIX}/tasks"


async def big(client, conn, events: int = 150, comments: int = 60) -> str:
    r = await client.post(TASKS, json={"title": "big"}, headers=AUTH)
    key = r.json()["key"]
    task_id = int(key[2:])
    conn.execute("DELETE FROM events WHERE task_id = %s", (task_id,))
    for i in range(events):
        conn.execute(
            "INSERT INTO events (task_id, actor_kind, kind, detail) VALUES (%s, 'agent', 'x', %s)",
            (task_id, f'{{"n": {i}}}'),
        )
    for i in range(comments):
        conn.execute(
            "INSERT INTO comments (task_id, author_kind, body) VALUES (%s, 'agent', %s)",
            (task_id, f"c{i}"),
        )
    return key


async def get(client, key, **params):
    r = await client.get(f"{TASKS}/{key}", params=params, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


async def test_no_params_returns_everything_with_totals(service_client, conn):
    key = await big(service_client, conn)
    task = await get(service_client, key)
    assert len(task["events"]) == 150 and task["events_total"] == 150
    assert len(task["comments"]) == 60 and task["comments_total"] == 60
    ids = [e["id"] for e in task["events"]]
    assert ids == sorted(ids)


async def test_newest_n_oldest_first(service_client, conn):
    key = await big(service_client, conn)
    full = await get(service_client, key)
    task = await get(service_client, key, events_limit=10, comments_limit=5)
    assert task["events"] == full["events"][-10:]
    assert task["comments"] == full["comments"][-5:]
    assert (task["events_total"], task["comments_total"]) == (150, 60)


async def test_before_cursor_pages_back_without_overlap(service_client, conn):
    key = await big(service_client, conn)
    full = [e["id"] for e in (await get(service_client, key))["events"]]
    seen: list[int] = []
    before = None
    while True:
        params = {"events_limit": 40, **({"events_before": before} if before else {})}
        page = [e["id"] for e in (await get(service_client, key, **params))["events"]]
        if not page:
            break
        seen = page + seen
        before = page[0]
    assert seen == full


async def test_zero_limit_is_the_decisions_only_view(service_client, conn):
    key = await big(service_client, conn)
    task = await get(service_client, key, events_limit=0, comments_limit=0)
    assert task["events"] == [] and task["comments"] == []
    assert task["events_total"] == 150
    assert "decisions" in task and "todo" in task


async def test_bad_window_params_422(service_client, conn):
    key = await big(service_client, conn, events=1, comments=1)
    for params in ({"events_limit": -1}, {"comments_limit": 201}, {"events_before": 0}):
        r = await service_client.get(f"{TASKS}/{key}", params=params, headers=AUTH)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_request"


async def test_list_and_write_answers_carry_totals(service_client, conn):
    key = await big(service_client, conn, events=3, comments=2)
    listed = (await service_client.get(TASKS, headers=AUTH)).json()["items"]
    row = next(t for t in listed if t["key"] == key)
    assert (row["events_total"], row["comments_total"]) == (3, 2)
    r = await service_client.post(f"{TASKS}/{key}/comments", json={"body": "hi"}, headers=AUTH)
    assert r.json()["comments_total"] == 3
