"""Plan header and plan steps (base design 3.17): a plan is a header on the spec (`spec_plans`)
plus an ordered set of steps, and a step is a task of the spec with a number (`tasks.step`,
dense from 1).

Every change of the step set or order runs under `SELECT ... FOR UPDATE` on the spec row, names
the `specs.steps_version` it was based on and bumps it, so a reorder from the web and a bulk set
from an agent cannot interleave (`StaleVersion(current)` for the loser). Dependencies link steps
of one spec; a write that would close a cycle is refused (`DependencyCycle`). A step is ready
when it is open and every dependency is done or dropped; `next_step` claims the first ready step
with `FOR UPDATE SKIP LOCKED`, so two implementers never get the same one.

A step created by `set_steps` or `insert_step` gets the client key `plan-<spec id>-<n>`; the key
is cleared whenever the step loses or changes its number, so a stored key always names the
current number and a new step can never collide with an old one.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agents_taskboard.api.actors import Actor
from agents_taskboard.db import specs
from agents_taskboard.db.events import (
    append_event,
    append_spec_event,
    notify_changed,
    notify_spec_changed,
)
from agents_taskboard.db.scope import scoped
from agents_taskboard.db.specs import InvalidRequest, NotFound, TooLarge
from agents_taskboard.db.tasks import STEP_BODY_CAP, parse_key, task_key

__all__ = [
    "DependencyCycle",
    "InvalidRequest",
    "NotFound",
    "StaleVersion",
    "StepInProgress",
    "TooLarge",
]

MAX_REFS = 20
MAX_TODO = 100
TODO_TEXT_CAP = 200
TITLE_CAP = 200
GOAL_CAP = 500
ARCHITECTURE_CAP = 4000
TECH_STACK_CAP = 1000
MAX_PLAN_ITEMS = 50
PLAN_ITEM_CAP = 1000
MODELS = ("haiku", "sonnet", "opus", "fable")
REF_KINDS = ("pr", "commit", "branch", "tag", "url")
STARTED = ("in_progress", "done")


class StaleVersion(specs.StaleVersion):
    """The plan `version` or the spec's `steps_version` moved; `current_version` is the current
    one."""


class StepInProgress(Exception):
    """A step set would remove a step that already started or is done."""


class DependencyCycle(Exception):
    pass


# -- validation ------------------------------------------------------------------------------


def check_step_body(body: str) -> None:
    specs.check_body(body, STEP_BODY_CAP)


def check_refs(refs: list[Any]) -> list[Any]:
    """`[{kind: pr | commit | branch | tag | url, value}]`, at most 20; returns `refs`."""
    if len(refs) > MAX_REFS:
        raise InvalidRequest(f"more than {MAX_REFS} refs")
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"kind", "value"}
            or ref["kind"] not in REF_KINDS
            or not isinstance(ref["value"], str)
            or not ref["value"]
        ):
            raise InvalidRequest("a ref is {kind: pr | commit | branch | tag | url, value}")
    return refs


def _check_model(model: str | None) -> None:
    if model is not None and model not in MODELS:
        raise InvalidRequest("model is one of haiku, sonnet, opus, fable")


def _check_title(title: str) -> None:
    if not 1 <= len(title) <= TITLE_CAP:
        raise InvalidRequest(f"title is 1 to {TITLE_CAP} characters")


def _check_todo(todo: list[str]) -> None:
    if len(todo) > MAX_TODO:
        raise InvalidRequest(f"more than {MAX_TODO} todo items")
    for text in todo:
        if not 1 <= len(text) <= TODO_TEXT_CAP or "\n" in text or "\r" in text:
            raise InvalidRequest(f"a todo item is one line of 1 to {TODO_TEXT_CAP} characters")


def _check_plan_items(name: str, items: list[str]) -> None:
    if len(items) > MAX_PLAN_ITEMS:
        raise InvalidRequest(f"{name} has more than {MAX_PLAN_ITEMS} items")
    if any(len(item) > PLAN_ITEM_CAP for item in items):
        raise InvalidRequest(f"an item of {name} is longer than {PLAN_ITEM_CAP}")


def _check_step(title: str, body: str, model: str | None) -> None:
    _check_title(title)
    check_step_body(body)
    _check_model(model)


# -- plan header -----------------------------------------------------------------------------

_PLAN_COLUMNS = (
    "p.goal, p.architecture, p.tech_stack, p.constraints, p.review_focus, p.version,"
    " p.updated_at, p.updated_by_kind, p.updated_by_session, p.updated_by_slug"
)
_EMPTY_PLAN: dict[str, Any] = {
    "goal": "",
    "architecture": "",
    "tech_stack": "",
    "constraints": [],
    "review_focus": [],
    "version": 0,
    "updated_at": None,
    "updated_by": None,
}


def _plan_json(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    updated_by = specs._by("updated_by", row)  # pops the raw columns
    return {**row, "updated_by": updated_by}


async def get_plan(conn: psycopg.AsyncConnection, spec_id: int) -> dict[str, Any]:
    """The plan header of a live spec; version 0 and empty fields before the first write.
    Raises NotFound for an absent or deleted spec (callers check the actor's scope)."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        f"SELECT p.spec_id, {_PLAN_COLUMNS} FROM specs s"
        " LEFT JOIN spec_plans p ON p.spec_id = s.id WHERE s.id = %s AND s.deleted_at IS NULL",
        (spec_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound
    if row.pop("spec_id") is None:
        return dict(_EMPTY_PLAN)
    return _plan_json(row)


async def put_plan(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    spec_id: int,
    *,
    version: int,
    goal: str,
    architecture: str,
    tech_stack: str,
    constraints: list[str],
    review_focus: list[str],
) -> dict[str, Any]:
    """Replace the plan header if `version` is current (0 before the first write); returns the
    new header. Raises NotFound, StaleVersion, InvalidRequest."""
    for name, value, cap in (
        ("goal", goal, GOAL_CAP),
        ("architecture", architecture, ARCHITECTURE_CAP),
        ("tech_stack", tech_stack, TECH_STACK_CAP),
    ):
        if len(value) > cap:
            raise InvalidRequest(f"{name} is longer than {cap}")
    _check_plan_items("constraints", constraints)
    _check_plan_items("review_focus", review_focus)
    async with conn.transaction():
        await _lock_spec(conn, actor, spec_id)
        cur = await conn.execute("SELECT version FROM spec_plans WHERE spec_id = %s", (spec_id,))
        row = await cur.fetchone()
        current = row[0] if row else 0
        if current != version:
            raise StaleVersion(current)
        cur = await conn.cursor(row_factory=dict_row).execute(
            "INSERT INTO spec_plans AS p (spec_id, goal, architecture, tech_stack, constraints,"
            " review_focus, updated_by_kind, updated_by_session, updated_by_slug)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (spec_id) DO UPDATE SET goal = excluded.goal,"
            " architecture = excluded.architecture, tech_stack = excluded.tech_stack,"
            " constraints = excluded.constraints, review_focus = excluded.review_focus,"
            " version = p.version + 1, updated_at = now(),"
            " updated_by_kind = excluded.updated_by_kind,"
            " updated_by_session = excluded.updated_by_session,"
            " updated_by_slug = excluded.updated_by_slug"
            f" RETURNING {_PLAN_COLUMNS}",
            (
                spec_id,
                goal,
                architecture,
                tech_stack,
                Jsonb(constraints),
                Jsonb(review_focus),
                actor.kind,
                actor.session_id,
                actor.slug,
            ),
        )
        plan = _plan_json(await cur.fetchone())
        await conn.execute("UPDATE specs SET updated_at = now() WHERE id = %s", (spec_id,))
        await append_spec_event(conn, spec_id, actor, "plan_set", {"version": plan["version"]})
        await notify_spec_changed(conn, spec_id)
        return plan


# -- reading steps ---------------------------------------------------------------------------

_READY = (
    "t.status = 'open' AND NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks p"
    " ON p.id = d.depends_on WHERE d.task_id = t.id AND p.status NOT IN ('done', 'dropped'))"
)
_STEP_COLUMNS = (
    "t.id, t.step, t.title, t.status, t.assignee_session, t.model, t.refs, t.version,"
    f" ({_READY}) AS ready,"
    " (SELECT count(*) FROM todo_items i WHERE i.task_id = t.id) AS todo_total,"
    " (SELECT count(*) FROM todo_items i WHERE i.task_id = t.id AND i.status = 'done')"
    " AS todo_done,"
    " ARRAY(SELECT d.depends_on FROM task_deps d JOIN tasks p ON p.id = d.depends_on"
    " WHERE d.task_id = t.id ORDER BY p.step NULLS LAST, p.id) AS deps"
)


def _step_json(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    return {
        "key": task_key(row.pop("id")),
        **row,
        "deps": [task_key(d) for d in row["deps"]],
    }


async def _step_rows(
    conn: psycopg.AsyncConnection, where: str, params: tuple
) -> list[dict[str, Any]]:
    cur = await conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_STEP_COLUMNS} FROM tasks t WHERE {where} ORDER BY t.step, t.id", params
    )
    return [_step_json(r) for r in await cur.fetchall()]


async def list_steps(
    conn: psycopg.AsyncConnection, spec_id: int
) -> tuple[list[dict[str, Any]], int]:
    """The steps of a live spec in order, without bodies (key, step, title, status, assignee,
    model, deps, refs, ready, todo counts, task version), and the spec's `steps_version`. Raises
    NotFound for an absent or deleted spec (callers check the actor's scope)."""
    cur = await conn.execute(
        "SELECT steps_version FROM specs WHERE id = %s AND deleted_at IS NULL", (spec_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound
    steps = await _step_rows(conn, "t.spec_id = %s AND t.step IS NOT NULL", (spec_id,))
    return steps, row[0]


# -- writing steps ---------------------------------------------------------------------------


async def _lock_spec(conn: psycopg.AsyncConnection, actor: Actor, spec_id: int) -> dict[str, Any]:
    """Row-lock the live spec within the actor's scope; NotFound otherwise."""
    scope, params = scoped(actor, "s.project")
    cur = await conn.cursor(row_factory=dict_row).execute(
        "SELECT s.id, s.project, s.steps_version FROM specs s"
        f" WHERE s.id = %s AND s.deleted_at IS NULL{scope} FOR UPDATE",
        (spec_id, *params),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound
    return row


async def _lock_steps(
    conn: psycopg.AsyncConnection, actor: Actor, spec_id: int, steps_version: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Lock the spec (checking `steps_version`) and its live steps; returns both, steps in
    order."""
    spec = await _lock_spec(conn, actor, spec_id)
    if spec["steps_version"] != steps_version:
        raise StaleVersion(spec["steps_version"])
    cur = await conn.cursor(row_factory=dict_row).execute(
        "SELECT id, step, status, title, body, model, labels FROM tasks"
        " WHERE spec_id = %s AND step IS NOT NULL ORDER BY step, id FOR UPDATE",
        (spec_id,),
    )
    return spec, await cur.fetchall()


async def _bump(conn: psycopg.AsyncConnection, spec_id: int) -> int:
    cur = await conn.execute(
        "UPDATE specs SET steps_version = steps_version + 1, updated_at = now() WHERE id = %s"
        " RETURNING steps_version",
        (spec_id,),
    )
    return (await cur.fetchone())[0]


async def _create_step(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    spec: dict[str, Any],
    n: int,
    *,
    title: str,
    body: str,
    model: str | None,
    labels: list[str],
) -> int:
    cur = await conn.execute(
        "INSERT INTO tasks (title, body, kind, status, labels, created_by_kind,"
        " created_by_session, created_by_slug, client_key, project, spec_id, step, model)"
        " VALUES (%s, %s, 'task', 'open', %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            title,
            body,
            labels,
            actor.kind,
            actor.session_id,
            actor.slug,
            f"plan-{spec['id']}-{n}",
            spec["project"],
            spec["id"],
            n,
            model,
        ),
    )
    task_id = (await cur.fetchone())[0]
    await append_event(
        conn, task_id, actor, "created", {"kind": "task", "status": "open", "step": n}
    )
    if labels:
        await append_event(conn, task_id, actor, "label_added", {"labels": labels})
    await notify_changed(conn, task_id)
    return task_id


async def _update_step(
    conn: psycopg.AsyncConnection, actor: Actor, row: dict[str, Any], item: dict[str, Any]
) -> bool:
    """Apply the title, body, model, labels and todo of `item` to an existing step; returns
    whether anything changed."""
    task_id = row["id"]
    wanted = {"title": item["title"], "body": item["body"], "model": item.get("model")}
    if "labels" in item:
        wanted["labels"] = item["labels"]
    changed = {k: v for k, v in wanted.items() if row[k] != v}
    if changed:
        sets = ", ".join(f"{k} = %s" for k in changed)
        await conn.execute(
            f"UPDATE tasks SET {sets}, version = version + 1, updated_at = now() WHERE id = %s",
            (*changed.values(), task_id),
        )
        plain = [k for k in ("title", "body", "model") if k in changed]
        if plain:
            await append_event(conn, task_id, actor, "updated", {"fields": plain})
        if "labels" in changed:
            old, new = row["labels"], changed["labels"]
            added = [lab for lab in new if lab not in old]
            removed = [lab for lab in old if lab not in new]
            if added:
                await append_event(conn, task_id, actor, "label_added", {"labels": added})
            if removed:
                await append_event(
                    conn, task_id, actor, "labels_changed", {"labels": new, "removed": removed}
                )
    todo_changed = "todo" in item and await _replace_todo(conn, actor, task_id, item["todo"])
    if changed or todo_changed:
        await notify_changed(conn, task_id)
    return bool(changed) or todo_changed


async def _replace_todo(
    conn: psycopg.AsyncConnection, actor: Actor, task_id: int, texts: list[str]
) -> bool:
    """Replace the todo list by `texts` unless it already reads the same; an item whose text is
    kept keeps its status. Returns whether the list changed."""
    cur = await conn.execute(
        "SELECT text, status FROM todo_items WHERE task_id = %s ORDER BY position, id", (task_id,)
    )
    current = await cur.fetchall()
    if [text for text, _ in current] == texts:
        return False
    statuses: dict[str, list[str]] = {}
    for text, status in current:
        statuses.setdefault(text, []).append(status)
    items = [(text, (statuses.get(text) or ["todo"]).pop(0)) for text in texts]
    await conn.execute("DELETE FROM todo_items WHERE task_id = %s", (task_id,))
    if items:
        async with conn.cursor() as c:
            await c.executemany(
                "INSERT INTO todo_items (task_id, text, status, position) VALUES (%s, %s, %s, %s)",
                [(task_id, text, status, pos) for pos, (text, status) in enumerate(items)],
            )
    await append_event(conn, task_id, actor, "todo_replaced", {"count": len(items)})
    return True


async def _replace_deps(
    conn: psycopg.AsyncConnection, actor: Actor, task_id: int, depends_on: list[int]
) -> bool:
    """Set the dependencies of one step (task ids); returns whether they changed. The caller
    checks for cycles afterwards."""
    if task_id in depends_on:
        raise DependencyCycle
    cur = await conn.execute("SELECT depends_on FROM task_deps WHERE task_id = %s", (task_id,))
    if {r[0] for r in await cur.fetchall()} == set(depends_on):
        return False
    await conn.execute("DELETE FROM task_deps WHERE task_id = %s", (task_id,))
    if depends_on:
        async with conn.cursor() as c:
            await c.executemany(
                "INSERT INTO task_deps (task_id, depends_on) VALUES (%s, %s)",
                [(task_id, dep) for dep in dict.fromkeys(depends_on)],
            )
    await append_event(
        conn, task_id, actor, "deps_set", {"depends_on": [task_key(d) for d in depends_on]}
    )
    await notify_changed(conn, task_id)
    return True


async def _refuse_cycle(conn: psycopg.AsyncConnection, spec_id: int) -> None:
    """DependencyCycle when any step of the spec reaches itself through its dependencies."""
    cur = await conn.execute(
        "WITH RECURSIVE walk (start, node) AS ("
        " SELECT d.task_id, d.depends_on FROM task_deps d JOIN tasks t ON t.id = d.task_id"
        " WHERE t.spec_id = %s"
        " UNION SELECT w.start, d.depends_on FROM walk w JOIN task_deps d ON d.task_id = w.node"
        ") SELECT 1 FROM walk WHERE start = node LIMIT 1",
        (spec_id,),
    )
    if await cur.fetchone() is not None:
        raise DependencyCycle


async def _renumber(
    conn: psycopg.AsyncConnection, actor: Actor, ordered: list[dict[str, Any]]
) -> bool:
    """Number `ordered` (existing step rows) 1..n; a step whose number changes loses its client
    key. The unique (spec_id, step) is deferred, so the swaps commit together."""
    moved = False
    for position, row in enumerate(ordered, start=1):
        if row["id"] is None or row["step"] == position:
            continue  # the slot of a step being inserted, or already in place
        moved = True
        await conn.execute(
            "UPDATE tasks SET step = %s, client_key = NULL, updated_at = now() WHERE id = %s",
            (position, row["id"]),
        )
        await append_event(
            conn, row["id"], actor, "step_moved", {"from": row["step"], "to": position}
        )
        await notify_changed(conn, row["id"])
    return moved


async def _drop_step(conn: psycopg.AsyncConnection, actor: Actor, row: dict[str, Any]) -> None:
    """Close an unstarted step removed from the set: `dropped`, no number, no dependencies."""
    task_id = row["id"]
    await conn.execute(
        "UPDATE tasks SET status = 'dropped', step = NULL, client_key = NULL,"
        " version = version + 1, updated_at = now() WHERE id = %s",
        (task_id,),
    )
    await conn.execute(
        "DELETE FROM task_deps WHERE task_id = %s OR depends_on = %s", (task_id, task_id)
    )
    await append_event(conn, task_id, actor, "step_removed", {"step": row["step"]})
    if row["status"] != "dropped":
        await append_event(
            conn, task_id, actor, "status_changed", {"from": row["status"], "to": "dropped"}
        )
    await notify_changed(conn, task_id)


def _check_set(steps: list[dict[str, Any]]) -> None:
    numbers = [item["n"] for item in steps]
    if numbers != list(range(1, len(steps) + 1)):
        raise InvalidRequest("steps are numbered densely from 1, in order")
    for item in steps:
        _check_step(item["title"], item["body"], item.get("model"))
        if "todo" in item:
            _check_todo(item["todo"])
        deps = item.get("depends_on") or []
        if item["n"] in deps:
            raise DependencyCycle
        if any(not isinstance(d, int) or not 1 <= d <= len(steps) for d in deps):
            raise InvalidRequest("depends_on names steps of the set")


async def set_steps(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    spec_id: int,
    *,
    steps_version: int,
    steps: list[dict[str, Any]],
) -> list[str]:
    """Replace the step set, keyed by `n` (`[{n, title, body, depends_on: [n], model?, labels?,
    todo?: [text]}]`, `n` dense from 1). Existing steps with that number are updated, new
    numbers are created, removed unstarted steps are closed `dropped` and lose their number. A
    set that changes nothing changes nothing (no version bump). Returns the step keys in order.
    Raises NotFound, StaleVersion, StepInProgress, DependencyCycle, InvalidRequest, TooLarge."""
    _check_set(steps)
    async with conn.transaction():
        spec, live = await _lock_steps(conn, actor, spec_id, steps_version)
        by_number = {row["step"]: row for row in live}
        removed = [row for row in live if row["step"] > len(steps)]
        if any(row["status"] in STARTED for row in removed):
            raise StepInProgress
        ids: dict[int, int] = {}
        created, updated = [], []
        for row in removed:
            await _drop_step(conn, actor, row)
        for item in steps:
            n = item["n"]
            row = by_number.get(n)
            if row is None:
                ids[n] = await _create_step(
                    conn,
                    actor,
                    spec,
                    n,
                    title=item["title"],
                    body=item["body"],
                    model=item.get("model"),
                    labels=item.get("labels") or [],
                )
                if item.get("todo"):
                    await _replace_todo(conn, actor, ids[n], item["todo"])
                created.append(ids[n])
            else:
                ids[n] = row["id"]
                if await _update_step(conn, actor, row, item):
                    updated.append(row["id"])
        for item in steps:
            task_id = ids[item["n"]]
            deps = [ids[d] for d in item.get("depends_on") or []]
            changed = await _replace_deps(conn, actor, task_id, deps)
            if changed and task_id not in created and task_id not in updated:
                updated.append(task_id)
        await _refuse_cycle(conn, spec_id)
        if created or updated or removed:
            version = await _bump(conn, spec_id)
            await append_spec_event(
                conn,
                spec_id,
                actor,
                "steps_set",
                {
                    "steps_version": version,
                    "created": [task_key(i) for i in created],
                    "updated": [task_key(i) for i in updated],
                    "dropped": [task_key(row["id"]) for row in removed],
                },
            )
            await notify_spec_changed(conn, spec_id)
        return [task_key(ids[item["n"]]) for item in steps]


async def insert_step(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    spec_id: int,
    *,
    steps_version: int,
    after: int | None,
    title: str,
    body: str,
    depends_on: list[int],
    model: str | None,
) -> dict[str, Any]:
    """Insert one step after step `after` (0: first, None: last); later steps are renumbered.
    `depends_on` names current step numbers. Returns the new step (as listed). Raises NotFound,
    StaleVersion, InvalidRequest, TooLarge."""
    _check_step(title, body, model)
    async with conn.transaction():
        spec, live = await _lock_steps(conn, actor, spec_id, steps_version)
        numbers = [row["step"] for row in live]
        if after is None:
            position = len(live)
        elif after == 0:
            position = 0
        elif after in numbers:
            position = numbers.index(after) + 1
        else:
            raise InvalidRequest("after names no step of the spec")
        by_number = {row["step"]: row["id"] for row in live}
        if any(d not in by_number for d in depends_on):
            raise InvalidRequest("depends_on names steps of the spec")
        await _renumber(
            conn, actor, [*live[:position], {"id": None, "step": None}, *live[position:]]
        )
        task_id = await _create_step(
            conn, actor, spec, position + 1, title=title, body=body, model=model, labels=[]
        )
        await _replace_deps(conn, actor, task_id, [by_number[d] for d in depends_on])
        version = await _bump(conn, spec_id)
        await append_spec_event(
            conn,
            spec_id,
            actor,
            "step_inserted",
            {"steps_version": version, "key": task_key(task_id), "step": position + 1},
        )
        await notify_spec_changed(conn, spec_id)
        (step,) = await _step_rows(conn, "t.id = %s", (task_id,))
        return step


async def reorder(
    conn: psycopg.AsyncConnection,
    actor: Actor,
    spec_id: int,
    *,
    steps_version: int,
    order: list[str],
) -> int:
    """Renumber the steps in the order of `order` (task keys, every live step exactly once);
    returns the `steps_version` after the write. Raises NotFound, StaleVersion,
    InvalidRequest."""
    async with conn.transaction():
        _, live = await _lock_steps(conn, actor, spec_id, steps_version)
        ids = [parse_key(key) for key in order]
        by_id = {row["id"]: row for row in live}
        if len(ids) != len(live) or set(ids) != set(by_id):
            raise InvalidRequest("order names every step of the spec exactly once")
        if not await _renumber(conn, actor, [by_id[i] for i in ids]):
            return steps_version
        version = await _bump(conn, spec_id)
        await append_spec_event(
            conn, spec_id, actor, "steps_reordered", {"steps_version": version, "order": order}
        )
        await notify_spec_changed(conn, spec_id)
        return version


async def set_deps(
    conn: psycopg.AsyncConnection, actor: Actor, task_id: int, depends_on: list[int]
) -> None:
    """Set one step's dependencies (task ids of live steps of the same spec). Raises NotFound
    (also outside the actor's projects), InvalidRequest (the task or a dependency is not a step
    of that spec), DependencyCycle."""
    scope, params = scoped(actor)
    async with conn.transaction():
        cur = await conn.execute(
            f"SELECT t.spec_id FROM tasks t WHERE t.id = %s{scope}", (task_id, *params)
        )
        row = await cur.fetchone()
        if row is None:
            raise NotFound
        if row[0] is None:
            raise InvalidRequest("the task is not a step")
        spec_id = row[0]
        # spec first, then the task: the lock order of every step-set write
        await _lock_spec(conn, actor, spec_id)
        cur = await conn.execute(
            "SELECT spec_id, step FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
        )
        row = await cur.fetchone()
        if row is None or row[0] != spec_id or row[1] is None:
            raise InvalidRequest("the task is not a step")
        wanted = list(dict.fromkeys(depends_on))
        cur = await conn.execute(
            "SELECT count(*) FROM tasks WHERE id = ANY(%s) AND spec_id = %s AND step IS NOT NULL",
            (wanted, spec_id),
        )
        if task_id not in wanted and (await cur.fetchone())[0] != len(wanted):
            raise InvalidRequest("depends_on names steps of the same spec")
        if await _replace_deps(conn, actor, task_id, wanted):
            await conn.execute("UPDATE tasks SET updated_at = now() WHERE id = %s", (task_id,))
        await _refuse_cycle(conn, spec_id)


async def next_step(
    conn: psycopg.AsyncConnection, actor: Actor, spec_id: int, *, claim: bool
) -> dict[str, Any] | None:
    """The first ready step in order, or None. With `claim` the caller becomes its assignee and
    it goes `in_progress` in one guarded update; the candidate row is taken with
    `FOR UPDATE SKIP LOCKED`, so concurrent claims get different steps. Raises NotFound."""
    scope, params = scoped(actor, "s.project")
    async with conn.transaction():
        cur = await conn.execute(
            f"SELECT 1 FROM specs s WHERE s.id = %s AND s.deleted_at IS NULL{scope}",
            (spec_id, *params),
        )
        if await cur.fetchone() is None:
            raise NotFound
        lock = " FOR UPDATE OF t SKIP LOCKED" if claim else ""
        cur = await conn.execute(
            f"SELECT t.id FROM tasks t WHERE t.spec_id = %s AND t.step IS NOT NULL AND {_READY}"
            f" ORDER BY t.step LIMIT 1{lock}",
            (spec_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        task_id = row[0]
        if claim:
            cur = await conn.execute(
                "UPDATE tasks SET status = 'in_progress', assignee_session = %s,"
                " version = version + 1, updated_at = now() WHERE id = %s AND status = 'open'"
                " RETURNING id",
                (actor.session_id, task_id),
            )
            if await cur.fetchone() is None:
                return None
            await append_event(
                conn, task_id, actor, "status_changed", {"from": "open", "to": "in_progress"}
            )
            await append_event(conn, task_id, actor, "updated", {"fields": ["assignee_session"]})
            await notify_changed(conn, task_id)
        (step,) = await _step_rows(conn, "t.id = %s", (task_id,))
        return step


async def gate_task_id(conn: psycopg.AsyncConnection, spec_id: int) -> int | None:
    """The spec's gate task: its lowest-id task that is not a step and not dropped (the
    improvement's own task, T-116 Q2 A)."""
    cur = await conn.execute(
        "SELECT min(id) FROM tasks WHERE spec_id = %s AND step IS NULL AND status <> 'dropped'",
        (spec_id,),
    )
    return (await cur.fetchone())[0]


async def get_gate(conn: psycopg.AsyncConnection, actor: Actor, spec_id: int) -> dict[str, Any]:
    """The spec's gate (T-116 Q2 A, Q3 A): status, the versions of the current approval (null
    unless approved), the gate task and its decisions other than withdrawn ones. `gate` is
    `none` with no such decision, `waiting` with an open one, `open` when all are answered;
    `approved` never gates. Raises NotFound (also outside the actor's projects)."""
    scope, params = scoped(actor, "s.project")
    cur = await conn.execute(
        f"SELECT s.status FROM specs s WHERE s.id = %s AND s.deleted_at IS NULL{scope}",
        (spec_id, *params),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound
    status = row[0]
    approved_versions = None
    if status == "approved":
        cur = await conn.execute(
            "SELECT detail -> 'versions' FROM events WHERE spec_id = %s AND kind = 'spec_updated'"
            " AND detail -> 'status' ->> 'to' = 'approved' ORDER BY id DESC LIMIT 1",
            (spec_id,),
        )
        found = await cur.fetchone()
        approved_versions = found[0] if found else {}
    gate_id = await gate_task_id(conn, spec_id)
    decisions: list[dict[str, Any]] = []
    if gate_id is not None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT id, question, status, answer FROM decisions"
            " WHERE task_id = %s AND status <> 'withdrawn' ORDER BY id",
            (gate_id,),
        )
        decisions = list(await cur.fetchall())
    if not decisions:
        gate = "none"
    elif any(d["status"] == "open" for d in decisions):
        gate = "waiting"
    else:
        gate = "open"
    return {
        "key": specs.spec_key(spec_id),
        "status": status,
        "approved_versions": approved_versions,
        "gate_task": None if gate_id is None else task_key(gate_id),
        "decisions": decisions,
        "gate": gate,
        "approved": status == "approved",
    }
