# agents-taskboard memory

Lessons that outlive a session. Newest first.

## 2026-11-02 - notifier

- agents-common v0.7.0 names: `OrchestratorClient.post_message(session_id, text)` has NO `lane` parameter and the orchestrator messages API takes only `{"text"}`; a posted message is the regular lane by default, so the notifier calls it without a lane (the briefed `lane="regular"` does not exist). `advisory_singleton(pool, key)` and `FakeOrchestrator` (`agents_common.fake_orchestrator`, `fail_next("post_message", status)`) match the brief.
- The notifier claims one row with `SELECT ... FOR UPDATE SKIP LOCKED` and holds that transaction across the send and `mark_sent` (leader ruling), so a second notifier skips the row. Delivery is at-least-once: a crash between the send and the commit re-sends after restart (rare duplicate); a pending row is never lost. In production the notifier also runs under the advisory lock `agents-taskboard-notifier`, so one instance sends at a time.
- A 404 from the orchestrator (session gone) ends the notice (marked sent); any other error or an unreachable orchestrator leaves it pending. Each notice has its own retry schedule (2026-11-02, fixing head-of-line blocking where the oldest failing row was retried first and stalled all later ones): `notices.attempts` and `notices.next_attempt_at` (in 0001, unreleased); `claim_notice` takes the oldest pending row with `next_attempt_at <= now()`; after a failed send the send transaction ends and a separate guarded UPDATE does `attempts + 1` and `next_attempt_at = now() + min(poll * 2^attempts, backoff_max)` (`TASKBOARD_NOTIFIER_POLL_SECONDS` 5, `TASKBOARD_NOTIFIER_BACKOFF_MAX_SECONDS` 60). The loop does not sleep after a failure, only when nothing is due (NOTIFY wake or poll). A persistently failing row retries about once a minute forever and delays nobody.
- The text is only `notice_text(task_key, public_url)`; the notifier never reads title or body.

## 2026-11-02 - decisions and notice outbox

- Ask is agent-only (403 `agent_required` for a user) on both apps; answer is built only when `build_router(..., answerable=True)`, which only the web app passes, so the service app has no route and no OpenAPI path. Withdraw is on both apps: 404 unknown decision or other task, 403 not the asking session (users included), 409 `not_open` when not open.
- Answer and withdraw are one status-guarded `UPDATE ... WHERE status='open' RETURNING`; the race loser matches zero rows and gets 409 `already_answered`. They touch `updated_at` only, never bump `version`, like comments and todo. Events: `decision_asked`, `decision_answered`, `decision_withdrawn` {decision_id}, all with NOTIFY in the same transaction.
- The notice insert is `INSERT ... ON CONFLICT DO NOTHING` against the partial unique index `notices_one_pending_idx`, so a second answer to the same asker while a notice is pending adds no row.
- Closing a task (done/dropped) does NOT withdraw its open decisions, and answering an open decision on a closed task works (leader decision 2026-11-02): the asking session still needs its answer.
- Answer is stored as `{"choice": i, "label": ...}` or `{"text": ...}`; a body must carry exactly one of `choice` (index, 422 out of range) and a non-blank `text`, plus an optional `note`. Ask/withdraw/answer return the decision JSON (same shape as in the task JSON plus `task_key`); an ask replayed on (task, asking session, client_key) returns the existing one with 200.

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
