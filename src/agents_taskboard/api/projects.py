"""GET /projects/summary: per-project counts within the caller's scope (projects plan 3.6)."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.tasks import pool_of
from agents_taskboard.db.scope import scoped

SUMMARY_SQL = """
SELECT t.project,
       count(*) FILTER (WHERE t.status NOT IN ('done', 'dropped')),
       coalesce(sum(w.n), 0)
FROM tasks t
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM decisions d WHERE d.task_id = t.id AND d.status = 'open'
) w ON true
WHERE true{scope}{only}
GROUP BY t.project ORDER BY t.project
"""


def build_router(actor_dep: Callable[..., Actor]) -> APIRouter:
    router = APIRouter()
    ActorDep = Annotated[Actor, Depends(actor_dep)]

    @router.get("/projects/summary")
    async def summary(
        request: Request, actor: ActorDep, project: str | None = None
    ) -> list[dict[str, Any]]:
        clause, params = scoped(actor)
        only = ""
        if project is not None:
            only = " AND t.project = %s"
            params = [*params, project]
        async with pool_of(request).connection() as conn:
            cur = await conn.execute(SUMMARY_SQL.format(scope=clause, only=only), params)
            rows = await cur.fetchall()
        # spec counts stay zero until specs exist (plan D4)
        return [
            {
                "project": name,
                "open_tasks": open_tasks,
                "waiting_decisions": int(waiting),
                "specs": {"draft": 0, "review": 0, "approved": 0},
            }
            for name, open_tasks, waiting in rows
        ]

    return router
