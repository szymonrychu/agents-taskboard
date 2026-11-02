# agents-taskboard memory

Lessons that outlive a session. Newest first.

## 2026-11-02 - skeleton, settings, schema, actors

- agents-common is pinned at v0.7.0 (agents-schedule still pins v0.3.0): v0.7.0 is the first tag with `ServiceTokenGate` (`agents_common.service_gate`). The local `code/agents-common` checkout is stale (v0.4.0 era), read the tags with `git show vX:path`. The migrator entry point is `agents_common.db.migrate.run_migrations(url, package)` (not `agents_common.migrate`); `db.migrate(settings)` wraps it and `taskboard migrate` is the chart init container.
- Two apps, one error shape and probes: `create_web_app` (SourceAllowlist on TASKBOARD_ALLOWED_CIDRS, never reads agent headers, the answer route goes here) and `create_service_app` (ServiceTokenGate with the relay token, `actor_from_service` reads `X-Orch-Session`/`X-Orch-Slug`). Identity headers are trusted only because the gate ran first; a missing `X-Orch-Session` is 400 `actor_required`, the slug is optional (None).
- The relay token is the raw secret in `TASKBOARD_RELAY_TOKEN` (a SecretStr), because `ServiceTokenGate` takes raw tokens and hashes them itself; the orchestrator relay (Task 9) sends the same value.
- Migration 0001 has `ON DELETE CASCADE` from every child table to `tasks`; the task key is `'T-'||id` in code only. `comments` and `events` carry the same actor columns (kind/session/slug).
- Tests need Docker (testcontainers postgres:18 via the agents-common pytest plugin). Without Docker, run against any Postgres (PG16 verified) by overriding the plugin's `pg_url` fixture in a throwaway plugin with `-p no:agents_common -p <plugin>`.

## 2026-11-02 - scaffold

Created as a separate Python microservice (not a part of the orchestrator) because the taskboard owns its own Postgres and the hard rules allow cross-service access only through HTTP or the queue, one writer per table. The scaffold has no runtime dependencies yet; FastAPI, psycopg and agents-common are added with the first feature task that needs them.
