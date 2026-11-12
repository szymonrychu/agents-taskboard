import hashlib
from importlib import resources

import psycopg
import pytest
from agents_common.db.migrate import run_migrations

PACKAGE = "agents_taskboard"


def add_task(conn, **cols) -> int:
    values = {
        "title": "t",
        "body": "",
        "kind": "task",
        "status": "open",
        "created_by_kind": "user",
        **cols,
    }
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    return conn.execute(
        f"INSERT INTO tasks ({names}) VALUES ({marks}) RETURNING id", tuple(values.values())
    ).fetchone()[0]


def add_decision(conn, task_id, **cols) -> int:
    values = {
        "task_id": task_id,
        "asking_session": "s1",
        "question": "q?",
        "status": "open",
        **cols,
    }
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    return conn.execute(
        f"INSERT INTO decisions ({names}) VALUES ({marks}) RETURNING id",
        tuple(
            psycopg.types.json.Jsonb(v) if k in ("answer", "options") else v
            for k, v in values.items()
        ),
    ).fetchone()[0]


def add_notice(conn, task_id, session="s1", reason="decision", status="pending") -> None:
    conn.execute(
        "INSERT INTO notices (task_id, session_id, reason, status) VALUES (%s, %s, %s, %s)",
        (task_id, session, reason, status),
    )


def test_applies_twice_idempotent(empty_db_url):
    assert run_migrations(empty_db_url, PACKAGE) == [
        "0001_taskboard.sql",
        "0002_projects.sql",
        "0003_decision_withdrawal.sql",
        "0004_specs.sql",
        "0005_plan_steps.sql",
        "0006_external_changes.sql",
    ]
    assert run_migrations(empty_db_url, PACKAGE) == []
    with psycopg.connect(empty_db_url) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }
    assert {"tasks", "comments", "decisions", "todo_items", "events", "notices"} <= tables


def test_status_check_rejects_unknown(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        add_task(conn, status="bogus")
    with pytest.raises(psycopg.errors.CheckViolation):
        add_task(conn, kind="bogus")
    for status in ("idea", "open", "in_progress", "done", "dropped"):
        add_task(conn, status=status)


def test_task_key_is_not_stored(conn):
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks'"
        )
    }
    assert "key" not in cols
    assert "id" in cols


def test_one_pending_notice_per_task_session_reason(conn):
    task = add_task(conn)
    add_notice(conn, task)
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_notice(conn, task)
    add_notice(conn, task, session="s2")
    conn.execute("UPDATE notices SET status = 'sent', sent_at = now() WHERE session_id = 's1'")
    add_notice(conn, task)


def test_one_label_notice_per_task_session_even_after_sent(conn):
    task = add_task(conn)
    add_notice(conn, task, reason="label", status="sent")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_notice(conn, task, reason="label", status="pending")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_notice(conn, task, reason="label", status="sent")
    add_notice(conn, task, reason="label", session="s2")


def test_notice_reason_check_rejects_unknown(conn):
    task = add_task(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        add_notice(conn, task, reason="other")
    with pytest.raises(psycopg.errors.CheckViolation):
        add_notice(conn, task, status="bogus")


def test_tasks_labels_default_empty_and_gin_index_exists(conn):
    task = add_task(conn)
    assert conn.execute("SELECT labels FROM tasks WHERE id = %s", (task,)).fetchone()[0] == []
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'tasks' AND indexdef ILIKE '%gin%'"
    ).fetchone()
    assert row is not None and "labels" in row[0]
    with pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute("UPDATE tasks SET labels = NULL WHERE id = %s", (task,))


def test_answered_decision_requires_answer(conn):
    task = add_task(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        add_decision(conn, task, status="answered")
    add_decision(conn, task, status="answered", answer={"choice": "a"})
    with pytest.raises(psycopg.errors.CheckViolation):
        add_decision(conn, task, status="bogus")


def test_decision_client_key_unique_per_task_and_asker(conn):
    task, other = add_task(conn), add_task(conn)
    add_decision(conn, task, client_key="k")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_decision(conn, task, client_key="k")
    add_decision(conn, task, client_key="k", asking_session="s2")
    add_decision(conn, other, client_key="k")
    add_decision(conn, task)
    add_decision(conn, task)


def test_task_client_key_unique_per_creator(conn):
    add_task(conn, created_by_session="s1", client_key="k")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_task(conn, created_by_session="s1", client_key="k")
    add_task(conn, created_by_session="s2", client_key="k")
    add_task(conn, created_by_session="s1")
    add_task(conn, created_by_session="s1")


def test_events_scanned_at_and_partial_index_exist(conn):
    task = add_task(conn)
    event = conn.execute(
        "INSERT INTO events (task_id, actor_kind, kind) VALUES (%s, 'user', 'label_added') "
        "RETURNING id",
        (task,),
    ).fetchone()[0]
    assert (
        conn.execute("SELECT scanned_at FROM events WHERE id = %s", (event,)).fetchone()[0] is None
    )
    defs = [
        r[0] for r in conn.execute("SELECT indexdef FROM pg_indexes WHERE tablename = 'events'")
    ]
    assert any("label_added" in d and "scanned_at IS NULL" in d for d in defs)


def test_required_indexes_exist(conn):
    defs = [
        r[0] for r in conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'")
    ]
    assert any("ON public.tasks" in d and "(status)" in d for d in defs)
    assert any("ON public.decisions" in d and "'open'" in d for d in defs)


def test_child_rows_cascade_with_task(conn):
    task = add_task(conn)
    add_decision(conn, task)
    conn.execute(
        "INSERT INTO comments (task_id, author_kind, body) VALUES (%s, 'user', 'c')", (task,)
    )
    conn.execute(
        "INSERT INTO todo_items (task_id, text, status, position) VALUES (%s, 'x', 'todo', 0)",
        (task,),
    )
    add_notice(conn, task)
    conn.execute("DELETE FROM tasks WHERE id = %s", (task,))
    for table in ("decisions", "comments", "todo_items", "notices"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_todo_status_check(conn):
    task = add_task(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO todo_items (task_id, text, status, position) VALUES (%s, 'x', 'bogus', 0)",
            (task,),
        )
    for status in ("todo", "inprogress", "done"):
        conn.execute(
            "INSERT INTO todo_items (task_id, text, status, position) VALUES (%s, 'x', %s, 0)",
            (task, status),
        )


def test_migration_backfills_default(empty_db_url):
    """Tasks and events written under 0001 get project `default` from 0002."""
    first = (resources.files(PACKAGE) / "migrations" / "0001_taskboard.sql").read_text()
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        conn.execute(first)
        conn.execute(
            "CREATE TABLE schema_migrations (package text NOT NULL, name text NOT NULL,"
            " checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now(),"
            " PRIMARY KEY (package, name))"
        )
        conn.execute(
            "INSERT INTO schema_migrations (package, name, checksum) VALUES (%s, %s, %s)",
            (PACKAGE, "0001_taskboard.sql", hashlib.sha256(first.encode()).hexdigest()),
        )
        task = add_task(conn)
        conn.execute(
            "INSERT INTO events (task_id, actor_kind, kind) VALUES (%s, 'user', 'created')",
            (task,),
        )
    assert run_migrations(empty_db_url, PACKAGE) == [
        "0002_projects.sql",
        "0003_decision_withdrawal.sql",
        "0004_specs.sql",
        "0005_plan_steps.sql",
        "0006_external_changes.sql",
    ]
    with psycopg.connect(empty_db_url) as conn:
        assert conn.execute("SELECT project FROM tasks").fetchall() == [("default",)]
        assert conn.execute("SELECT project FROM events").fetchall() == [("default",)]
        assert add_task(conn) and conn.execute(
            "SELECT count(*) FROM tasks WHERE project = 'default'"
        ).fetchone() == (2,)
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute("UPDATE tasks SET project = NULL")


def test_tasks_project_status_index_exists(conn):
    defs = [r[0] for r in conn.execute("SELECT indexdef FROM pg_indexes WHERE tablename = 'tasks'")]
    assert any("(project, status)" in d for d in defs)


def test_withdrawal_columns_exist(conn):
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'decisions'"
        )
    }
    assert {"withdraw_reason", "superseded_by", "answered_elsewhere"} <= cols


def test_withdrawn_row_needs_reason_and_exactly_one_outcome(conn):
    task = add_task(conn)
    other = add_decision(conn, task)
    for cols in (
        {},
        {"withdraw_reason": "r"},
        {"answered_elsewhere": "x"},
        {"withdraw_reason": "r", "superseded_by": other, "answered_elsewhere": "x"},
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            add_decision(conn, task, status="withdrawn", **cols)
    add_decision(conn, task, status="withdrawn", withdraw_reason="r", superseded_by=other)
    add_decision(conn, task, status="withdrawn", withdraw_reason="r", answered_elsewhere="x")
    add_decision(conn, task, status="open")


def test_legacy_withdrawn_row_survives_check(empty_db_url):
    """A withdrawn row written before 0003 stays; the CHECK is NOT VALID."""
    for name in ("0001_taskboard.sql", "0002_projects.sql"):
        sql = (resources.files(PACKAGE) / "migrations" / name).read_text()
        with psycopg.connect(empty_db_url, autocommit=True) as conn:
            conn.execute(sql)
            if name.startswith("0001"):
                conn.execute(
                    "CREATE TABLE schema_migrations (package text NOT NULL, name text NOT NULL,"
                    " checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now(),"
                    " PRIMARY KEY (package, name))"
                )
            conn.execute(
                "INSERT INTO schema_migrations (package, name, checksum) VALUES (%s, %s, %s)",
                (PACKAGE, name, hashlib.sha256(sql.encode()).hexdigest()),
            )
        if name.startswith("0001"):
            with psycopg.connect(empty_db_url, autocommit=True) as conn:
                legacy = add_decision(conn, add_task(conn), status="withdrawn")
    assert run_migrations(empty_db_url, PACKAGE) == [
        "0003_decision_withdrawal.sql",
        "0004_specs.sql",
        "0005_plan_steps.sql",
        "0006_external_changes.sql",
    ]
    with psycopg.connect(empty_db_url) as conn:
        row = conn.execute(
            "SELECT status, withdraw_reason, superseded_by, answered_elsewhere FROM decisions"
            " WHERE id = %s",
            (legacy,),
        ).fetchone()
        assert row == ("withdrawn", None, None, None)


def add_spec(conn, **cols) -> int:
    values = {"name": "s", "title": "t", "created_by_kind": "user", **cols}
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    return conn.execute(
        f"INSERT INTO specs ({names}) VALUES ({marks}) RETURNING id", tuple(values.values())
    ).fetchone()[0]


def columns(conn, table: str) -> dict[str, str]:
    """column -> is_nullable of `table`."""
    return dict(
        conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = %s",
            (table,),
        ).fetchall()
    )


def test_specs_schema(conn):
    assert {"specs", "spec_versions", "spec_comments"} <= {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    specs = columns(conn, "specs")
    for col in (
        "project",
        "name",
        "title",
        "status",
        "labels",
        "assignee_session",
        "assignee_slug",
        "created_by_kind",
        "created_by_session",
        "created_by_slug",
        "client_key",
        "version",
        "deleted_at",
        "created_at",
        "updated_at",
    ):
        assert col in specs, col
    assert "created_by_device" not in specs
    versions = columns(conn, "spec_versions")
    for col in (
        "spec_id",
        "path",
        "version",
        "body",
        "sha256",
        "size",
        "note",
        "sources",
        "created_by_kind",
        "client_key",
        "created_at",
    ):
        assert col in versions, col
    assert versions["body"] == "YES"
    comments = columns(conn, "spec_comments")
    for col in (
        "spec_id",
        "path",
        "version",
        "anchor",
        "body",
        "suggestion",
        "suggestion_status",
        "accepted_version",
        "decided_at",
        "decided_by_kind",
        "attachments",
        "resolved_at",
        "resolved_by_kind",
        "created_by_kind",
        "client_key",
        "created_at",
    ):
        assert col in comments, col
    assert columns(conn, "tasks")["spec_id"] == "YES"
    events = columns(conn, "events")
    assert events["task_id"] == "YES" and events["spec_id"] == "YES"
    notices = columns(conn, "notices")
    assert notices["task_id"] == "YES" and notices["spec_id"] == "YES"


def test_spec_checks(conn):
    for bad in (
        {"name": "Bad"},
        {"name": "-x"},
        {"name": "x" * 81},
        {"title": ""},
        {"status": "bogus"},
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            add_spec(conn, **bad)
    spec = add_spec(conn, name="ok-1")
    for bad in ("../x.md", "a.txt", "A.md", "a/../b.md"):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO spec_versions (spec_id, path, version, created_by_kind)"
                " VALUES (%s, %s, 1, 'user')",
                (spec, bad),
            )


def test_spec_name_unique_until_deleted(conn):
    first = add_spec(conn, name="n")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_spec(conn, name="n")
    add_spec(conn, name="n", project="b")
    conn.execute("UPDATE specs SET deleted_at = now() WHERE id = %s", (first,))
    add_spec(conn, name="n")


def test_task_spec_fk_cascades_project_and_nulls_on_delete(conn):
    spec = add_spec(conn)
    task = add_task(conn, spec_id=spec)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        add_task(conn, spec_id=spec, project="b")
    conn.execute("UPDATE specs SET project = 'b' WHERE id = %s", (spec,))
    assert conn.execute("SELECT project FROM tasks WHERE id = %s", (task,)).fetchone() == ("b",)
    conn.execute("DELETE FROM specs WHERE id = %s", (spec,))
    assert conn.execute("SELECT project, spec_id FROM tasks WHERE id = %s", (task,)).fetchone() == (
        "b",
        None,
    )


def test_events_need_exactly_one_of_task_and_spec(conn):
    task, spec = add_task(conn), add_spec(conn)
    insert = "INSERT INTO events (task_id, spec_id, actor_kind, kind) VALUES (%s, %s, 'user', 'x')"
    for bad in ((None, None), (task, spec)):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, bad)
    conn.execute(insert, (task, None))
    conn.execute(insert, (None, spec))


def test_notices_spec_reason(conn):
    task, spec = add_task(conn), add_spec(conn)
    insert = (
        "INSERT INTO notices (task_id, spec_id, session_id, reason, status)"
        " VALUES (%s, %s, 's1', %s, 'pending')"
    )
    conn.execute(insert, (None, spec, "spec"))
    for bad in ((None, None, "spec"), (task, spec, "spec")):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, bad)


def test_migration_is_idempotent(conn):
    text = (resources.files(PACKAGE) / "migrations" / "0004_specs.sql").read_text()
    conn.execute(text)
    conn.execute(text)
    test_specs_schema(conn)


def test_plan_steps_schema(conn):
    assert "spec_plans" in {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    plans = columns(conn, "spec_plans")
    for col in (
        "spec_id",
        "goal",
        "architecture",
        "tech_stack",
        "constraints",
        "review_focus",
        "version",
        "updated_at",
        "updated_by_kind",
        "updated_by_session",
        "updated_by_slug",
    ):
        assert col in plans, col
    tasks = columns(conn, "tasks")
    assert tasks["step"] == "YES" and tasks["model"] == "YES" and tasks["refs"] == "NO"
    assert columns(conn, "specs")["steps_version"] == "NO"
    assert set(columns(conn, "task_deps")) == {"task_id", "depends_on"}
    spec = add_spec(conn)
    assert conn.execute("SELECT steps_version FROM specs WHERE id = %s", (spec,)).fetchone() == (0,)
    task = add_task(conn)
    assert conn.execute("SELECT refs::text, step FROM tasks WHERE id = %s", (task,)).fetchone() == (
        "[]",
        None,
    )


def test_step_checks(conn):
    spec = add_spec(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        add_task(conn, step=1)
    with pytest.raises(psycopg.errors.CheckViolation):
        add_task(conn, spec_id=spec, step=0)
    for bad in ("gpt", ""):
        with pytest.raises(psycopg.errors.CheckViolation):
            add_task(conn, model=bad)
    for model in ("haiku", "sonnet", "opus", "fable"):
        add_task(conn, model=model)
    with pytest.raises(psycopg.errors.CheckViolation):
        add_task(conn, refs=psycopg.types.json.Jsonb([{"kind": "pr", "value": "x"}] * 21))
    with pytest.raises(psycopg.errors.CheckViolation):
        add_task(conn, refs=psycopg.types.json.Jsonb({}))
    add_task(conn, refs=psycopg.types.json.Jsonb([{"kind": "pr", "value": "x"}] * 20))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO spec_plans (spec_id, goal, updated_by_kind) VALUES (%s, %s, 'user')",
            (spec, "g" * 501),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO spec_plans (spec_id, constraints, updated_by_kind)"
            " VALUES (%s, %s, 'user')",
            (spec, psycopg.types.json.Jsonb(["c"] * 51)),
        )


def test_step_numbers_unique_per_spec_and_deferred(conn):
    spec, other = add_spec(conn, name="a"), add_spec(conn, name="b")
    one = add_task(conn, spec_id=spec, step=1)
    two = add_task(conn, spec_id=spec, step=2)
    add_task(conn, spec_id=other, step=1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_task(conn, spec_id=spec, step=1)
    with conn.transaction():
        conn.execute("UPDATE tasks SET step = 2 WHERE id = %s", (one,))
        conn.execute("UPDATE tasks SET step = 1 WHERE id = %s", (two,))
    assert conn.execute(
        "SELECT step FROM tasks WHERE id = ANY(%s) ORDER BY id", ([one, two],)
    ).fetchall() == [(2,), (1,)]


def test_task_deps_rows(conn):
    one, two = add_task(conn), add_task(conn)
    conn.execute("INSERT INTO task_deps (task_id, depends_on) VALUES (%s, %s)", (two, one))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO task_deps (task_id, depends_on) VALUES (%s, %s)", (two, one))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO task_deps (task_id, depends_on) VALUES (%s, %s)", (one, one))
    conn.execute("DELETE FROM tasks WHERE id = %s", (one,))
    assert conn.execute("SELECT count(*) FROM task_deps").fetchone() == (0,)


def test_plan_steps_migration_is_idempotent(conn):
    text = (resources.files(PACKAGE) / "migrations" / "0005_plan_steps.sql").read_text()
    conn.execute(text)
    conn.execute(text)
    test_plan_steps_schema(conn)


def test_external_changes_migration_is_idempotent_and_allows_linked(conn):
    text = (resources.files(PACKAGE) / "migrations" / "0006_external_changes.sql").read_text()
    conn.execute(text)
    conn.execute(text)
    task = add_task(conn)
    conn.execute(
        "INSERT INTO notices (task_id, session_id, reason, status)"
        " VALUES (%s, 's1', 'linked', 'pending')",
        (task,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO notices (task_id, session_id, reason, status)"
            " VALUES (%s, 's1', 'linked', 'pending')",
            (task,),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO notices (task_id, session_id, reason, status)"
            " VALUES (%s, 's2', 'bogus', 'pending')",
            (task,),
        )
