"""Deterministic OpenAPI export of the web app: `python -m agents_taskboard.api.openapi`.

Redirect stdout to `openapi.json`.
The web app is the user-facing contract (it includes the answer route; the service app is the
relay's subset and has none; the delegate app serves the same routes as the web app, with 401
instead of 403 as its gate error). FastAPI documents a validation error as its own
`HTTPValidationError`; this service answers every error with `{"error": {"code", "message"}}`
(see `errors.py`), so the export replaces the generated error responses with that shape and
the statuses each route really returns.
"""

from __future__ import annotations

import json
from typing import Any

from agents_taskboard.api.app import PREFIX, create_web_app
from agents_taskboard.settings import Settings

ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}

ERROR_SCHEMAS: dict[str, Any] = {
    "ErrorBody": {
        "type": "object",
        "title": "ErrorBody",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string", "title": "Code"},
            "message": {"type": "string", "title": "Message"},
        },
    },
    "ErrorResponse": {
        "type": "object",
        "title": "ErrorResponse",
        "required": ["error"],
        "properties": {"error": {"$ref": "#/components/schemas/ErrorBody"}},
    },
}

ERRORS = {
    403: "source address is not allowed (forbidden)",
    404: "no such task, decision or todo item (not_found)",
    409: "the decision is not open (already_answered, not_open)",
    412: "the task changed since it was read (stale_version)",
    413: "over a size cap (payload_too_large)",
    422: "invalid request (invalid_request)",
    503: "the orchestrator cannot validate the project (upstream_unavailable)",
}

PROJECT_422 = "invalid request (invalid_request, unknown_project, project_archived)"

SPEC_ERRORS = {
    403: "forbidden (forbidden, user_only)",
    404: "no such spec, document or comment (not_found)",
    409: "conflict (name_taken, already_resolved, already_decided)",
    412: "changed since it was read (stale_version, with current_version on a document)",
    422: "invalid request (invalid_request, no_suggestion)",
}

ROUTE_ERRORS: dict[tuple[str, str], tuple[int, ...]] = {
    ("get", "/stream"): (403,),
    ("get", "/projects/summary"): (403,),
    ("post", "/tasks"): (403, 422, 503),
    ("get", "/tasks"): (403, 422),
    ("get", "/tasks/{key}"): (403, 404, 422),
    ("patch", "/tasks/{key}"): (403, 404, 412, 422, 503),
    ("post", "/tasks/{key}/comments"): (403, 404, 422),
    ("put", "/tasks/{key}/todo"): (403, 404, 422),
    ("patch", "/tasks/{key}/todo/{item_id}"): (403, 404, 422),
    ("post", "/tasks/{key}/decisions"): (403, 404, 422),
    ("post", "/tasks/{key}/decisions/{decision_id}/withdraw"): (403, 404, 409, 422),
    ("post", "/tasks/{key}/decisions/{decision_id}/answer"): (403, 404, 409, 422),
    ("get", "/specs"): (403, 422),
    ("post", "/specs"): (403, 409, 413, 422, 503),
    ("get", "/specs/{key}"): (403, 404),
    ("patch", "/specs/{key}"): (403, 404, 409, 412, 422, 503),
    ("delete", "/specs/{key}"): (403, 404),
    ("get", "/specs/{key}/docs"): (403, 404),
    ("get", "/specs/{key}/docs/{path}"): (403, 404, 422),
    ("put", "/specs/{key}/docs/{path}"): (403, 404, 412, 413, 422),
    ("get", "/specs/{key}/docs/{path}/versions"): (403, 404, 422),
    ("get", "/specs/{key}/comments"): (403, 404, 422),
    ("post", "/specs/{key}/comments"): (403, 404, 413, 422),
    ("post", "/specs/{key}/comments/{comment_id}/resolve"): (403, 404, 409, 422),
    ("post", "/specs/{key}/comments/{comment_id}/accept"): (403, 404, 409, 412, 422),
    ("post", "/specs/{key}/comments/{comment_id}/dismiss"): (403, 404, 409, 422),
}

PROJECT_ROUTES = {
    ("post", "/tasks"),
    ("patch", "/tasks/{key}"),
    ("post", "/specs"),
    ("patch", "/specs/{key}"),
}


def _document() -> dict[str, Any]:
    settings = Settings(
        database_url="postgresql://openapi/export",
        orchestrator_url="http://orchestrator.invalid",
        orchestrator_token="openapi-export",
        public_base_url="https://tasks.invalid",
        relay_token="openapi-export",
    )
    app = create_web_app(settings, pool=None)  # type: ignore[arg-type]
    doc = app.openapi()
    for path, methods in doc["paths"].items():
        if not path.startswith(PREFIX):
            continue
        for method, operation in methods.items():
            responses = operation["responses"]
            for status in [s for s in responses if not s.startswith(("2", "3"))]:
                del responses[status]
            route = (method, path.removeprefix(PREFIX))
            for status in ROUTE_ERRORS[route]:
                if status == 422 and route in PROJECT_ROUTES:
                    description = PROJECT_422
                elif route[1].startswith("/specs") and status in SPEC_ERRORS:
                    description = SPEC_ERRORS[status]
                else:
                    description = ERRORS[status]
                responses[str(status)] = {
                    "description": description,
                    "content": {"application/json": {"schema": ERROR_REF}},
                }
    schemas = doc["components"]["schemas"]
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    schemas.update(ERROR_SCHEMAS)
    return doc


def export() -> str:
    """The web app's OpenAPI document as JSON with sorted keys and a trailing newline."""
    return json.dumps(_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(export(), end="")
