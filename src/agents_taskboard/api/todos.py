"""Todo checklist routes, mounted on both apps like the tasks routes."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.errors import ApiError
from agents_taskboard.api.schemas import TodoPatch, TodoReplace
from agents_taskboard.api.tasks import load_one, pool_of, task_id_of
from agents_taskboard.db import tasks as tasks_repo
from agents_taskboard.db import todos as repo


def build_router(actor_dep: Callable[..., Actor]) -> APIRouter:
    router = APIRouter()
    ActorDep = Annotated[Actor, Depends(actor_dep)]

    @router.put("/tasks/{key}/todo")
    async def replace_todo(
        key: str, body: TodoReplace, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        task_id = task_id_of(key)
        items = [(i.text, i.status) for i in body.items]
        try:
            async with pool_of(request).connection() as conn:
                await repo.replace_todo(conn, actor, task_id, items)
        except tasks_repo.NotFound:
            raise ApiError(404, "not_found", f"no task {key}") from None
        return await load_one(request, actor, task_id)

    @router.patch("/tasks/{key}/todo/{item_id}")
    async def patch_item(
        key: str, item_id: int, body: TodoPatch, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        task_id = task_id_of(key)
        changes = {f: getattr(body, f) for f in ("text", "status") if f in body.model_fields_set}
        try:
            async with pool_of(request).connection() as conn:
                await repo.patch_item(conn, actor, task_id, item_id, changes)
        except tasks_repo.NotFound:
            raise ApiError(404, "not_found", f"no item {item_id} on {key}") from None
        return await load_one(request, actor, task_id)

    return router
