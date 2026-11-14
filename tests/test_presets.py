import httpx
import pytest
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient

from agents_taskboard.presets import PresetLookup

SID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def fake() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
async def lookup(fake: FakeOrchestrator):
    client = OrchestratorClient(
        "http://orch.test", fake.token, transport=httpx.ASGITransport(app=fake.app)
    )
    lk = PresetLookup(client)
    yield lk
    await lk.aclose()


def add_session(fake: FakeOrchestrator, preset: str | None) -> None:
    fake.sessions[SID] = {"id": SID, "slug": "mx-1", "preset_id": preset, "project": "default"}


async def test_of_session_returns_preset_id(fake, lookup):
    ref = fake.add_preset("p")
    add_session(fake, str(ref.id))
    assert await lookup.of_session(SID) == str(ref.id)


async def test_of_session_unknown_is_none(lookup):
    assert await lookup.of_session(SID) is None


async def test_project_default_needs_is_default_and_project(fake, lookup):
    fake.add_project("other", "Other")
    fake.add_preset("plain", project="default")
    fake.add_preset("elsewhere", is_default=True, project="other")
    assert await lookup.project_default("default") is None
    ref = fake.add_preset("def", is_default=True, project="default")
    assert await lookup.project_default("default") == str(ref.id)


async def test_for_new_spec_falls_back_to_project_default(fake, lookup):
    own = fake.add_preset("own")
    default = fake.add_preset("def", is_default=True)
    add_session(fake, str(own.id))
    assert await lookup.for_new_spec(SID, "default") == str(own.id)
    assert await lookup.for_new_spec(None, "default") == str(default.id)
    add_session(fake, None)
    assert await lookup.for_new_spec(SID, "default") == str(default.id)


async def test_orchestrator_down_gives_none(fake, lookup):
    add_session(fake, str(fake.add_preset("p").id))
    fake.fail_next("get_session", 503)
    assert await lookup.of_session(SID) is None
    fake.fail_next("list_presets", 503)
    assert await lookup.project_default("default") is None
