import httpx
import pytest

from agents_taskboard.api.app import PREFIX

AUTH = {"Authorization": "Bearer relay-secret"}
S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
S3 = "33333333-3333-3333-3333-333333333333"
TASKS = f"{PREFIX}/tasks"


def agent(session: str = S1, slug: str | None = "mx-1", projects: str = "*") -> dict[str, str]:
    headers = {**AUTH, "X-Orch-Session": session, "X-Orch-Projects": projects}
    if slug:
        headers["X-Orch-Slug"] = slug
    return headers


async def create(client: httpx.AsyncClient, headers=None, **body) -> dict:
    body.setdefault("title", "a task")
    r = await client.post(TASKS, json=body, headers=headers if headers is not None else agent())
    assert r.status_code in (200, 201), r.text
    return r.json()


def add_decision(conn, key: str, asking: str, status: str = "open", slug: str = "mx") -> None:
    conn.execute(
        "INSERT INTO decisions (task_id, asking_session, asking_slug, question, status, answer)"
        " VALUES (%s, %s, %s, 'q?', %s, %s)",
        (int(key[2:]), asking, slug, status, '"a"' if status == "answered" else None),
    )


async def test_create_replays_same_key_returns_same_task(service_client, conn):
    first = await service_client.post(
        TASKS, json={"title": "t", "client_key": "k1"}, headers=agent()
    )
    assert first.status_code == 201
    again = await service_client.post(
        TASKS, json={"title": "other", "client_key": "k1"}, headers=agent()
    )
    assert again.status_code == 200
    assert again.json()["key"] == first.json()["key"]
    assert again.json()["title"] == "t"
    other = await service_client.post(
        TASKS, json={"title": "t", "client_key": "k1"}, headers=agent(S2)
    )
    assert other.status_code == 201
    assert other.json()["key"] != first.json()["key"]
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


async def test_create_replays_same_key_for_web_user(web_client):
    first = await web_client.post(TASKS, json={"title": "t", "client_key": "k1"})
    again = await web_client.post(TASKS, json={"title": "t", "client_key": "k1"})
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["key"] == again.json()["key"]


async def test_create_returns_full_task_shape(service_client):
    task = await create(service_client, title="Shape", labels=["A"])
    assert task["key"].startswith("T-")
    assert set(task) == {
        "key",
        "project",
        "title",
        "body",
        "kind",
        "status",
        "labels",
        "assignee_session",
        "blocked",
        "open_decisions",
        "waiting_sessions",
        "todo",
        "decisions",
        "comments",
        "comments_total",
        "events",
        "events_total",
        "spec",
        "url",
        "version",
        "created_at",
        "updated_at",
    }
    assert task["url"] == f"https://tasks.example.test/tasks/{task['key']}"
    assert (task["kind"], task["status"], task["version"]) == ("task", "open", 1)
    assert task["project"] == "default"
    assert task["blocked"] is False
    assert task["todo"] == [] and task["decisions"] == [] and task["comments"] == []
    assert task["events"][0]["kind"] == "created"
    assert task["events"][0]["actor_session"] == S1


@pytest.mark.parametrize(
    "body",
    [
        {"title": "t", "kind": "epic"},
        {"title": "t", "status": "blocked"},
        {"title": ""},
        {"title": "x" * 201},
        {},
    ],
)
async def test_create_validates_kind_status(service_client, body):
    r = await service_client.post(TASKS, json=body, headers=agent())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


async def test_create_title_bounds_accepted(service_client):
    await create(service_client, title="x")
    await create(service_client, title="x" * 200)


async def test_idea_kind_defaults_status_idea(service_client):
    assert (await create(service_client, kind="idea"))["status"] == "idea"


async def test_patch_stale_version_is_412(service_client):
    task = await create(service_client)
    url = f"{TASKS}/{task['key']}"
    ok = await service_client.patch(url, json={"version": 1, "title": "n"}, headers=agent())
    assert ok.status_code == 200
    stale = await service_client.patch(url, json={"version": 1, "title": "m"}, headers=agent())
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "stale_version"
    missing = await service_client.patch(url, json={"title": "m"}, headers=agent())
    assert missing.status_code == 422


async def test_patch_bumps_version_and_logs_event(service_client, conn):
    task = await create(service_client)
    r = await service_client.patch(
        f"{TASKS}/{task['key']}",
        json={"version": 1, "title": "new", "body": "b", "status": "in_progress"},
        headers=agent(S2, "mx-2"),
    )
    assert r.status_code == 200
    body = r.json()
    assert (body["version"], body["title"], body["status"]) == (2, "new", "in_progress")
    kinds = [e["kind"] for e in body["events"]]
    assert kinds == ["created", "updated", "status_changed"]
    updated = body["events"][1]
    assert updated["detail"] == {"fields": ["title", "body"]}
    assert updated["actor_session"] == S2 and updated["actor_slug"] == "mx-2"
    assert body["events"][2]["detail"] == {"from": "open", "to": "in_progress"}
    assert body["updated_at"] >= task["updated_at"]


async def test_patch_without_changes_keeps_version(service_client):
    task = await create(service_client, title="same")
    r = await service_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 1, "title": "same"}, headers=agent()
    )
    assert r.status_code == 200
    assert r.json()["version"] == 1
    assert [e["kind"] for e in r.json()["events"]] == ["created"]


async def test_patch_unknown_and_empty_patch(service_client):
    assert (
        await service_client.patch(
            f"{TASKS}/T-99999", json={"version": 1, "title": "x"}, headers=agent()
        )
    ).status_code == 404
    task = await create(service_client)
    r = await service_client.patch(f"{TASKS}/{task['key']}", json={"version": 1}, headers=agent())
    assert r.status_code == 422


async def test_every_mutation_notifies_task_changed(service_client, db_url):
    import asyncio

    import psycopg

    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as listener:
        await listener.execute("LISTEN task_changed")
        task = await create(service_client)
        await service_client.patch(
            f"{TASKS}/{task['key']}", json={"version": 1, "title": "n"}, headers=agent()
        )
        await service_client.post(
            f"{TASKS}/{task['key']}/comments", json={"body": "c"}, headers=agent()
        )
        got = []
        async with asyncio.timeout(5):
            async for n in listener.notifies():
                got.append(n.payload)
                if len(got) == 3:
                    break
    assert got == [task["key"][2:]] * 3


async def test_list_default_hides_done_and_dropped(service_client):
    open_ = await create(service_client, title="open")
    idea = await create(service_client, title="idea", kind="idea")
    prog = await create(service_client, title="prog", status="in_progress")
    await create(service_client, title="done", status="done")
    await create(service_client, title="dropped", status="dropped")
    r = await service_client.get(TASKS, headers=agent())
    assert r.status_code == 200
    keys = {t["key"] for t in r.json()["items"]}
    assert keys == {open_["key"], idea["key"], prog["key"]}
    assert r.json()["next_cursor"] is None


async def test_list_status_all_and_done_filters(service_client):
    await create(service_client, title="open")
    done = await create(service_client, title="done", status="done")
    dropped = await create(service_client, title="dropped", status="dropped")
    all_ = await service_client.get(TASKS, params={"status": "all"}, headers=agent())
    assert len(all_.json()["items"]) == 3
    only_done = await service_client.get(TASKS, params={"status": "done"}, headers=agent())
    assert [t["key"] for t in only_done.json()["items"]] == [done["key"]]
    only_dropped = await service_client.get(TASKS, params={"status": "dropped"}, headers=agent())
    assert [t["key"] for t in only_dropped.json()["items"]] == [dropped["key"]]
    bad = await service_client.get(TASKS, params={"status": "weird"}, headers=agent())
    assert bad.status_code == 422


async def keys_of(client, headers=None, **params) -> set[str]:
    r = await client.get(TASKS, params=params, headers=headers if headers is not None else agent())
    assert r.status_code == 200, r.text
    return {t["key"] for t in r.json()["items"]}


async def test_list_filters_by_session_assignee_and_mine(service_client):
    mine = await create(service_client, headers=agent(S2), assignee_session=S1)
    theirs = await create(service_client, headers=agent(S2), assignee_session=S3)
    await create(service_client, headers=agent(S2))
    assert await keys_of(service_client, session=S1) == {mine["key"]}
    assert await keys_of(service_client, session=S3) == {theirs["key"]}
    assert await keys_of(service_client, mine="true") == {mine["key"]}
    assert await keys_of(service_client, headers=agent(S3), mine="true") == {theirs["key"]}
    assert len(await keys_of(service_client, mine="false")) == 3


async def test_list_session_filter_matches_creator(service_client):
    created = await create(service_client, headers=agent(S1))
    await create(service_client, headers=agent(S2))
    assert await keys_of(service_client, session=S1) == {created["key"]}


async def test_list_session_filter_matches_asking_session_of_any_decision(service_client, conn):
    asked_open = await create(service_client, headers=agent(S2))
    asked_answered = await create(service_client, headers=agent(S2))
    await create(service_client, headers=agent(S2))
    add_decision(conn, asked_open["key"], S1, "open")
    add_decision(conn, asked_answered["key"], S1, "answered")
    assert await keys_of(service_client, session=S1) == {
        asked_open["key"],
        asked_answered["key"],
    }


async def test_list_waiting_restricts_to_open_decisions(service_client, conn):
    waiting = await create(service_client, headers=agent(S2))
    answered = await create(service_client, headers=agent(S2))
    await create(service_client, headers=agent(S2))
    add_decision(conn, waiting["key"], S3, "open")
    add_decision(conn, answered["key"], S3, "answered")
    assert await keys_of(service_client, waiting="true") == {waiting["key"]}
    assert len(await keys_of(service_client, waiting="false")) == 3


async def test_list_waiting_with_session_needs_open_decision_asked_by_that_session(
    service_client, conn
):
    own = await create(service_client, headers=agent(S1))
    assigned = await create(service_client, headers=agent(S2), assignee_session=S1)
    other_asker = await create(service_client, headers=agent(S1))
    answered = await create(service_client, headers=agent(S1))
    add_decision(conn, own["key"], S1, "open")
    add_decision(conn, assigned["key"], S1, "open")
    add_decision(conn, other_asker["key"], S2, "open")
    add_decision(conn, answered["key"], S1, "answered")
    assert await keys_of(service_client, session=S1, waiting="true") == {
        own["key"],
        assigned["key"],
    }
    assert await keys_of(service_client, session=S1) == {
        own["key"],
        assigned["key"],
        other_asker["key"],
        answered["key"],
    }


async def test_list_mine_conflicting_with_session_and_on_web_is_422(service_client, web_client):
    r = await service_client.get(TASKS, params={"mine": "true", "session": S2}, headers=agent(S1))
    assert r.status_code == 422
    assert (await web_client.get(TASKS, params={"mine": "true"})).status_code == 422
    assert (await web_client.get(TASKS, params={"session": S1})).status_code == 200


async def test_list_blocked_first(service_client, conn):
    first = await create(service_client, title="first")
    blocked = await create(service_client, title="blocked")
    last = await create(service_client, title="last")
    add_decision(conn, blocked["key"], S1, "open", slug="mx-asker")
    add_decision(conn, blocked["key"], S2, "open", slug="mx-two")
    add_decision(conn, blocked["key"], S2, "open", slug="mx-two")
    add_decision(conn, first["key"], S1, "answered")
    r = await service_client.get(TASKS, headers=agent())
    items = r.json()["items"]
    assert [t["key"] for t in items] == [blocked["key"], last["key"], first["key"]]
    assert items[0]["blocked"] is True and items[0]["open_decisions"] == 3
    assert {w["session_id"]: w["slug"] for w in items[0]["waiting_sessions"]} == {
        S1: "mx-asker",
        S2: "mx-two",
    }
    assert len(items[0]["waiting_sessions"]) == 2
    assert items[1]["blocked"] is False and items[1]["waiting_sessions"] == []
    assert len(items[0]["decisions"]) == 3


async def test_list_pagination_cursor(service_client, conn):
    made = [await create(service_client, title=f"t{i}") for i in range(5)]
    add_decision(conn, made[0]["key"], S1)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        r = await service_client.get(TASKS, params=params, headers=agent())
        body = r.json()
        seen += [t["key"] for t in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert pages == 3
    assert seen[0] == made[0]["key"]
    assert sorted(seen) == sorted(t["key"] for t in made)
    assert len(set(seen)) == 5
    bad = await service_client.get(TASKS, params={"cursor": "zzz"}, headers=agent())
    assert bad.status_code == 422


async def test_get_unknown_key_404(service_client):
    for key in ("T-99999", "T-0", "nope", "T-1x"):
        r = await service_client.get(f"{TASKS}/{key}", headers=agent())
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


async def test_get_returns_task(service_client):
    task = await create(service_client, body="hello")
    r = await service_client.get(f"{TASKS}/{task['key']}", headers=agent(S3))
    assert r.status_code == 200
    assert r.json()["body"] == "hello"


async def test_title_with_newlines_and_markdown_roundtrips(service_client):
    title = "# Fix `bug`\n\n- [ ] **bold** _it_ é中\ntail"
    body = "line1\r\n\r\n```py\nprint(1)\n```"
    task = await create(service_client, title=title, body=body)
    got = (await service_client.get(f"{TASKS}/{task['key']}", headers=agent())).json()
    assert got["title"] == title and got["body"] == body


async def test_comment_does_not_create_notice(service_client, web_client, conn):
    task = await create(service_client)
    r = await service_client.post(
        f"{TASKS}/{task['key']}/comments", json={"body": "agent says"}, headers=agent(S2, "mx-2")
    )
    assert r.status_code == 201
    r2 = await web_client.post(f"{TASKS}/{task['key']}/comments", json={"body": "user says"})
    assert r2.status_code == 201
    body = r2.json()
    assert [(c["author_kind"], c["author_session"], c["body"]) for c in body["comments"]] == [
        ("agent", S2, "agent says"),
        ("user", None, "user says"),
    ]
    assert [e["kind"] for e in body["events"]] == ["created", "comment_added", "comment_added"]
    assert body["version"] == 1
    assert conn.execute("SELECT count(*) FROM notices").fetchone()[0] == 0


async def test_comment_validation_and_unknown_task(service_client):
    task = await create(service_client)
    r = await service_client.post(
        f"{TASKS}/{task['key']}/comments", json={"body": ""}, headers=agent()
    )
    assert r.status_code == 422
    r = await service_client.post(f"{TASKS}/T-99999/comments", json={"body": "x"}, headers=agent())
    assert r.status_code == 404


async def test_status_edit_does_not_create_notice(service_client, web_client, conn):
    task = await create(service_client)
    r = await web_client.patch(f"{TASKS}/{task['key']}", json={"version": 1, "status": "done"})
    assert r.status_code == 200
    assert conn.execute("SELECT count(*) FROM notices").fetchone()[0] == 0


async def test_labels_normalised_on_create_and_patch(service_client):
    task = await create(service_client, labels=["  Foo ", "BAR", "foo", "a:b_c.d-e"])
    assert task["labels"] == ["foo", "bar", "a:b_c.d-e"]
    r = await service_client.patch(
        f"{TASKS}/{task['key']}",
        json={"version": 1, "labels": [" Baz", "BAZ", "x"]},
        headers=agent(),
    )
    assert r.json()["labels"] == ["baz", "x"]


@pytest.mark.parametrize(
    "labels",
    [["has space"], ["a/b"], [""], ["a" * 41], [f"l{i}" for i in range(21)], ["unié"]],
)
async def test_label_invalid_chars_or_length_422(service_client, labels):
    r = await service_client.post(TASKS, json={"title": "t", "labels": labels}, headers=agent())
    assert r.status_code == 422
    task = await create(service_client)
    r = await service_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 1, "labels": labels}, headers=agent()
    )
    assert r.status_code == 422
    r = await service_client.get(TASKS, params={"label": labels}, headers=agent())
    assert r.status_code == 422


async def test_list_label_filter_any_match(service_client):
    a = await create(service_client, labels=["alpha", "beta"])
    await create(service_client, labels=["gamma"])
    await create(service_client)
    assert await keys_of(service_client, label="beta") == {a["key"]}
    assert await keys_of(service_client, label="BETA") == {a["key"]}
    assert await keys_of(service_client, label="nothing") == set()


async def test_list_label_filter_repeated_params(service_client):
    a = await create(service_client, labels=["alpha"])
    b = await create(service_client, labels=["beta", "x"])
    await create(service_client, labels=["gamma"])
    r = await service_client.get(f"{TASKS}?label=alpha&label=beta", headers=agent())
    assert {t["key"] for t in r.json()["items"]} == {a["key"], b["key"]}


async def test_label_added_logs_label_added_event_with_detail(service_client, conn):
    task = await create(service_client, labels=["Foo", "bar"])
    created = [e for e in task["events"] if e["kind"] == "label_added"]
    assert [e["detail"] for e in created] == [{"labels": ["foo", "bar"]}]
    r = await service_client.patch(
        f"{TASKS}/{task['key']}",
        json={"version": 1, "labels": ["foo", "baz", "qux"]},
        headers=agent(S2),
    )
    events = r.json()["events"]
    added = [e for e in events if e["kind"] == "label_added"]
    assert [e["detail"] for e in added] == [{"labels": ["foo", "bar"]}, {"labels": ["baz", "qux"]}]
    assert added[1]["actor_session"] == S2
    assert [e["kind"] for e in events if e["kind"] == "labels_changed"] == ["labels_changed"]
    rows = conn.execute(
        "SELECT count(*) FROM events WHERE kind = 'label_added' AND scanned_at IS NULL"
    ).fetchone()
    assert rows[0] == 2


async def test_create_without_labels_logs_no_label_added(service_client):
    task = await create(service_client)
    assert [e["kind"] for e in task["events"]] == ["created"]


async def test_label_removal_logs_no_label_added_event(service_client):
    task = await create(service_client, labels=["a", "b"])
    r = await service_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 1, "labels": ["a"]}, headers=agent()
    )
    events = r.json()["events"]
    assert [e["kind"] for e in events] == ["created", "label_added", "labels_changed"]
    assert events[-1]["detail"] == {"labels": ["a"], "removed": ["b"]}
    same = await service_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 2, "labels": ["a"]}, headers=agent()
    )
    assert same.json()["version"] == 2


async def test_label_removal_creates_no_notice(service_client, conn):
    task = await create(service_client, labels=["a", "b"])
    await service_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 1, "labels": []}, headers=agent()
    )
    assert conn.execute("SELECT count(*) FROM notices").fetchone()[0] == 0


async def test_assignee_self_resolves_to_actor_session(service_client):
    task = await create(service_client, headers=agent(S2), assignee_self=True)
    assert task["assignee_session"] == S2
    other = await create(service_client, headers=agent(S2))
    assert other["assignee_session"] is None
    r = await service_client.patch(
        f"{TASKS}/{other['key']}", json={"version": 1, "assignee_self": True}, headers=agent(S3)
    )
    assert r.json()["assignee_session"] == S3
    r = await service_client.patch(
        f"{TASKS}/{other['key']}", json={"version": 2, "assignee_session": None}, headers=agent(S3)
    )
    assert r.json()["assignee_session"] is None
    r = await service_client.patch(
        f"{TASKS}/{other['key']}", json={"version": 3, "assignee_session": S1}, headers=agent(S3)
    )
    assert r.json()["assignee_session"] == S1


async def test_assignee_self_and_assignee_session_together_422(service_client):
    r = await service_client.post(
        TASKS, json={"title": "t", "assignee_self": True, "assignee_session": S1}, headers=agent()
    )
    assert r.status_code == 422
    task = await create(service_client)
    r = await service_client.patch(
        f"{TASKS}/{task['key']}",
        json={"version": 1, "assignee_self": True, "assignee_session": S1},
        headers=agent(),
    )
    assert r.status_code == 422


async def test_assignee_self_on_web_app_422(web_client):
    r = await web_client.post(TASKS, json={"title": "t", "assignee_self": True})
    assert r.status_code == 422
    task = (await web_client.post(TASKS, json={"title": "t"})).json()
    r = await web_client.patch(f"{TASKS}/{task['key']}", json={"version": 1, "assignee_self": True})
    assert r.status_code == 422
    r = await web_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 1, "assignee_session": S1}
    )
    assert r.json()["assignee_session"] == S1


async def test_web_created_task_has_user_creator(web_client, conn):
    await web_client.post(TASKS, json={"title": "t"})
    row = conn.execute("SELECT created_by_kind, created_by_session FROM tasks").fetchone()
    assert row == ("user", None)


async def test_service_routes_require_bearer_and_session(service_client):
    assert (await service_client.get(TASKS)).status_code == 401
    assert (await service_client.get(TASKS, headers=AUTH)).status_code == 400


SPECS = f"{PREFIX}/specs"


@pytest.fixture
def any_project(service_client, web_client):
    """Every project slug is an active orchestrator project."""
    from agents_taskboard.projects import ProjectLookup

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"archived": False})

    for client in (service_client, web_client):
        client.app.state.projects = ProjectLookup(
            "http://orch.test", "t", transport=httpx.MockTransport(handle)
        )


async def make_spec(client, project: str, name: str = "s") -> str:
    r = await client.post(
        SPECS, json={"name": name, "title": "t", "project": project}, headers=agent()
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


async def test_task_with_spec_takes_spec_project(service_client, web_client, any_project):
    spec = await make_spec(service_client, "a")
    r = await web_client.post(TASKS, json={"title": "t", "spec": spec})
    assert r.status_code == 201, r.text
    assert (r.json()["project"], r.json()["spec"]) == ("a", spec)
    r = await service_client.post(
        TASKS, json={"title": "t", "spec": spec, "project": "a"}, headers=agent(projects="a")
    )
    assert (r.status_code, r.json()["spec"]) == (201, spec)
    r = await web_client.get(f"{SPECS}/{spec}")
    assert len(r.json()["tasks"]) == 2


async def test_explicit_other_project_422_project_mismatch(service_client, web_client, any_project):
    spec = await make_spec(service_client, "a")
    r = await web_client.post(TASKS, json={"title": "t", "spec": spec, "project": "b"})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "project_mismatch")
    task = await create(web_client, {}, project="b")
    r = await web_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 1, "spec": spec, "project": "b"}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (422, "project_mismatch")


async def test_patch_links_task_to_spec_and_unlinks(service_client, web_client, any_project):
    spec = await make_spec(service_client, "a")
    task = await create(web_client, {})
    r = await web_client.patch(f"{TASKS}/{task['key']}", json={"version": 1, "spec": spec})
    assert r.status_code == 200, r.text
    assert (r.json()["project"], r.json()["spec"], r.json()["version"]) == ("a", spec, 2)
    linked = [e for e in r.json()["events"] if e["kind"] == "spec_set"]
    assert linked and linked[0]["detail"] == {"from": None, "to": spec}
    r = await web_client.patch(f"{TASKS}/{task['key']}", json={"version": 2, "spec": None})
    assert (r.status_code, r.json()["spec"], r.json()["project"]) == (200, None, "a")


async def test_task_move_detaches_spec(service_client, web_client, any_project):
    spec = await make_spec(service_client, "a")
    task = (await web_client.post(TASKS, json={"title": "t", "spec": spec})).json()
    r = await web_client.patch(f"{TASKS}/{task['key']}", json={"version": 1, "project": "b"})
    assert r.status_code == 200, r.text
    assert (r.json()["project"], r.json()["spec"]) == ("b", None)


async def test_spec_move_keeps_tasks_attached(service_client, web_client, any_project):
    spec = await make_spec(service_client, "a")
    task = (await web_client.post(TASKS, json={"title": "t", "spec": spec})).json()
    r = await web_client.patch(f"{SPECS}/{spec}", json={"version": 1, "project": "b"})
    assert r.status_code == 200
    r = await web_client.get(f"{TASKS}/{task['key']}")
    assert (r.json()["project"], r.json()["spec"]) == ("b", spec)


async def test_spec_out_of_scope_404(service_client, web_client, any_project):
    spec = await make_spec(service_client, "a")
    r = await service_client.post(
        TASKS, json={"title": "t", "spec": spec}, headers=agent(projects="b")
    )
    unknown = await service_client.post(
        TASKS, json={"title": "t", "spec": "S-999999"}, headers=agent(projects="b")
    )
    assert (r.status_code, unknown.status_code) == (404, 404)
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["message"] == unknown.json()["error"]["message"].replace(
        "S-999999", spec
    )
    task = await create(service_client, agent(projects="b"))
    r = await service_client.patch(
        f"{TASKS}/{task['key']}", json={"version": 1, "spec": spec}, headers=agent(projects="b")
    )
    assert r.status_code == 404
    await web_client.delete(f"{SPECS}/{spec}")
    r = await web_client.post(TASKS, json={"title": "t", "spec": spec})
    assert r.status_code == 404


async def test_list_tasks_by_spec(service_client, web_client, any_project):
    spec = await make_spec(service_client, "a")
    other = await make_spec(service_client, "a", name="other")
    linked = (await web_client.post(TASKS, json={"title": "t", "spec": spec})).json()
    await web_client.post(TASKS, json={"title": "t", "spec": other})
    await create(web_client, {})
    r = await web_client.get(TASKS, params={"spec": spec})
    assert [t["key"] for t in r.json()["items"]] == [linked["key"]]
    r = await web_client.get(TASKS, params={"spec": "bogus"})
    assert r.status_code == 422
