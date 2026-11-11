"""The committed openapi.json is the web app; the service app lacks the answer route and the
delegate app serves exactly the web app's routes."""

import json
from pathlib import Path

from fastapi.routing import APIRoute

from agents_taskboard.api import openapi
from agents_taskboard.api.app import PREFIX, create_delegate_app, create_service_app, create_web_app
from agents_taskboard.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
ANSWER = f"{PREFIX}/tasks/{{key}}/decisions/{{decision_id}}/answer"
SETTINGS = Settings(
    database_url="postgresql://unused/db",
    orchestrator_url="http://orchestrator.invalid",
    orchestrator_token="t",
    public_base_url="https://tasks.example.test",
    relay_token="r",
)


def _paths(app) -> set[str]:
    out = set()

    def walk(router, prefix=""):
        for route in router.routes:
            if isinstance(route, APIRoute) and route.include_in_schema:
                out.add(prefix + route.path_format)
            elif hasattr(route, "original_router"):
                walk(route.original_router, prefix + route.include_context.prefix)

    walk(app)
    return out


def _methods(app) -> set[tuple[str, str]]:
    out = set()

    def walk(router, prefix=""):
        for route in router.routes:
            if isinstance(route, APIRoute):
                out.update((m, prefix + route.path) for m in route.methods)
            elif hasattr(route, "original_router"):
                walk(route.original_router, prefix + route.include_context.prefix)

    walk(app)
    return out


def test_openapi_json_matches_apps():
    committed = (ROOT / "openapi.json").read_text(encoding="utf-8")
    assert committed == openapi.export(), (
        "run: uv run python -m agents_taskboard.api.openapi > openapi.json"
    )
    assert committed.endswith("\n")
    documented = {p for p in json.loads(committed)["paths"] if p.startswith(PREFIX)}
    assert ANSWER in documented
    assert documented == {p for p in _paths(create_web_app(SETTINGS, None)) if p.startswith(PREFIX)}


def test_service_app_has_no_answer_route():
    service = _paths(create_service_app(SETTINGS, None))
    assert ANSWER not in service
    assert any(p.startswith(f"{PREFIX}/tasks") for p in service)


def test_delegate_app_routes_equal_web_app_routes():
    web = _methods(create_web_app(SETTINGS, None))
    delegate = _methods(create_delegate_app(SETTINGS, None))
    assert web and delegate == web
