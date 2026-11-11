"""GET /projects/summary: per-project counts within the caller's scope (projects plan 3.6)."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.tasks import pool_of
from agents_taskboard.db.scope import scoped

SUMMARY_SQL = """
WITH t AS (
    SELECT t.project,
           count(*) FILTER (WHERE t.status NOT IN ('done', 'dropped')) AS open_tasks,
           coalesce(sum(w.n), 0) AS waiting
    FROM tasks t
    LEFT JOIN LATERAL (
        SELECT count(*) AS n FROM decisions d WHERE d.task_id = t.id AND d.status = 'open'
    ) w ON true
    WHERE true{task_scope}{task_only}
    GROUP BY t.project
), s AS (
    SELECT s.project,
           count(*) FILTER (WHERE s.status = 'draft') AS draft,
           count(*) FILTER (WHERE s.status = 'review') AS review,
           count(*) FILTER (WHERE s.status = 'approved') AS approved
    FROM specs s
    WHERE s.deleted_at IS NULL AND s.status <> 'archived'{spec_scope}{spec_only}
    GROUP BY s.project
)
SELECT project, coalesce(t.open_tasks, 0), coalesce(t.waiting, 0),
       coalesce(s.draft, 0), coalesce(s.review, 0), coalesce(s.approved, 0)
FROM t FULL JOIN s USING (project)
ORDER BY project
"""


def build_router(actor_dep: Callable[..., Actor]) -> APIRouter:
    router = APIRouter()
    ActorDep = Annotated[Actor, Depends(actor_dep)]

    @router.get("/projects/summary")
    async def summary(
        request: Request, actor: ActorDep, project: str | None = None
    ) -> list[dict[str, Any]]:
        parts: dict[str, str] = {}
        params: list = []
        for name, column in (("task", "t.project"), ("spec", "s.project")):
            clause, scope_params = scoped(actor, column)
            parts[f"{name}_scope"] = clause
            parts[f"{name}_only"] = "" if project is None else f" AND {column} = %s"
            params += scope_params + ([] if project is None else [project])
        async with pool_of(request).connection() as conn:
            cur = await conn.execute(SUMMARY_SQL.format(**parts), params)
            rows = await cur.fetchall()
        return [
            {
                "project": name,
                "open_tasks": open_tasks,
                "waiting_decisions": int(waiting),
                "specs": {"draft": draft, "review": review, "approved": approved},
            }
            for name, open_tasks, waiting, draft, review, approved in rows
        ]

    return router
