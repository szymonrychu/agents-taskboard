# agents-taskboard memory

Lessons that outlive a session. Newest first.

## 2026-11-02 - scaffold

Created as a separate Python microservice (not a part of the orchestrator) because the taskboard owns its own Postgres and the hard rules allow cross-service access only through HTTP or the queue, one writer per table. The scaffold has no runtime dependencies yet; FastAPI, psycopg and agents-common are added with the first feature task that needs them.
