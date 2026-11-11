# agents-taskboard memory

Lessons that outlive a session. Newest first.

## 2026-11-11 - spec notices, stream ids and catch-up (T-116 A5)

- The stream hub now treats the events table as its only source: NOTIFY on `task_changed` or `spec_changed` (one LISTEN per channel) only wakes the poll, which reads events past its id cursor and publishes `(event id, task or spec, project, payload)`. Why: every SSE event needs `id: <events.id>`, which a NOTIFY payload (the task id) did not carry, and a cursor read never repeats a change the way the old "heard" set had to avoid. A subscription keeps the latest change per task or spec (coalescing), sent in id order.
- Catch-up accepts both `Last-Event-ID` and `?after_id=` (the larger wins) because agents-web's `openStream` reconnects by building a new `EventSource` with `?after_id=` and cannot set the header. The handler subscribes first, waits for the hub cursor (`hub.ready()`), replays the latest event per key past the id, then skips live changes with an id at or below the last one sent. An id below `min(events.id) - 1` sends one `reset` (with the newest id) and no replay. The stream is user-only, so replay applies no actor scope, only `?project=` (on the event row's project).
- Spec notices: reason `spec`, `notices.spec_id`, pending unique index `(spec_id, session_id) WHERE reason = 'spec' AND status = 'pending'` (in 0004, still unreleased). Sent for user comments, user approval, user-written versions (also accept) and dismiss, to the assignee else the creator; agents' own writes notify nobody. The notifier also wakes on `spec_changed`.
- Gate semantics for the stage 4 planning tools (recorded here so they survive): the gate task is the improvement's own task linked to the spec, the lowest-id non-step task of the spec (T-116 Q2 A); the gate opens when its decisions are answered, approval stays optional (Q3 A).

## 2026-11-11 - tasks linked to specs, spec counts (T-116 A4)

- `spec` on task create and PATCH: the spec is looked up in the caller's scope first (404 with the unknown-key body), the task takes the spec's project (an explicit other `project` is 422 `project_mismatch`), and `require_target` still checks that project. A PATCH changing `project` without `spec` sets `spec_id = NULL` in the same UPDATE (the composite FK would refuse the move otherwise); only the spec's own move keeps its tasks attached (FK `ON UPDATE CASCADE`). A spec that moves between lookup and write trips the FK, mapped to 422 `project_mismatch` (`tasks.SpecMoved`). Link changes bump the task version and log `spec_set {from, to}`.
- `GET /projects/summary` is a FULL JOIN of the task counts and the spec counts per project, so a project with specs but no tasks is listed; deleted and archived specs are not counted. `tests/test_api_tasks.py` task shape gained `spec` (the task JSON changed).

## 2026-11-11 - spec comments, accept and dismiss (T-116 A3)

- Accept is one transaction under the spec row lock: lock the comment, check it has an open suggestion (422 `no_suggestion`, 409 `already_decided`), write the version through `write_version_locked` (a moved document raises `StaleVersion` and the whole transaction rolls back, so the comment stays `open`), then the status-guarded `UPDATE ... WHERE suggestion_status = 'open'`. It logs one event, `suggestion_accepted {path, version, comment_id}`, not a `document_put` plus a second one. The response is `{"version", "comment"}`.
- Accept and dismiss also resolve the comment (resolver = the deciding user, unless it was already resolved). A suggestion needs `path` (422 otherwise): accept writes the whole suggestion to that document. Comment replay is per (spec, creating session, client_key); users serialise per key with an advisory lock like task creates.
- Accept and dismiss exist only on user listeners, so the relay answers the plain 404 (no other method shares those paths, so no hidden handler is needed as for DELETE).

## 2026-11-11 - spec and document routes (T-116 A2)

- One router (`api/specs.build_spec_router(actor_dep, user_routes=...)`) on all three listeners. User-only routes exist only with `user_routes=True`; the relay gets a hidden (`include_in_schema=False`) `DELETE /specs/{key}` that answers the plain route 404, because without it Starlette answers 405 for a path that exists with other methods, and the design wants the relay to look like it has no such route.
- `412 stale_version` on a document carries `current_version` inside the error object (`ApiError(..., extra=...)`); the spec PATCH 412 also does. The 50-document cap answers `413 payload_too_large` like the byte caps ("over the cap the write is refused"); the count caps checked by pydantic (sources 20) are `422`.
- Document routes use the `{path:path}` converter so `research/a.md` works; `/versions` is registered before the catch-all document route. Paths end in `.md`, so a document can never be named `.../versions`. `tests/test_openapi_current.py` now compares `route.path_format` (the OpenAPI form without `:path`).

## 2026-11-11 - specs schema and repository (T-116 A1)

- Migration is `0004_specs.sql` (0003 was the decision withdrawal; the base design said 0003). No `created_by_device` columns: the device path is stage 3b. Session columns are `text` like `tasks`, not `uuid` as the base design wrote, so one comparison rule holds everywhere.
- `events.task_id` and `notices.task_id` are nullable now, with `CHECK (num_nonnulls(task_id, spec_id) = 1)`; the label scanner keeps its `JOIN tasks`, so spec events are skipped there. The stream poll ignores spec events until A5 adds `spec_changed`.
- A document write locks the spec row, compares `base_version` with the current version (a tombstone counts as a version, so re-creating a removed path sends the tombstone's version), and appends exactly one event. A version written on an `approved` spec sets `review` and bumps `specs.version` (so a PATCH based on the approved view is stale); other document writes touch only `updated_at`. Removing a document that is absent or already removed is `NotFound`.
- Soft delete detaches tasks explicitly (`spec_detached` task event each); the FK `ON DELETE SET NULL (spec_id)` only covers a hard delete, which no route does.

## 2026-11-09 - withdraw needs a reason and an outcome (T-102)

`POST .../withdraw` now requires a body: `reason` plus exactly one of `superseded_by` (decision id, any task in the caller's project scope, checked with `get_decision`, not itself) or `answered_elsewhere` (text). Why required: an agent that withdraws a question must say where the answer went, so the user sees no silent disappearance. Bodyless calls get 422 on purpose; no compatibility layer, so the orchestrator plugin must be deployed before this version. Migration 0003 adds the columns and the CHECK `decisions_withdrawn_has_outcome` as NOT VALID: withdrawn rows from before it have no outcome and are never rewritten (writes are guarded on status = 'open'), so validating would force a fake backfill; new rows are still checked. `[IF T-102 Q1=A]` restrict superseded_by to the same task and drop `superseded_by_task_key`; `[IF Q2=B]` backfill a reason and VALIDATE.

## 2026-11-09 - label notices scoped by project (projects P10)

- Label notices go only to sessions of the task's project plus superleaders. `SessionLookup.live_session_ids(labels, project)` sends `label=..&project=<task project>&superleaders=include`; the orchestrator combines the filters (label AND (project OR superleader)), and the client-side label check stays because `FakeOrchestrator` ignores query params. The project is read from the task at scan time (`claim_label_event` joins tasks), so a task moved before the scan notifies its new project. An archived project still lists in the orchestrator; an unknown one answers 422, which marks the event scanned with no notices (1fc6c49).

## 2026-11-09 - project scope (projects plan 3.6, task P9)

- `tasks.project` (NOT NULL, default `default`, index `(project, status)`) and `events.project` (nullable for future task-less rows, backfilled from tasks; `append_event` stamps the task's project at write time, so a move's `project_changed` event already carries the new project).
- The relay requires `X-Orch-Projects` (`*` or one slug; missing/blank/malformed is 400 `actor_required`), so the taskboard must deploy after the orchestrator release that sends it. Web and delegate actors keep `projects=None` (every project).
- Scope lives in SQL, never as a pre-check: `db/scope.scoped(actor)` is appended to the task lock (`touch_task`, the patch `FOR UPDATE`), the withdraw `UPDATE ... FROM tasks`, `get_decision` (joins its task), the list query (every page, so a cursor from a `*` list cannot cross projects) and `load_tasks`. Out of scope raises the same `NotFound` as an absent row, so the 404 body equals the unknown-key body. `todos._touch` and `decisions._touch` were merged into `tasks.touch_task`.
- Create/move target check (`api/tasks.require_target`): 403 `project_forbidden` outside the scope first (decided on the request alone, leaks nothing), then `ProjectLookup.require_active` (orchestrator `GET /api/v1/projects/{slug}` with the notifier's url and token; only active projects cached, 60 s). `default` is never looked up: it always exists and cannot be archived. The lookup runs only when the project changes, so a PATCH naming the task's current (even archived) project is 200 (4d7b5aa).

## 2026-11-02 - label notice skips the actor; malformed orchestrator answers

The label-match notifier skips the session that made the change (`events.actor_session`), not the task creator: a user adding a label to an agent-created task must notify the creator if it matches, and an agent must not be told about its own edit. `SessionLookup.live_session_ids` wraps a malformed 200 (bad JSON, wrong shapes, missing `id`) as `OrchestratorUnavailable`, so the notifier loop logs and backs off instead of crash-looping, which would also have stopped decision notices.

## 2026-11-02 packaging

- The CLI had no http commands until packaging: `serve-web` (`TASKBOARD_HTTP_PORT`, 8000) and `serve-service` (`TASKBOARD_SERVICE_PORT`, chart sets 8002) in `runtime.py`, one uvicorn per process, pattern of agents-schedule `runtime.py` (no uvicorn signal handling, `proxy_headers=False` so the allowlist sees the socket peer). One process per listener keeps the web allowlist and the relay bearer gate on separate Deployments and Services.
- Chart: three Deployments (`web`, `service`, `notifier`), each with a `migrate` init container (migrations are idempotent under the migrator lock). The notifier is one replica with `Recreate`. Only the web Service is routed by the ingress (`/taskboard` on `lab.szymonrichert.pl`); the relay Service stays in the cluster. oauth2-proxy admission is `components.web.podAnnotations`; the web listener allows `127.0.0.1/32` only (`allowedCidrs`). The three secret values (database URL, orchestrator token, relay token) are env `secretKeyRef`s either to the chart's own Secret (when given as values) or to a named existing Secret and key; setting both for one value, or neither, fails the render.
- `publicUrl` (-> `TASKBOARD_PUBLIC_BASE_URL`) is the agents-web base, not the API: notices link to `<publicUrl>/tasks/<key>`. The ingress `/taskboard` is the API only.
- `openapi.json` is the web app (it has the answer route and the stream); the service app is a subset checked by a test. Error responses are rewritten to the service's `{"error": {...}}` shape with the statuses per route in `api/openapi.py` (hand-kept table, like agents-schedule).
- CI mirrors agents-usage: lint, test (testcontainers postgres:18), chart, images (build, smoke, uid and no-dev-deps checks, push of `:<sha7>` on main), chart-publish, then `release` last (image retag, then the git tag as the final step). Repo needs the secrets `HARBOR_USERNAME` and `HARBOR_PASSWORD` (also needed by agents-usage) before the first main run.

## 2026-11-04 - delegate listener

- Why: the agents-app-connector (Boox Tasks tab) must act on the taskboard as a user, which the agent relay cannot give (service app = agent actor, no answer route, no stream) and the web listener refuses (source allowlist, oauth2). A third, cluster-only listener serves the web app's exact routes (`_user_app` shared by `create_web_app` and `create_delegate_app`, actor `user`) behind `ServiceTokenGate` on `TASKBOARD_DELEGATE_TOKENS` (comma separated list, so a token can be rotated without downtime). Its gate answers 401 where the web gate answers 403; `openapi.json` is unchanged (a test pins the route sets equal).
- The relay token must not open it and a delegate token must not equal the relay token (settings error naming `TASKBOARD_DELEGATE_TOKENS`, never the value); `delegate_port` must differ from the http and service ports. `taskboard serve-delegate` exits 78 with an empty list: a listener without tokens must not start.
- Chart: the `delegate` Deployment and Service render only when a token value or an existing Secret is set (neither = absent, so existing releases render unchanged; both fails). Every component then also gets `TASKBOARD_DELEGATE_TOKENS` from the Secret so settings validate identically in every process; `TASKBOARD_DELEGATE_PORT` goes to the ConfigMap only when enabled. Secret key `delegate-tokens`.
- Ask and withdraw still refuse a user actor on this listener (`agent_required`), as on web. Because of the shared factory the delegate also owns a `TaskChangeHub` (own LISTEN connection) for its stream.

## 2026-11-02 label-match notifier

- The scanner runs inside the notifier loop (same advisory singleton), before each send: `scan_label_events(conn, lookup)` locks one unscanned `label_added` event (`FOR UPDATE OF e SKIP LOCKED`), asks the orchestrator once with all added labels (`GET /api/v1/sessions?label=..` repeated, any match), inserts `reason='label'` notices `ON CONFLICT DO NOTHING` (unique `(task_id, session_id)` makes it once per task and session, ever), skips `tasks.created_by_session`, then a guarded `UPDATE events SET scanned_at`. One transaction, so a failed lookup rolls back and the event stays unscanned.
- agents-common v0.7.0 `OrchestratorClient` has no list-sessions method, so `notifier.SessionLookup` is a small local httpx client (agents-common untouched). The sessions list is a plain JSON array (no paging). It also filters client-side on `desired == 'active'` and label intersection, because the orchestrator already does but `FakeOrchestrator` ignores `label`.
- Lookup failure backoff is in memory in the loop (poll * 2^n, capped at the notifier backoff max), not a column: it pauses only scanning, never sending, and a restart simply retries at once. `run_notifier(..., lookup=None)` builds its own `SessionLookup` from the orchestrator url/token when none is passed.

## 2026-11-02 - sse stream

- `GET /stream` (`api/stream.py`) is mounted on the web app only; the service app has no route and no hub. Events are `task_changed` with data `{"key": "T-5"}` and nothing else. `app.state.hub` is a `TaskChangeHub`: one LISTEN connection (agents-common `listen`, which reconnects) fans task ids out to subscribers; each subscriber holds a set of ids, so a slow client coalesces instead of growing a queue or being dropped.
- Polling fallback (every 5 s, `create_web_app(stream_poll_seconds=...)`) uses an id cursor over `events` (every mutation appends an event), not `updated_at`: it starts at `max(events.id)` and reports tasks with newer events that no NOTIFY announced since the last poll. A NOTIFY races the poll, so a duplicate `task_changed` for one task is possible and harmless (clients refetch).
- The hub starts lazily with the first subscriber, stops (cancels listener and poller) when the last one leaves, and `stop()` runs in the web app lifespan shutdown. The 15 s keepalive comment is FastAPI's own SSE ping (`: ping`), not ours.
- Tests serve the app through real uvicorn: `httpx.ASGITransport` buffers the whole response and cannot read an SSE stream.

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

## 2026-11-02 stream listener restarts on any error
agents-common `listen` reconnects only on OperationalError/OSError; any other exception would end the hub's listen task silently while `running` stayed true. `_listen_loop` now logs and restarts after a capped exponential backoff (0.5 s to 30 s, reset on a delivered notification); CancelledError still propagates. The poll fallback covers the gap.

## 2026-11-07 - task read windows (T-61)

- Why: the MCP output budget; a task with a long history does not fit one tool answer. `GET /tasks/{key}` takes `events_limit`/`comments_limit` (0..200, newest N, oldest first) and `events_before`/`comments_before` (id cursors for paging back). Absent params return everything, so agents-web is unchanged.
- The limit is a `row_number() OVER (PARTITION BY task_id ...)` window, so `load_tasks` stays correct for any id list (list answers, write answers), not just one task. `events_total` and `comments_total` are on every task answer and ignore windows and cursors.

- 2026-11-09: `GET /projects/summary` (all three listeners, scope applied in SQL) and `task_changed {key, project}` with `?project=` on the stream. The hub carries (task id, project) pairs: the poll reads `events.project`, a LISTEN hint looks the project up from tasks. Index `events (project, id)` is in 0002 (unreleased, so no new migration). agents-common pinned at v0.12.0 (v0.11.0 was the labels release); deploy after the orchestrator sends `X-Orch-Projects`.
