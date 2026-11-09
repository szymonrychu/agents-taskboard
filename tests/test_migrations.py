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
    assert run_migrations(empty_db_url, PACKAGE) == ["0001_taskboard.sql", "0002_projects.sql"]
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
    assert run_migrations(empty_db_url, PACKAGE) == ["0002_projects.sql"]
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
