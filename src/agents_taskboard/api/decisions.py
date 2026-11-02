"""Decision routes. Asking and withdrawing are mounted on both apps (agents only); answering
exists only on the web app, so the service app has no such route."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.errors import ApiError
from agents_taskboard.api.schemas import DecisionAnswer, DecisionCreate
from agents_taskboard.api.tasks import pool_of, public_url, task_id_of
from agents_taskboard.db import decisions as repo
from agents_taskboard.db import tasks as tasks_repo


def build_router(actor_dep: Callable[..., Actor], *, answerable: bool = False) -> APIRouter:
    router = APIRouter()
    ActorDep = Annotated[Actor, Depends(actor_dep)]

    @router.post("/tasks/{key}/decisions", status_code=201)
    async def ask(
        key: str, body: DecisionCreate, request: Request, response: Response, actor: ActorDep
    ) -> dict[str, Any]:
        task_id = task_id_of(key)
        if actor.kind != "agent":
            raise ApiError(403, "agent_required", "only an agent session can ask a decision")
        options = [o.model_dump() for o in body.options]
        try:
            async with pool_of(request).connection() as conn:
                decision, created = await repo.ask_decision(
                    conn, actor, task_id, body.question, body.context, options, body.client_key
                )
        except tasks_repo.NotFound:
            raise ApiError(404, "not_found", f"no task {key}") from None
        if not created:
            response.status_code = 200
        return decision.as_json(public_url(request))

    @router.post("/tasks/{key}/decisions/{decision_id}/withdraw")
    async def withdraw(
        key: str, decision_id: int, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        task_id = task_id_of(key)
        async with pool_of(request).connection() as conn:
            decision = await repo.withdraw_decision(conn, actor, decision_id, task_id)
            if decision is not None:
                return decision.as_json(public_url(request))
            current = await repo.get_decision(conn, decision_id)
        if current is None or current.task_id != task_id:
            raise ApiError(404, "not_found", f"no decision {decision_id} on {key}")
        if current.asking_session != actor.session_id:
            raise ApiError(403, "forbidden", "only the asking session can withdraw")
        raise ApiError(409, "not_open", f"decision is already {current.status}")

    if answerable:

        @router.post("/tasks/{key}/decisions/{decision_id}/answer")
        async def answer(
            key: str, decision_id: int, body: DecisionAnswer, request: Request, actor: ActorDep
        ) -> dict[str, Any]:
            task_id = task_id_of(key)
            async with pool_of(request).connection() as conn:
                current = await repo.get_decision(conn, decision_id)
                if current is None or current.task_id != task_id:
                    raise ApiError(404, "not_found", f"no decision {decision_id} on {key}")
                if body.choice is not None:
                    if body.choice >= len(current.options):
                        raise ApiError(422, "invalid_request", "choice: no such option")
                    given: dict[str, Any] = {
                        "choice": body.choice,
                        "label": current.options[body.choice]["label"],
                    }
                else:
                    given = {"text": body.text}
                decision = await repo.answer_decision(conn, decision_id, given, body.note)
            if decision is None:
                raise ApiError(409, "already_answered", f"decision is already {current.status}")
            return decision.as_json(public_url(request))

    return router
