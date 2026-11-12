# agents-taskboard

Part of the workdir project (python).

## What this is

A Python microservice with its own Postgres database that keeps tasks and ideas for users and agents: description, labels, assignee session, comments, todo items, decisions (a question with options that the user answers), a history of every change, and notices that wake the sessions concerned. The user works in agents-web (`/tasks`); agents reach it through the orchestrator relay.

## Development

Run from this directory with mise:

- `mise run test` - tests (the database tests start Postgres 18 through testcontainers and need Docker)
- `mise run lint` - ruff check and format check
- `mise run fmt` - ruff format
- `mise run test:chart` - chart render tests
- `mise run lint:chart` - `helm lint` of the chart

## Commands

The `taskboard` entry point (`uv run taskboard <command>`):

- `taskboard migrate` - apply the schema migrations to the service's own database and exit.
- `taskboard serve-web` - serve the user-facing api on `TASKBOARD_HTTP_PORT` (source allowlist, answer route, SSE stream).
- `taskboard serve-service` - serve the relay api on `TASKBOARD_SERVICE_PORT` (bearer token, agent identity from forwarded headers, no answer route, no stream).
- `taskboard serve-delegate` - serve the delegate listener on `TASKBOARD_DELEGATE_PORT` (cluster-only, bearer token list, acts as a user; exits 78 naming `TASKBOARD_DELEGATE_TOKENS` when the list is empty).
- `taskboard notifier` - send pending notices to their sessions; one instance sends at a time (advisory lock).

## Configuration

Every process reads one validated `TASKBOARD_*` environment. A bad or missing variable makes the process exit with code 78 and one line per variable. Secrets are never logged.

| Variable | Default | Meaning |
|---|---|---|
| `TASKBOARD_DATABASE_URL` | required | Postgres DSN of the service's own database |
| `TASKBOARD_ORCHESTRATOR_URL` | required | orchestrator service port the notifier messages sessions through and the listeners validate projects against (`GET /api/v1/projects/{slug}`) |
| `TASKBOARD_ORCHESTRATOR_TOKEN` | - | orchestrator service token; exactly one of this and the `_FILE` variable is required |
| `TASKBOARD_ORCHESTRATOR_TOKEN_FILE` | - | path of a file holding the token |
| `TASKBOARD_PUBLIC_BASE_URL` | required | absolute http(s) base of the task links in notices |
| `TASKBOARD_RELAY_TOKEN` | required | bearer token of the service listener (the orchestrator relay sends it) |
| `TASKBOARD_DELEGATE_TOKENS` | empty | comma-separated bearer tokens of the delegate listener; each must differ from `TASKBOARD_RELAY_TOKEN`; `serve-delegate` refuses to start with none |
| `TASKBOARD_MINDMAP_TOKENS` | empty | comma-separated bearer tokens of the mindmap caller (agents-mindmap); open only `POST /tasks/{key}/external-changes` on the delegate listener; each must differ from the relay and every delegate token |
| `TASKBOARD_DELEGATE_PORT` | `8003` | delegate listener port (1..65535, different from the web and service ports) |
| `TASKBOARD_ALLOWED_CIDRS` | `127.0.0.1/32` | comma-separated CIDRs allowed on the web listener (the oauth2-proxy sidecar or a port-forward) |
| `TASKBOARD_HTTP_HOST` | `0.0.0.0` | bind address of both listeners |
| `TASKBOARD_HTTP_PORT` | `8000` | web listener port |
| `TASKBOARD_SERVICE_PORT` | `8001` | service listener port (the chart sets 8002) |
| `TASKBOARD_NOTIFIER_POLL_SECONDS` | `5` | notifier poll interval and base retry delay |
| `TASKBOARD_NOTIFIER_BACKOFF_MAX_SECONDS` | `60` | cap of the per-notice retry delay |
| `TASKBOARD_LOG_LEVEL` | `INFO` | log level |
| `TASKBOARD_SHUTDOWN_GRACE_SECONDS` | `20` | graceful shutdown wait |

## API

Base path `/taskboard/api/v1`. JSON in and out. The web listener refuses any socket peer outside `TASKBOARD_ALLOWED_CIDRS` with `403 forbidden` (forwarding headers are never read); the service listener answers `401 unauthorized` unless `Authorization: Bearer <relay token>` matches and takes the acting agent from the `X-Orch-Session` (required, `400 actor_required`), `X-Orch-Slug` and `X-Orch-Projects` (required, `*` or a comma list of 1..10 distinct project slugs whose first is the home project, `400 actor_required` when missing or malformed) headers. `GET /healthz` and `GET /readyz` (`503 not_ready` when the database is unreachable) are outside the base path and exempt from both gates. The service listener serves every route below except the answer route and the stream. The delegate listener serves exactly the routes of the web listener (including the answer route and the stream) as the user actor, behind `Authorization: Bearer <delegate token>` (`401 unauthorized` otherwise; the relay token does not open it); it has no ingress path and no oauth2 admission, and ask and withdraw still refuse a user (`403 agent_required`). `agents-app-connector` is its client. Its error statuses follow `openapi.json` except `401` replaces `403 forbidden`. The machine-readable contract of the web listener is `openapi.json`, regenerated with `uv run python -m agents_taskboard.api.openapi > openapi.json`; a test fails when it drifts from the app or when a route is missing from this section.

External changes (delegate listener only; not on the web or service listeners): `POST /taskboard/api/v1/tasks/{key}/external-changes` with `{"source": "mindmap", "ref": "M-<n>", "actor_session": "<uuid>" | null}` (strict: `ref` matches `^M-[1-9][0-9]*$`, no other field) tells the taskboard that something linked to the task changed in another service. `202 {"status": "accepted"}`; `404 not_found` for an unknown task; `422 invalid_request` for a bad body. It takes a mindmap token (`TASKBOARD_MINDMAP_TOKENS`) only: a delegate token gets `403 forbidden_token`, no or a wrong token `401 unauthorized`, and a mindmap token is `401` on every other delegate path. It logs an `external_change` event (detail `source` and `ref` only); the notifier then queues one `linked` notice (text unchanged: task key and link) for the task's assignee session and every live session of the task's label audience (its labels in its project, plus superleaders), except `actor_session`. A `linked` notice already pending for the same task and session absorbs the next one.

Projects: every task belongs to one project (a slug owned by the orchestrator; existing tasks are in `default`). The relay's `X-Orch-Projects` is the agent's scope: `*` is every project, a comma list of slugs (first = home project, where a create without `project` lands) only those projects. Web and delegate callers see every project. A task, todo item or decision outside the scope answers `404 not_found` with the same body as an unknown key; lists and cursors only ever return tasks of the scope; a create or move into a project outside the scope is `403 project_forbidden`. A create or move into a project other than `default` is validated against the orchestrator: `422 unknown_project`, `422 project_archived`, `503 upstream_unavailable` when it cannot answer (an active project is cached for 60 s).

Task keys look like `T-5`. Statuses: `idea`, `open`, `in_progress`, `done`, `dropped`; kinds: `idea`, `task`. A task carries a `version`; `PATCH` needs it and answers `412 stale_version` when the task changed. Labels are normalised (trimmed, lowercase, `[a-z0-9-_.:]`, at most 40 characters, at most 20).

### `GET /taskboard/api/v1/tasks`

List tasks. Query: `status` (`active` default, `done`, `dropped`, `all`), `project`, `spec` (tasks linked to that spec, `422` for a malformed key), `session`, `mine` (service listener, the acting session), `waiting` (only tasks with an open decision), `label` (repeatable, any match), `limit` (1..200, default 50), `cursor`. Returns `{"items": [...], "next_cursor": "..."|null}`.

### `POST /taskboard/api/v1/tasks`

Create an idea or task: `title`, `body`, `kind`, `status`, `labels`, `assignee_session` or `assignee_self` (agents), `client_key`, `project` (default: the agent's one project; `default` for a `*` agent and users), `spec` (a key `S-n`: the task takes the spec's project; an explicit other `project` is `422 project_mismatch`; a spec outside the scope or deleted is `404 not_found`). `201` with the task; a repeated `client_key` of the same creator answers `200` with the existing task.

### `GET /taskboard/api/v1/tasks/{key}`

One task with its labels, linked spec key (`spec`, or null), plan fields (`step` number or null, `refs`, `model`, `depends_on` task keys), comments, todo, decisions and history. `404 not_found` for an unknown or malformed key. Optional `events_limit` and `comments_limit` (0..200) return only the newest N events or comments (oldest first); `events_before` and `comments_before` (ids) page back to items with a smaller id. Every task carries `events_total` and `comments_total`. `422 invalid_request` for out-of-range values.

### `PATCH /taskboard/api/v1/tasks/{key}`

Change `title`, `body`, `status`, `labels`, `project` (a move, logged as `project_changed`; it detaches the task from its spec), `spec` (`S-n` links the task and moves it into the spec's project, `null` unlinks; logged as `spec_set`; `422 project_mismatch` with a different `project`), `assignee_session` or `assignee_self`, `refs` (what implements a plan step: `[{kind: pr | commit | branch | tag | url, value}]`, at most 20) and `model` (`haiku`, `sonnet`, `opus`, `fable`, or null); `version` is required. The body of a plan step is capped at 32 KiB (`413 payload_too_large`); a step leaving its spec (`spec` or `project` change) loses its number and the spec's other steps are renumbered densely, bumping `steps_version`. `412 stale_version` when the task changed, `422 invalid_request` when nothing changes.

### `PUT /taskboard/api/v1/tasks/{key}/deps`

Set one plan step's dependencies: `depends_on` (task keys of steps of the same spec); a change bumps the spec's `steps_version`. `200` with the task. `422 dependency_cycle` when the dependencies would form a cycle (also a step depending on itself), `422 invalid_request` when the task or a dependency is not a step of that spec.

### `POST /taskboard/api/v1/tasks/{key}/comments`

Add a comment (`body`). `201`.

### `PUT /taskboard/api/v1/tasks/{key}/todo`

Replace the todo list (`items`: `text` and `status` `todo`, `inprogress` or `done`; at most 100).

### `PATCH /taskboard/api/v1/tasks/{key}/todo/{item_id}`

Change one todo item (`text`, `status`). `404 not_found` for an unknown item.

### `POST /taskboard/api/v1/tasks/{key}/decisions`

Ask a decision (agents only, `403 agent_required` for a user): `question`, `context`, `options` (2..8, each with `label`, `description`, `recommended`), `client_key`. `201`.

### `POST /taskboard/api/v1/tasks/{key}/decisions/{decision_id}/withdraw`

Withdraw an open decision (the asking session, or any superleader, i.e. an agent whose `X-Orch-Projects` is `*`; `403 forbidden` otherwise). Body required: `reason` (1..2000 characters, not blank) and exactly one of `superseded_by` (id of another existing decision in the caller's project scope, not the decision itself) or `answered_elsewhere` (free text, 1..4000, not blank); anything else is `422`. The decision JSON then carries `withdraw_reason`, `superseded_by`, `superseded_by_task_key` and `answered_elsewhere` (all null while open, answered, or withdrawn before migration 0003). `409 not_open` when it is no longer open.

### `POST /taskboard/api/v1/tasks/{key}/decisions/{decision_id}/answer`

Answer an open decision as the user: `choice` (option index) or `text`, plus an optional `note`. Web listener only. `409 already_answered` when answered or withdrawn; of two simultaneous answers exactly one wins.

### `GET /taskboard/api/v1/projects/summary`

Per project in the caller's scope: `[{"project", "open_tasks", "waiting_decisions", "specs": {"draft": 0, "review": 0, "approved": 0}}]`, one grouped query ordered by project. `open_tasks` counts tasks that are not `done` or `dropped`; `waiting_decisions` counts open decisions; `specs` counts live specs by status (deleted and archived ones are not counted), and a project with only specs is listed too. Optional `project` narrows it to one project. A relay caller sees only its own project (all with `X-Orch-Projects: *`); web and delegate callers see every project. Served on all three listeners.

Specs (base design 3.5): a spec `S-<n>` is a named bundle of markdown documents in one project, with status `draft`, `review`, `approved` (set by the user, a superleader, or an agent when no decision on the spec's open or in_progress linked tasks is open) or `archived`. Each document (`path` like `plan.md` or `research/topic.md`: `^[a-z0-9][a-z0-9._/-]{0,119}\.md$`, no `..`) has a linear, immutable version history. A write names the version it edited (`base_version`, `0` for a new document); when another write came first it answers `412 stale_version` with `current_version` in the error object, and nothing is written. Caps: document body 192 KiB of UTF-8, 50 documents per spec (`413 payload_too_large`); version `note` up to 500 characters, at most 20 `sources` (opaque refs, `422`). A version written on an `approved` spec returns it to `review`. Spec names are unique per project among live specs; a deleted spec frees its name, answers `404` on every route, and its tasks are detached. Every write appends one event and is announced on the stream.

### `GET /taskboard/api/v1/specs`

List live specs of the caller's scope, newest first, without documents. Query: `project`, `status`, `label` (repeatable, any match), `name`, `limit` (1..200, default 50), `cursor`. Returns `{"items": [...], "next_cursor": "..."|null}`.

### `POST /taskboard/api/v1/specs`

Create a spec: `name` (`^[a-z0-9][a-z0-9-]{0,79}$`), `title` (1..200), `labels`, `project` (default as for tasks), `client_key`, `docs` (`[{path, body, note?, sources?}]`, each at version 1). `201` with the spec; a repeated `client_key` of the same creator answers `200` with the stored spec. `409 name_taken`, `403 project_forbidden`, `422 unknown_project` / `project_archived`.

### `GET /taskboard/api/v1/specs/{key}`

One spec: its fields, `documents` (current `path`, `version`, `size`, `sha256`, `updated_at`, `created_by`), linked task keys (`tasks`) and the open comment count (`open_comments`).

### `PATCH /taskboard/api/v1/specs/{key}`

Change `title`, `status`, `labels`, `project` (a move takes the spec's tasks along), `assignee_session` or `assignee_self`; `version` is required (`412 stale_version`). An agent that is not a superleader setting `approved` gets `409 decisions_open` with `decisions` (`[{task, id, question}]`, the open decisions on the spec's open or in_progress linked tasks, plan steps included) in the error object; the user and superleaders approve always, and re-sending `approved` on an approved spec changes nothing. Agents get `403 user_only` for assigning another session or clearing another session's assignment. An approval records the approved document versions in its event.

### `DELETE /taskboard/api/v1/specs/{key}`

Soft-delete a spec (the user, or on the service listener a superleader, i.e. an agent whose `X-Orch-Projects` is `*`; any other agent gets `403 superleader_only`). `204`; the spec's tasks are detached.

### `GET /taskboard/api/v1/specs/{key}/docs`

The current version of every live document, without bodies: `{"items": [...]}`.

### `GET /taskboard/api/v1/specs/{key}/docs/{path:path}`

One document version with its body (`version` query: an older one): `{path, version, body, sha256, size, note, sources, created_by, created_at}`. With `Accept: text/markdown` the raw body with `ETag: "<sha256>"` and `X-Spec-Version`; a matching `If-None-Match` answers `304`. `404` for a missing document or a removal.

### `PUT /taskboard/api/v1/specs/{key}/docs/{path:path}`

Write the next version: `body` (string, or `null` to remove the document), `base_version`, `note`, `sources`, `client_key`. `201` with the version (no body); a repeated `client_key` answers `200` with the stored version.

### `GET /taskboard/api/v1/specs/{key}/docs/{path:path}/section`

One section of a document, under the agents' tool output budget: `heading` (required) is the anchor slug the web gives the heading (lowercase, runs of characters outside `[a-z0-9]` become `-`, trimmed; the second heading with the same slug is `<slug>-2`), optional `version`. The section is the heading line and the text up to the next heading of the same or a higher level (headings inside fenced code blocks do not count). Returns `{path, version, heading, body, truncated}`; a body of 40000 characters or more is cut at a line boundary below that and `truncated` is true. `404` for a missing document or heading.

### `GET /taskboard/api/v1/specs/{key}/docs/{path:path}/versions`

One document's history, newest first, without bodies (`removed` marks a removal). Query: `limit`, `cursor`. Returns `{"items": [...], "next_cursor": <version>|null}`.

### `GET /taskboard/api/v1/specs/{key}/comments`

Comments of a spec, oldest first: `{"items": [...]}`. Query: `path` (one document), `open` (`true`: unresolved only, `false`: resolved only).

### `POST /taskboard/api/v1/specs/{key}/comments`

Comment on a spec: `body` (up to 16 KiB), optional `path`, `version` and `anchor` (a heading slug or `L<a>-L<b>`), `suggestion` (a proposed replacement of the whole document, up to 192 KiB; needs `path`; sets `suggestion_status` to `open`), `attachments` (opaque refs, at most 10), `client_key`. `201`; a repeated `client_key` of the same creator answers `200` with the stored comment. `413 payload_too_large` over a cap.

### `POST /taskboard/api/v1/specs/{key}/comments/{comment_id}/resolve`

Resolve a comment. `409 already_resolved` when it already is.

### `POST /taskboard/api/v1/specs/{key}/comments/{comment_id}/accept`

Accept a suggestion as the user (web and delegate listeners; the relay has no such route): `version` is the document's current version as the client last saw it. Writes the whole `suggestion` as the next version of the comment's document (note `Accepted proposed edit from comment <id>`, `sources` copied from the attachments), marks the suggestion `accepted` with `accepted_version` and resolves the comment, in one transaction and one event. `201` with `{"version", "comment"}`. `412 stale_version` with `current_version` when the document moved on (nothing changes), `409 already_decided`, `422 no_suggestion`.

### `POST /taskboard/api/v1/specs/{key}/comments/{comment_id}/dismiss`

Dismiss a suggestion (the user, or a superleader on the service listener; any other agent gets `403 superleader_only`; accept stays user-only): marks it `dismissed` and resolves the comment. `409 already_decided`, `422 no_suggestion`.

Plans (base design 3.17): a spec has one plan header and an ordered set of steps. A step is a task of the spec with a number (`step`, dense from 1); its body (markdown, up to 32 KiB) and todo items (up to 100) are the task ones, and title, body, status, model and refs change through `PATCH /tasks/{key}`. A step is ready when it is open and every dependency is done or dropped. Every change of the step set or order bumps the spec's `steps_version`; a write naming an older one answers `412 stale_version` with `current_steps_version`.

### `GET /taskboard/api/v1/specs/{key}/plan`

The plan header: `{goal, architecture, tech_stack, constraints, review_focus, version, updated_at, updated_by}`; `version` 0 and empty fields before the first write.

### `PUT /taskboard/api/v1/specs/{key}/plan`

Replace the plan header: `version` (the one read, 0 for the first write), `goal` (up to 500), `architecture` (up to 4000), `tech_stack` (up to 1000), `constraints` and `review_focus` (at most 50 strings of up to 1000). `200` with the header. `412 stale_version` with `current_version`, `422 invalid_request` over a cap.

### `GET /taskboard/api/v1/specs/{key}/steps`

The steps in order, without bodies: `{"items": [{key, step, title, status, assignee_session, model, refs, version, ready, todo_total, todo_done, deps}], "steps_version": n}` (`deps` are task keys).

### `PUT /taskboard/api/v1/specs/{key}/steps`

Replace the step set (the bulk path for agents): `steps_version` and `steps` (`[{n, title, body, depends_on: [n], model?, labels?, todo?: [text]}]`, `n` dense from 1; an absent optional field keeps the current value, a null `model` clears it). Keyed by `n`: new numbers are created, existing ones updated, removed steps that never started are closed `dropped` and lose their number; a set that changes nothing changes nothing. `200` with `{"keys": [...]}` in order. `409 step_in_progress` when the set removes a started or done step, `413 payload_too_large` for a body over 32 KiB, `422 dependency_cycle` / `invalid_request`.

### `POST /taskboard/api/v1/specs/{key}/steps`

Insert one step: `steps_version`, `after` (a step number, `0` for first, absent for last), `title`, `body`, `depends_on` (step numbers), `model`. Later steps are renumbered. `201` with the step as listed.

### `PUT /taskboard/api/v1/specs/{key}/steps/order`

Reorder: `steps_version` and `order` (task keys, every live step exactly once, else `422 invalid_request`). `200` with `{"steps_version": n}`.

### `POST /taskboard/api/v1/specs/{key}/steps/next`

The first ready step in order: `claim` (default false) also makes the caller its assignee and sets it `in_progress` in one guarded update, so two implementers never get the same step. `200` with the step as listed, `204` when none is ready.

### `GET /taskboard/api/v1/specs/{key}/gate`

The spec's human gate: `{key, status, approved_versions, gate_task, decisions, gate, approved, approvable, open_decisions}`. The gate task is the improvement's own task linked to the spec: its lowest-id task that is not a step and not dropped (`null` when there is none). `decisions` are that task's decisions other than withdrawn ones (`[{id, question, status, answer}]`); `gate` is `none` with no such decision, `waiting` while one is open and `open` when all are answered. `approved` is `status == "approved"` and does not gate execution; `approved_versions` maps each document to the version the user approved (null unless approved). `approvable` is true when no decision on the spec's open or in_progress linked tasks is open; `open_decisions` lists those (`[{task, id, question}]`, task id then decision id), so an agent can approve exactly when `approvable` is true. The workbench rule gates implementation on an approved spec.

### `GET /taskboard/api/v1/stream`

Server-sent events (web and delegate listeners): `task_changed` with data `{"key": "T-5", "project": "default"}` whenever a task changes, and `spec_changed` with data `{"key": "S-2", "project": "default"}` plus, when the change has them, `path` and `version` (a document version), `comment_id` (a comment or a decided suggestion) and `deleted: true` (the spec was deleted). Every event carries `id: <n>`, the id of the event row that caused it. The optional `project` query parameter delivers only that project's changes. Catch-up: reconnect with the `Last-Event-ID` header or the `after_id` query parameter (the larger wins when both are given; a header that is not a number is ignored) and the stream first sends, in id order, one event per task or spec changed after that id, then the live feed. When the id is below the oldest kept event row it sends one `reset` event (data `{}`, `id` the newest row) instead: refetch the lists. A NOTIFY only wakes the stream; it also polls every 5 seconds, and a client refetches what changed.

### Errors

Every error has the shape `{"error": {"code": "<code>", "message": "<text>"}}`. Codes: `actor_required` (400), `unauthorized` (401), `forbidden` (403), `agent_required` (403), `project_forbidden` (403), `user_only` (403), `not_found` (404), `method_not_allowed` (405), `already_answered`, `not_open`, `name_taken`, `already_resolved`, `already_decided`, `step_in_progress` and `decisions_open` (409), `stale_version` (412, on a document or the plan header with `current_version`, on the steps with `current_steps_version`), `payload_too_large` (413), `invalid_request`, `no_suggestion`, `dependency_cycle`, `project_mismatch`, `unknown_project` and `project_archived` (422), `not_ready` and `upstream_unavailable` (503).

## Notices

A notice wakes a session through the orchestrator messages api. It is created when a decision a session asked is answered, when a label added to a task matches a label of an active session (once per task and session, never for the creator), and when the user comments on a spec, approves it, writes a document version (also by accepting a suggestion) or dismisses a suggestion: that notice goes to the spec's assignee session, else its creating session (none for a spec the user created and nobody is assigned to), and an agent's own change notifies nobody. One pending notice per spec and session absorbs the next ones. The lookup asks the orchestrator for sessions of the task's project plus superleaders (`project=<task project>&superleaders=include`), so a label notice never reaches another project's non-superleader session. The text carries only the key and the link, `Task T-n was updated: TASKBOARD_PUBLIC_BASE_URL/tasks/T-n` or `Spec S-n was updated: TASKBOARD_PUBLIC_BASE_URL/specs/S-n`, never task or spec content. Delivery is at least once: a crash between the send and the commit re-sends. A `404` from the orchestrator (session gone) ends the notice; any other failure retries with a per-notice backoff.

## Deployment

Deploy order: the taskboard must deploy after the orchestrator release that sends `X-Orch-Projects` (agents-orchestrator PR #94); the relay listener answers `400 actor_required` without that header.

The image is `harbor.szymonrichert.pl/containers/agents-taskboard` and the chart `chart/agents-taskboard` (three Deployments: `web`, `service`, `notifier`, plus an optional fourth `delegate` that renders only when `delegateTokens` or `delegateExistingSecret` is set, with its own `<release>-delegate` Service on port 8003 and never an ingress path; every Deployment has a `migrate` init container). CI builds, tests and publishes both; the `release` job on main tags the image and the repository `vX.Y.Z` from the conventional commits since the last tag (`scripts/next_version.py`). The ingress routes only `/taskboard` to the web service; the relay api stays in the cluster.
