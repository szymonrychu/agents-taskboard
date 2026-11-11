"""Spec and document routes (base design 3.5, 4.2), mounted on every listener. Only the actor
dependency differs; `user_routes=True` (web and delegate listeners) adds the user-only routes,
so the relay answers 404 for them like for any unknown route."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response

from agents_taskboard.api.actors import Actor
from agents_taskboard.api.errors import ApiError
from agents_taskboard.api.schemas import (
    CommentList,
    DocumentBody,
    DocumentList,
    DocumentPut,
    DocumentVersion,
    Section,
    SpecComment,
    SpecCommentCreate,
    SpecCreate,
    SpecDetail,
    SpecPage,
    SpecPatch,
    SpecStatus,
    SuggestionAccept,
    SuggestionAccepted,
    VersionPage,
)
from agents_taskboard.api.tasks import MAX_LIMIT, pool_of, public_url, require_target
from agents_taskboard.db import specs as repo
from agents_taskboard.labels import normalize_labels
from agents_taskboard.sections import find_section

MARKDOWN = "text/markdown"
VERSION_HEADER = "X-Spec-Version"


def spec_id_of(key: str) -> int:
    spec_id = repo.parse_spec_key(key)
    if spec_id is None:
        raise ApiError(404, "not_found", f"no spec {key}")
    return spec_id


def spec_url(public: str, key: str) -> str:
    return f"{public}/specs/{key}"


@contextmanager
def repo_errors(key: str) -> Iterator[None]:
    """Map repository errors to API errors."""
    try:
        yield
    except repo.NotFound:
        raise ApiError(404, "not_found", f"no spec {key}") from None
    except repo.NameTaken:
        raise ApiError(409, "name_taken", "a spec of this project has that name") from None
    except repo.StaleVersion as exc:
        raise ApiError(
            412,
            "stale_version",
            "the document changed since it was read",
            {"current_version": exc.current_version},
        ) from None
    except (repo.TooLarge, repo.TooManyDocuments) as exc:
        raise ApiError(413, "payload_too_large", str(exc) or "over the cap") from None
    except (repo.InvalidPath, repo.InvalidRequest, repo.BadCursor) as exc:
        raise ApiError(422, "invalid_request", str(exc)) from None
    except repo.UserOnly as exc:
        raise ApiError(403, "user_only", str(exc)) from None
    except repo.AlreadyResolved:
        raise ApiError(409, "already_resolved", "the comment is already resolved") from None
    except repo.AlreadyDecided:
        raise ApiError(409, "already_decided", "the suggestion is already decided") from None
    except repo.NoSuggestion:
        raise ApiError(422, "no_suggestion", "the comment has no suggestion") from None


async def load_spec(request: Request, actor: Actor, key: str) -> dict[str, Any]:
    """The spec JSON, or 404 (also deleted or outside the actor's projects)."""
    spec_id = spec_id_of(key)
    async with pool_of(request).connection() as conn:
        spec = await repo.get_spec(conn, actor, spec_id)
    if spec is None:
        raise ApiError(404, "not_found", f"no spec {key}")
    spec["url"] = spec_url(public_url(request), spec["key"])
    return spec


def build_spec_router(actor_dep: Callable[..., Actor], *, user_routes: bool) -> APIRouter:
    router = APIRouter()
    ActorDep = Annotated[Actor, Depends(actor_dep)]

    @router.get("/specs", response_model=SpecPage)
    async def list_specs(
        request: Request,
        actor: ActorDep,
        project: str | None = None,
        status: SpecStatus | None = None,
        label: Annotated[list[str] | None, Query()] = None,
        name: str | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        try:
            labels = normalize_labels(label) if label else None
        except ValueError as exc:
            raise ApiError(422, "invalid_request", str(exc)) from None
        with repo_errors("list"):
            async with pool_of(request).connection() as conn:
                items, next_cursor = await repo.list_specs(
                    conn,
                    actor,
                    project=project,
                    status=status,
                    labels=labels,
                    name=name,
                    limit=limit,
                    cursor=cursor,
                )
        public = public_url(request)
        for item in items:
            item["url"] = spec_url(public, item["key"])
        return {"items": items, "next_cursor": next_cursor}

    @router.post("/specs", status_code=201, response_model=SpecDetail)
    async def create_spec(
        body: SpecCreate, request: Request, response: Response, actor: ActorDep
    ) -> dict[str, Any]:
        project = body.project or actor.default_project()
        await require_target(request, actor, project)
        with repo_errors(body.name):
            async with pool_of(request).connection() as conn:
                spec, created = await repo.create_spec(
                    conn,
                    actor,
                    project=project,
                    name=body.name,
                    title=body.title,
                    labels=body.labels,
                    client_key=body.client_key,
                    docs=[d.model_dump() for d in body.docs],
                )
        if not created:
            response.status_code = 200
        return await load_spec(request, actor, spec["key"])

    @router.get("/specs/{key}", response_model=SpecDetail)
    async def get_spec(key: str, request: Request, actor: ActorDep) -> dict[str, Any]:
        return await load_spec(request, actor, key)

    @router.patch("/specs/{key}", response_model=SpecDetail)
    async def patch_spec(
        key: str, body: SpecPatch, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        spec_id = spec_id_of(key)
        changes: dict[str, Any] = {}
        for field in ("title", "status", "labels", "project"):
            if field in body.model_fields_set:
                value = getattr(body, field)
                if value is None:
                    raise ApiError(422, "invalid_request", f"{field} cannot be null")
                changes[field] = value
        if body.assignee_self:
            if actor.session_id is None:
                raise ApiError(422, "invalid_request", "assignee_self needs an agent session")
            changes["assignee_session"] = actor.session_id
            changes["assignee_slug"] = actor.slug
        elif "assignee_session" in body.model_fields_set:
            changes["assignee_session"] = body.assignee_session
            changes["assignee_slug"] = None
        if not changes:
            raise ApiError(422, "invalid_request", "no fields to change")
        if "project" in changes:
            current = await load_spec(request, actor, key)
            if changes["project"] != current["project"]:
                await require_target(request, actor, changes["project"])
        with repo_errors(key):
            async with pool_of(request).connection() as conn:
                await repo.patch_spec(conn, actor, spec_id, body.version, changes)
        return await load_spec(request, actor, key)

    if user_routes:

        @router.delete("/specs/{key}", status_code=204)
        async def delete_spec(key: str, request: Request, actor: ActorDep) -> Response:
            if actor.kind != "user":
                raise ApiError(403, "user_only", "only the user deletes a spec")
            spec_id = spec_id_of(key)
            with repo_errors(key):
                async with pool_of(request).connection() as conn:
                    await repo.soft_delete(conn, actor, spec_id)
            return Response(status_code=204)

    else:

        @router.delete("/specs/{key}", status_code=204, include_in_schema=False)
        async def delete_spec_relay(key: str) -> Response:
            """Agents never delete: the relay answers like an unknown route (404), not 405."""
            raise ApiError(404, "not_found", "Not Found")

    @router.get("/specs/{key}/docs", response_model=DocumentList)
    async def list_documents(key: str, request: Request, actor: ActorDep) -> dict[str, Any]:
        spec = await load_spec(request, actor, key)
        async with pool_of(request).connection() as conn:
            items = await repo.list_documents(conn, spec_id_of(spec["key"]))
        return {"items": items}

    @router.get("/specs/{key}/docs/{path:path}/versions", response_model=VersionPage)
    async def list_versions(
        key: str,
        path: str,
        request: Request,
        actor: ActorDep,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
        cursor: Annotated[int | None, Query(ge=1)] = None,
    ) -> dict[str, Any]:
        spec = await load_spec(request, actor, key)
        async with pool_of(request).connection() as conn:
            items, next_cursor = await repo.list_versions(
                conn, spec_id_of(spec["key"]), path, limit, cursor
            )
        if not items and cursor is None:
            raise ApiError(404, "not_found", f"no document {path} in {key}")
        return {"items": items, "next_cursor": next_cursor}

    @router.get("/specs/{key}/docs/{path:path}/section", response_model=Section)
    async def get_section(
        key: str,
        path: str,
        request: Request,
        actor: ActorDep,
        heading: Annotated[str, Query(min_length=1, max_length=200)],
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> dict[str, Any]:
        """One section of a document: the heading whose web anchor slug is `heading` (the
        second of two equal headings is `<slug>-2`) and its text up to the next heading of the
        same or a higher level, under 40000 characters (longer text is cut at a line boundary
        and `truncated` is true)."""
        spec = await load_spec(request, actor, key)
        async with pool_of(request).connection() as conn:
            doc = await repo.get_document(conn, spec_id_of(spec["key"]), path, version)
        if doc is None:
            raise ApiError(404, "not_found", f"no document {path} in spec {key}")
        found = find_section(doc["body"], heading)
        if found is None:
            raise ApiError(404, "not_found", f"no section {heading} in {path}")
        text, truncated = found
        return {
            "path": path,
            "version": doc["version"],
            "heading": heading,
            "body": text,
            "truncated": truncated,
        }

    @router.get(
        "/specs/{key}/docs/{path:path}",
        response_model=DocumentBody,
        responses={
            200: {"content": {MARKDOWN: {"schema": {"type": "string"}}}},
            304: {"description": "Not modified (If-None-Match matched the ETag)"},
        },
    )
    async def get_document(
        key: str,
        path: str,
        request: Request,
        actor: ActorDep,
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> Any:
        """The document JSON, or with `Accept: text/markdown` the raw body with
        `ETag: "<sha256>"` and `X-Spec-Version`."""
        spec = await load_spec(request, actor, key)
        async with pool_of(request).connection() as conn:
            doc = await repo.get_document(conn, spec_id_of(spec["key"]), path, version)
        if doc is None:
            raise ApiError(404, "not_found", f"no document {path} in spec {key}")
        if MARKDOWN not in request.headers.get("accept", ""):
            return doc
        etag = f'"{doc["sha256"]}"'
        headers = {"ETag": etag, VERSION_HEADER: str(doc["version"])}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(doc["body"], media_type=f"{MARKDOWN}; charset=utf-8", headers=headers)

    @router.put("/specs/{key}/docs/{path:path}", status_code=201, response_model=DocumentVersion)
    async def put_document(
        key: str,
        path: str,
        body: DocumentPut,
        request: Request,
        response: Response,
        actor: ActorDep,
    ) -> dict[str, Any]:
        spec_id = spec_id_of(key)
        with repo_errors(key):
            async with pool_of(request).connection() as conn:
                version, created = await repo.put_document(
                    conn,
                    actor,
                    spec_id,
                    path,
                    body=body.body,
                    base_version=body.base_version,
                    note=body.note,
                    sources=body.sources,
                    client_key=body.client_key,
                )
        if not created:
            response.status_code = 200
        return version

    @router.get("/specs/{key}/comments", response_model=CommentList)
    async def list_comments(
        key: str,
        request: Request,
        actor: ActorDep,
        path: str | None = None,
        open: bool | None = None,  # noqa: A002 - the documented query parameter
    ) -> dict[str, Any]:
        spec = await load_spec(request, actor, key)
        async with pool_of(request).connection() as conn:
            items = await repo.list_comments(conn, spec_id_of(spec["key"]), path, open)
        return {"items": items}

    @router.post("/specs/{key}/comments", status_code=201, response_model=SpecComment)
    async def add_comment(
        key: str,
        body: SpecCommentCreate,
        request: Request,
        response: Response,
        actor: ActorDep,
    ) -> dict[str, Any]:
        spec_id = spec_id_of(key)
        with repo_errors(key):
            async with pool_of(request).connection() as conn:
                comment, created = await repo.add_comment(
                    conn,
                    actor,
                    spec_id,
                    path=body.path,
                    version=body.version,
                    anchor=body.anchor,
                    body=body.body,
                    suggestion=body.suggestion,
                    attachments=body.attachments,
                    client_key=body.client_key,
                )
        if not created:
            response.status_code = 200
        return comment

    @router.post("/specs/{key}/comments/{comment_id}/resolve", response_model=SpecComment)
    async def resolve_comment(
        key: str, comment_id: int, request: Request, actor: ActorDep
    ) -> dict[str, Any]:
        spec_id = spec_id_of(key)
        with repo_errors(key):
            async with pool_of(request).connection() as conn:
                return await repo.resolve_comment(conn, actor, spec_id, comment_id)

    if user_routes:

        @router.post(
            "/specs/{key}/comments/{comment_id}/accept",
            status_code=201,
            response_model=SuggestionAccepted,
        )
        async def accept_suggestion(
            key: str, comment_id: int, body: SuggestionAccept, request: Request, actor: ActorDep
        ) -> dict[str, Any]:
            """Write the suggestion as the next version of the comment's document."""
            if actor.kind != "user":
                raise ApiError(403, "user_only", "only the user accepts a suggestion")
            spec_id = spec_id_of(key)
            with repo_errors(key):
                async with pool_of(request).connection() as conn:
                    version, comment = await repo.accept_suggestion(
                        conn, actor, spec_id, comment_id, body.version
                    )
            return {"version": version, "comment": comment}

        @router.post("/specs/{key}/comments/{comment_id}/dismiss", response_model=SpecComment)
        async def dismiss_suggestion(
            key: str, comment_id: int, request: Request, actor: ActorDep
        ) -> dict[str, Any]:
            if actor.kind != "user":
                raise ApiError(403, "user_only", "only the user dismisses a suggestion")
            spec_id = spec_id_of(key)
            with repo_errors(key):
                async with pool_of(request).connection() as conn:
                    return await repo.dismiss_suggestion(conn, actor, spec_id, comment_id)

    return router
