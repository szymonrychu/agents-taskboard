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
    422: "invalid request (invalid_request)",
    503: "the orchestrator cannot validate the project (upstream_unavailable)",
}

PROJECT_422 = "invalid request (invalid_request, unknown_project, project_archived)"

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
}

PROJECT_ROUTES = {("post", "/tasks"), ("patch", "/tasks/{key}")}


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
            for status in [s for s in responses if not s.startswith("2")]:
                del responses[status]
            for status in ROUTE_ERRORS[(method, path.removeprefix(PREFIX))]:
                project_422 = (
                    status == 422 and (method, path.removeprefix(PREFIX)) in PROJECT_ROUTES
                )
                responses[str(status)] = {
                    "description": PROJECT_422 if project_422 else ERRORS[status],
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
