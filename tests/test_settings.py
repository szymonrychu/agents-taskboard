import os

import pytest
from agents_common.settings import SettingsError

from agents_taskboard.settings import Settings, load_settings

REQUIRED = {
    "TASKBOARD_DATABASE_URL": "postgresql://u:p@db/taskboard",
    "TASKBOARD_ORCHESTRATOR_URL": "http://agents-orchestrator-services:8002",
    "TASKBOARD_ORCHESTRATOR_TOKEN": "orch-secret",
    "TASKBOARD_PUBLIC_BASE_URL": "https://tasks.example.test/",
    "TASKBOARD_RELAY_TOKEN": "relay-secret",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("TASKBOARD_"):
            monkeypatch.delenv(key)


def set_env(monkeypatch, **extra):
    for key, value in {**REQUIRED, **extra}.items():
        monkeypatch.setenv(key, value)


def test_defaults(monkeypatch):
    set_env(monkeypatch)
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.allowed_cidrs == ["127.0.0.1/32"]
    assert s.http_port == 8000
    assert s.service_port == 8001
    assert s.public_base_url == "https://tasks.example.test"
    assert s.relay_token.get_secret_value() == "relay-secret"
    assert "relay-secret" not in repr(s)
    assert "postgresql://u:p" not in repr(s)


def test_allowed_cidrs_csv(monkeypatch):
    set_env(monkeypatch, TASKBOARD_ALLOWED_CIDRS="127.0.0.1/32, 10.0.0.0/8")
    assert load_settings().allowed_cidrs == ["127.0.0.1/32", "10.0.0.0/8"]


def test_errors_name_each_variable(monkeypatch):
    set_env(monkeypatch, TASKBOARD_ALLOWED_CIDRS="nope", TASKBOARD_PUBLIC_BASE_URL="not-a-url")
    monkeypatch.delenv("TASKBOARD_RELAY_TOKEN")
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert set(exc.value.problems) == {
        "TASKBOARD_ALLOWED_CIDRS",
        "TASKBOARD_PUBLIC_BASE_URL",
        "TASKBOARD_RELAY_TOKEN",
    }


def test_delegate_tokens_default_empty(monkeypatch):
    set_env(monkeypatch)
    assert load_settings().delegate_tokens == []


def test_delegate_tokens_parsed_from_csv(monkeypatch):
    set_env(monkeypatch, TASKBOARD_DELEGATE_TOKENS="a, ,b,")
    s = load_settings()
    assert [t.get_secret_value() for t in s.delegate_tokens] == ["a", "b"]
    assert "a," not in repr(s) and "'a'" not in repr(s)


def test_delegate_token_equal_to_relay_token_is_rejected(monkeypatch):
    set_env(monkeypatch, TASKBOARD_DELEGATE_TOKENS="x,relay-secret")
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert set(exc.value.problems) == {"TASKBOARD_DELEGATE_TOKENS"}
    assert "relay-secret" not in str(exc.value)


def test_delegate_port_defaults_8003(monkeypatch):
    set_env(monkeypatch)
    assert load_settings().delegate_port == 8003


@pytest.mark.parametrize("env", ["TASKBOARD_HTTP_PORT", "TASKBOARD_SERVICE_PORT"])
def test_delegate_port_must_differ_from_http_and_service_port(monkeypatch, env):
    set_env(monkeypatch, **{env: "9100", "TASKBOARD_DELEGATE_PORT": "9100"})
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert set(exc.value.problems) == {"TASKBOARD_DELEGATE_PORT"}


@pytest.mark.parametrize("value", ["0", "65536"])
def test_delegate_port_range(monkeypatch, value):
    set_env(monkeypatch, TASKBOARD_DELEGATE_PORT=value)
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert set(exc.value.problems) == {"TASKBOARD_DELEGATE_PORT"}


def test_mindmap_tokens_default_empty_and_secret(monkeypatch):
    set_env(monkeypatch)
    assert load_settings().mindmap_tokens == []
    set_env(monkeypatch, TASKBOARD_MINDMAP_TOKENS="mm-1, mm-2")
    s = load_settings()
    assert [t.get_secret_value() for t in s.mindmap_tokens] == ["mm-1", "mm-2"]
    assert "mm-1" not in repr(s)


@pytest.mark.parametrize(
    ("extra", "clash"),
    [
        ({"TASKBOARD_MINDMAP_TOKENS": "relay-secret"}, "TASKBOARD_RELAY_TOKEN"),
        (
            {"TASKBOARD_MINDMAP_TOKENS": "x,dlg", "TASKBOARD_DELEGATE_TOKENS": "dlg"},
            "TASKBOARD_DELEGATE_TOKENS",
        ),
    ],
)
def test_settings_mindmap_token_distinct(monkeypatch, extra, clash):
    set_env(monkeypatch, **extra)
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert "TASKBOARD_MINDMAP_TOKENS" in exc.value.problems
    message = exc.value.problems["TASKBOARD_MINDMAP_TOKENS"]
    assert clash in message
    for value in ("relay-secret", "dlg", "x"):
        assert value not in message
