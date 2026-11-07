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
| `TASKBOARD_ORCHESTRATOR_URL` | required | orchestrator service port the notifier messages sessions through |
| `TASKBOARD_ORCHESTRATOR_TOKEN` | - | orchestrator service token; exactly one of this and the `_FILE` variable is required |
| `TASKBOARD_ORCHESTRATOR_TOKEN_FILE` | - | path of a file holding the token |
| `TASKBOARD_PUBLIC_BASE_URL` | required | absolute http(s) base of the task links in notices |
| `TASKBOARD_RELAY_TOKEN` | required | bearer token of the service listener (the orchestrator relay sends it) |
| `TASKBOARD_DELEGATE_TOKENS` | empty | comma-separated bearer tokens of the delegate listener; each must differ from `TASKBOARD_RELAY_TOKEN`; `serve-delegate` refuses to start with none |
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

Base path `/taskboard/api/v1`. JSON in and out. The web listener refuses any socket peer outside `TASKBOARD_ALLOWED_CIDRS` with `403 forbidden` (forwarding headers are never read); the service listener answers `401 unauthorized` unless `Authorization: Bearer <relay token>` matches and takes the acting agent from the `X-Orch-Session` (required, `400 actor_required`) and `X-Orch-Slug` headers. `GET /healthz` and `GET /readyz` (`503 not_ready` when the database is unreachable) are outside the base path and exempt from both gates. The service listener serves every route below except the answer route and the stream. The delegate listener serves exactly the routes of the web listener (including the answer route and the stream) as the user actor, behind `Authorization: Bearer <delegate token>` (`401 unauthorized` otherwise; the relay token does not open it); it has no ingress path and no oauth2 admission, and ask and withdraw still refuse a user (`403 agent_required`). `agents-app-connector` is its client. Its error statuses follow `openapi.json` except `401` replaces `403 forbidden`. The machine-readable contract of the web listener is `openapi.json`, regenerated with `uv run python -m agents_taskboard.api.openapi > openapi.json`; a test fails when it drifts from the app or when a route is missing from this section.

Task keys look like `T-5`. Statuses: `idea`, `open`, `in_progress`, `done`, `dropped`; kinds: `idea`, `task`. A task carries a `version`; `PATCH` needs it and answers `412 stale_version` when the task changed. Labels are normalised (trimmed, lowercase, `[a-z0-9-_.:]`, at most 40 characters, at most 20).

### `GET /taskboard/api/v1/tasks`

List tasks. Query: `status` (`active` default, `done`, `dropped`, `all`), `session`, `mine` (service listener, the acting session), `waiting` (only tasks with an open decision), `label` (repeatable, any match), `limit` (1..200, default 50), `cursor`. Returns `{"items": [...], "next_cursor": "..."|null}`.

### `POST /taskboard/api/v1/tasks`

Create an idea or task: `title`, `body`, `kind`, `status`, `labels`, `assignee_session` or `assignee_self` (agents), `client_key`. `201` with the task; a repeated `client_key` of the same creator answers `200` with the existing task.

### `GET /taskboard/api/v1/tasks/{key}`

One task with its labels, comments, todo, decisions and history. `404 not_found` for an unknown or malformed key. Optional `events_limit` and `comments_limit` (0..200) return only the newest N events or comments (oldest first); `events_before` and `comments_before` (ids) page back to items with a smaller id. Every task carries `events_total` and `comments_total`. `422 invalid_request` for out-of-range values.

### `PATCH /taskboard/api/v1/tasks/{key}`

Change `title`, `body`, `status`, `labels`, `assignee_session` or `assignee_self`; `version` is required. `412 stale_version` when the task changed, `422 invalid_request` when nothing changes.

### `POST /taskboard/api/v1/tasks/{key}/comments`

Add a comment (`body`). `201`.

### `PUT /taskboard/api/v1/tasks/{key}/todo`

Replace the todo list (`items`: `text` and `status` `todo`, `inprogress` or `done`; at most 100).

### `PATCH /taskboard/api/v1/tasks/{key}/todo/{item_id}`

Change one todo item (`text`, `status`). `404 not_found` for an unknown item.

### `POST /taskboard/api/v1/tasks/{key}/decisions`

Ask a decision (agents only, `403 agent_required` for a user): `question`, `context`, `options` (2..8, each with `label`, `description`, `recommended`), `client_key`. `201`.

### `POST /taskboard/api/v1/tasks/{key}/decisions/{decision_id}/withdraw`

Withdraw an open decision (the asking session only, `403 forbidden` otherwise). `409 not_open` when it is no longer open.

### `POST /taskboard/api/v1/tasks/{key}/decisions/{decision_id}/answer`

Answer an open decision as the user: `choice` (option index) or `text`, plus an optional `note`. Web listener only. `409 already_answered` when answered or withdrawn; of two simultaneous answers exactly one wins.

### `GET /taskboard/api/v1/stream`

Server-sent events (web listener only): `task_changed` with data `{"key": "T-5"}` whenever a task changes. A notification is a hint; the stream also polls every 5 seconds, and a client refetches the task.

### Errors

Every error has the shape `{"error": {"code": "<code>", "message": "<text>"}}`. Codes: `actor_required` (400), `unauthorized` (401), `forbidden` (403), `agent_required` (403), `not_found` (404), `method_not_allowed` (405), `already_answered` and `not_open` (409), `stale_version` (412), `invalid_request` (422; body or query fails validation) and `not_ready` (503).

## Notices

A notice wakes a session through the orchestrator messages api. It is created when a decision a session asked is answered, and when a label added to a task matches a label of an active session (once per task and session, never for the creator). The text carries only the task key and the link `TASKBOARD_PUBLIC_BASE_URL/tasks/<key>`, never task content. Delivery is at least once: a crash between the send and the commit re-sends. A `404` from the orchestrator (session gone) ends the notice; any other failure retries with a per-notice backoff.

## Deployment

The image is `harbor.szymonrichert.pl/containers/agents-taskboard` and the chart `chart/agents-taskboard` (three Deployments: `web`, `service`, `notifier`, plus an optional fourth `delegate` that renders only when `delegateTokens` or `delegateExistingSecret` is set, with its own `<release>-delegate` Service on port 8003 and never an ingress path; every Deployment has a `migrate` init container). CI builds, tests and publishes both; the `release` job on main tags the image and the repository `vX.Y.Z` from the conventional commits since the last tag (`scripts/next_version.py`). The ingress routes only `/taskboard` to the web service; the relay api stays in the cluster.
