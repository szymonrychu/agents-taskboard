"""README API coverage: every route of the apps is documented, none invented."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from agents_taskboard.api.app import PREFIX, create_delegate_app, create_service_app, create_web_app
from agents_taskboard.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
SETTINGS = Settings(
    database_url="postgresql://unused/db",
    orchestrator_url="http://orchestrator.invalid",
    orchestrator_token="t",
    public_base_url="https://tasks.example.test",
    relay_token="r",
)


def section(title: str) -> str:
    """The body of the `## title` section (up to the next H2)."""
    match = re.search(rf"^## {re.escape(title)}\s*$", README, re.MULTILINE)
    assert match, f"README has no '## {title}' section"
    rest = README[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _walk(router, prefix: str = ""):
    """(path, methods) of every route; FastAPI includes routers lazily, so descend into them."""
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route.methods
        elif hasattr(route, "original_router"):
            yield from _walk(route.original_router, prefix + route.include_context.prefix)


def routes_of(app) -> set[str]:
    out = set()
    for path, methods in _walk(app):
        if path.startswith(PREFIX):
            out.update(f"{m} {path}" for m in methods - {"HEAD"})
    return out


def readme_routes() -> set[str]:
    found = re.findall(
        r"^### `(GET|POST|PATCH|PUT|DELETE) (/taskboard/api/v1\S*)`\s*$",
        section("API"),
        re.MULTILINE,
    )
    return {f"{m} {p}" for m, p in found}


def test_readme_lists_every_route():
    expected = routes_of(create_web_app(SETTINGS, None))  # type: ignore[arg-type]
    assert len(expected) == 12
    assert expected - readme_routes() == set(), "undocumented routes"
    assert readme_routes() - expected == set(), "README documents routes the app lacks"


def test_service_routes_are_a_documented_subset():
    service = routes_of(create_service_app(SETTINGS, None))  # type: ignore[arg-type]
    assert service < readme_routes()
    assert readme_routes() - service == {
        f"POST {PREFIX}/tasks/{{key}}/decisions/{{decision_id}}/answer",
        f"GET {PREFIX}/stream",
    }
    assert f"GET {PREFIX}/projects/summary" in service


def test_delegate_routes_are_the_web_routes_and_documented():
    delegate = routes_of(create_delegate_app(SETTINGS, None))  # type: ignore[arg-type]
    assert delegate == readme_routes()
    api = section("API")
    assert "delegate listener" in api
    config = section("Configuration")
    assert "`TASKBOARD_DELEGATE_TOKENS`" in config and "`TASKBOARD_DELEGATE_PORT`" in config


def test_readme_documents_commands_and_settings():
    commands = section("Commands")
    for name in ("migrate", "serve-web", "serve-service", "serve-delegate", "notifier"):
        assert f"`taskboard {name}`" in commands
    config = section("Configuration")
    for name in Settings.model_fields:
        assert f"`TASKBOARD_{name.upper()}`" in config, name
