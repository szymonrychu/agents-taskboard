"""Approval routes: an agent asks a Go/No-go approval on a task. An approval is a decision with
fixed options and a mandatory expiry. Asking is mounted on every listener (agents only ask);
the batch answer exists only on the user listeners (web and delegate)."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.errors import ApiError
from agents_taskboard.api.schemas import ApprovalAnswerBatch, ApprovalCreate
from agents_taskboard.api.tasks import pool_of, public_url, task_id_of
from agents_taskboard.db import decisions as repo
from agents_taskboard.db import tasks as tasks_repo
from agents_taskboard.db.tasks import task_key


def build_approval_router(
    actor_dep: Callable[..., Actor], *, answerable: bool = False
) -> APIRouter:
    """`answerable` marks the user listeners, which carry the batch answer route."""
    router = APIRouter()
    ActorDep = Annotated[Actor, Depends(actor_dep)]

    @router.post("/tasks/{key}/approvals", status_code=201)
    async def ask_approval(
        key: str, body: ApprovalCreate, request: Request, response: Response, actor: ActorDep
    ) -> dict[str, Any]:
        task_id = task_id_of(key)
        if actor.kind != "agent":
            raise ApiError(403, "agent_required", "only an agent session can ask an approval")
        max_ttl = request.app.state.settings.approval_max_ttl_seconds
        if body.expires_in_seconds is not None and body.expires_in_seconds > max_ttl:
            raise ApiError(422, "invalid_request", f"expires_in_seconds: at most {max_ttl}")
        ttl = body.expires_in_seconds or min(repo.DEFAULT_APPROVAL_TTL_SECONDS, max_ttl)
        try:
            async with pool_of(request).connection() as conn:
                decision, created = await repo.ask_decision(
                    conn,
                    actor,
                    task_id,
                    body.title,
                    body.context,
                    repo.APPROVAL_OPTIONS,
                    body.client_key,
                    kind="approval",
                    subject=body.subject,
                    ttl_seconds=ttl,
                )
        except tasks_repo.NotFound:
            raise ApiError(404, "not_found", f"no task {key}") from None
        if not created:
            if decision.kind != "approval":
                raise ApiError(
                    409, "client_key_conflict", "client_key: already used by a question decision"
                )
            response.status_code = 200
        return decision.as_json(public_url(request))

    if answerable:

        @router.post("/approvals/answer")
        async def answer_approvals(
            body: ApprovalAnswerBatch, request: Request, actor: ActorDep
        ) -> dict[str, Any]:
            if actor.via == "delegate" and body.source is not None:
                via = f"delegate:{body.source}"
            else:
                via = actor.via or "web"
            items = []
            async with pool_of(request).connection() as conn:
                for item in body.items:
                    result, decision = await repo.answer_approval(
                        conn, item.id, item.verdict, item.note, via=via, answer_ref=item.answer_ref
                    )
                    key = None if decision is None else task_key(decision.task_id)
                    items.append({"id": item.id, "result": result, "task_key": key})
            return {"items": items}

    return router
