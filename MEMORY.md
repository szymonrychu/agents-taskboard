# agents-taskboard memory

Lessons that outlive a session. Newest first.

## 2026-11-02 - todo checklist

- Todo edits (`PUT /tasks/{key}/todo`, `PATCH /tasks/{key}/todo/{item_id}`) follow the comment rule: they touch `updated_at` (which also row-locks the task), log `todo_replaced` {count} / `todo_updated` {item_id, fields}, NOTIFY, never bump `version` (so they cannot make a title/body editor stale) and never create a notice. PUT deletes and reinserts, so item ids change on every replace; PATCH only addresses ids from the latest read and answers 404 for an item of another task.
- `api/tasks.py` helpers (`task_id_of`, `pool_of`, `load_one`) are module level now so `api/todos.py` reuses them; its router is mounted on both apps like the tasks router. Text is one line (no CR/LF), 1-200 chars; max 100 items.

## 2026-11-02 - tasks api

- Both apps mount the same router (`api/tasks.build_router(actor_dep)`); only the actor dependency differs. Do not add `from __future__ import annotations` to it: FastAPI cannot resolve the locally built `Annotated` alias and turns `actor` into a required query param (every route 422s).
- `session=<id>` on the list matches assignee, creator or asking session of any decision (leader ruling); `waiting=true` needs an open decision (asked by `session` when given). `mine=true` is `session=<caller session>` (422 on the web app or when it conflicts with `session`). List order is blocked first then newest id, keyset cursor `(blocked, id)`; the response is `{items, next_cursor}` and each item is the full task JSON.
- Web users have no session, and the `(created_by_session, client_key)` unique index treats NULLs as distinct, so user creates serialise per key with `pg_advisory_xact_lock(hashtext(key))` plus a select. Agents rely on `ON CONFLICT DO NOTHING`.
- Patch locks the row `FOR UPDATE`, compares `version`, then does the guarded `UPDATE ... WHERE version`. A patch that changes nothing returns the task without bumping the version or logging. Comments touch `updated_at` only: no version bump, so they never make an editor stale.
- Events: `created`, `updated` {fields}, `status_changed` {from,to}, `label_added` {labels:[added]} (create and patch, the single writer), `labels_changed` {labels, removed} on removal, `comment_added` {comment_id}. NOTIFY is `pg_notify('task_changed', id)` in the same transaction.

## 2026-11-02 - skeleton, settings, schema, actors

- agents-common is pinned at v0.7.0 (agents-schedule still pins v0.3.0): v0.7.0 is the first tag with `ServiceTokenGate` (`agents_common.service_gate`). The local `code/agents-common` checkout is stale (v0.4.0 era), read the tags with `git show vX:path`. The migrator entry point is `agents_common.db.migrate.run_migrations(url, package)` (not `agents_common.migrate`); `db.migrate(settings)` wraps it and `taskboard migrate` is the chart init container.
- Two apps, one error shape and probes: `create_web_app` (SourceAllowlist on TASKBOARD_ALLOWED_CIDRS, never reads agent headers, the answer route goes here) and `create_service_app` (ServiceTokenGate with the relay token, `actor_from_service` reads `X-Orch-Session`/`X-Orch-Slug`). Identity headers are trusted only because the gate ran first; a missing `X-Orch-Session` is 400 `actor_required`, the slug is optional (None).
- The relay token is the raw secret in `TASKBOARD_RELAY_TOKEN` (a SecretStr), because `ServiceTokenGate` takes raw tokens and hashes them itself; the orchestrator relay (Task 9) sends the same value.
- Migration 0001 has `ON DELETE CASCADE` from every child table to `tasks`; the task key is `'T-'||id` in code only. `comments` and `events` carry the same actor columns (kind/session/slug).
- Tests need Docker (testcontainers postgres:18 via the agents-common pytest plugin). Without Docker, run against any Postgres (PG16 verified) by overriding the plugin's `pg_url` fixture in a throwaway plugin with `-p no:agents_common -p <plugin>`.

## 2026-11-02 - scaffold

Created as a separate Python microservice (not a part of the orchestrator) because the taskboard owns its own Postgres and the hard rules allow cross-service access only through HTTP or the queue, one writer per table. The scaffold has no runtime dependencies yet; FastAPI, psycopg and agents-common are added with the first feature task that needs them.
