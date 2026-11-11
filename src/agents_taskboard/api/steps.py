"""Plan header, steps, next step and gate routes of a spec (base design 3.17 and 4.2, gate per
T-116 Q2 A and Q3 A), mounted on every listener. Only the actor dependency differs. Writes are
scoped inside the repository; reads check the spec's scope first, so a spec outside the
caller's projects answers the unknown-key 404."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.errors import ApiError
from agents_taskboard.api.schemas import (
    Gate,
    NextStep,
    Plan,
    PlanPut,
    Step,
    StepInsert,
    StepKeys,
    StepList,
    StepsOrder,
    StepsPut,
    StepsVersion,
)
from agents_taskboard.api.specs import spec_id_of
from agents_taskboard.api.tasks import pool_of, resolve_spec
from agents_taskboard.db import steps as repo


@contextmanager
def step_errors(key: str, version_field: str = "current_steps_version") -> Iterator[None]:
    """Map repository errors to API errors; a stale write names the current version in
    `version_field`."""
    try:
        yield
    except repo.NotFound:
        raise ApiError(404, "not_found", f"no spec {key}") from None
    except repo.StaleVersion as exc:
        raise ApiError(
            412,
            "stale_version",
            "the plan changed since it was read",
            {version_field: exc.current_version},
        ) from None
    except repo.StepInProgress:
        raise ApiError(
            409, "step_in_progress", "the set removes a step that started or is done"
        ) from None
    except repo.DependencyCycle:
        raise ApiError(422, "dependency_cycle", "the dependencies would form a cycle") from None
    except repo.TooLarge as exc:
        raise ApiError(413, "payload_too_large", str(exc) or "over the cap") from None
    except repo.InvalidRequest as exc:
        raise ApiError(422, "invalid_request", str(exc)) from None


def build_steps_router(actor_dep: Callable[..., Actor]) -> APIRouter:
    router = APIRouter()
    ActorDep = Annotated[Actor, Depends(actor_dep)]

    @router.get("/specs/{key}/plan", response_model=Plan)
    async def get_plan(key: str, request: Request, actor: ActorDep) -> dict[str, Any]:
        spec_id, _ = await resolve_spec(request, actor, key)
        with step_errors(key):
            async with pool_of(request).connection() as conn:
                return await repo.get_plan(conn, spec_id)

    @router.put("/specs/{key}/plan", response_model=Plan)
    async def put_plan(
        key: str, body: PlanPut, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        spec_id = spec_id_of(key)
        with step_errors(key, "current_version"):
            async with pool_of(request).connection() as conn:
                return await repo.put_plan(
                    conn,
                    actor,
                    spec_id,
                    version=body.version,
                    goal=body.goal,
                    architecture=body.architecture,
                    tech_stack=body.tech_stack,
                    constraints=body.constraints,
                    review_focus=body.review_focus,
                )

    @router.get("/specs/{key}/steps", response_model=StepList)
    async def list_steps(key: str, request: Request, actor: ActorDep) -> dict[str, Any]:
        spec_id, _ = await resolve_spec(request, actor, key)
        with step_errors(key):
            async with pool_of(request).connection() as conn:
                items, steps_version = await repo.list_steps(conn, spec_id)
        return {"items": items, "steps_version": steps_version}

    @router.put("/specs/{key}/steps", response_model=StepKeys)
    async def set_steps(
        key: str, body: StepsPut, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        """Replace the step set, keyed by `n` (dense from 1)."""
        spec_id = spec_id_of(key)
        steps = []
        for item in body.steps:
            step = item.model_dump(include={"n", "title", "body", "depends_on"})
            if "model" in item.model_fields_set:  # absent keeps the model, null clears it
                step["model"] = item.model
            for optional in ("labels", "todo"):
                value = getattr(item, optional)
                if value is not None:
                    step[optional] = value
            steps.append(step)
        with step_errors(key):
            async with pool_of(request).connection() as conn:
                keys = await repo.set_steps(
                    conn, actor, spec_id, steps_version=body.steps_version, steps=steps
                )
        return {"keys": keys}

    @router.post("/specs/{key}/steps", status_code=201, response_model=Step)
    async def insert_step(
        key: str, body: StepInsert, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        """Insert one step after step `after` (0: first, absent: last)."""
        spec_id = spec_id_of(key)
        with step_errors(key):
            async with pool_of(request).connection() as conn:
                return await repo.insert_step(
                    conn,
                    actor,
                    spec_id,
                    steps_version=body.steps_version,
                    after=body.after,
                    title=body.title,
                    body=body.body,
                    depends_on=body.depends_on,
                    model=body.model,
                )

    @router.put("/specs/{key}/steps/order", response_model=StepsVersion)
    async def reorder(
        key: str, body: StepsOrder, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        spec_id = spec_id_of(key)
        with step_errors(key):
            async with pool_of(request).connection() as conn:
                version = await repo.reorder(
                    conn, actor, spec_id, steps_version=body.steps_version, order=body.order
                )
        return {"steps_version": version}

    @router.post(
        "/specs/{key}/steps/next",
        response_model=Step,
        responses={204: {"description": "No step is ready"}},
    )
    async def next_step(key: str, body: NextStep, request: Request, actor: ActorDep) -> Any:
        """The first ready step in order; with `claim` the caller takes it (`in_progress`)."""
        spec_id = spec_id_of(key)
        with step_errors(key):
            async with pool_of(request).connection() as conn:
                step = await repo.next_step(conn, actor, spec_id, claim=body.claim)
        if step is None:
            return Response(status_code=204)
        return step

    @router.get("/specs/{key}/gate", response_model=Gate)
    async def get_gate(key: str, request: Request, actor: ActorDep) -> dict[str, Any]:
        spec_id = spec_id_of(key)
        with step_errors(key):
            async with pool_of(request).connection() as conn:
                return await repo.get_gate(conn, actor, spec_id)

    return router
