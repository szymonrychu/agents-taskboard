"""serve-web and serve-service: the two http listeners as subcommands."""

import os

import pytest

from agents_taskboard.cli import EXIT_CONFIG, build_parser, main


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("TASKBOARD_"):
            monkeypatch.delenv(key)


def test_commands_registered():
    parser = build_parser()
    text = parser.format_help()
    for name in ("serve-web", "serve-service", "serve-delegate", "notifier", "migrate"):
        assert name in text


@pytest.mark.parametrize("command", ["serve-web", "serve-service"])
def test_serve_with_bad_config_exits_78(command, capsys):
    assert main([command]) == EXIT_CONFIG
    assert "TASKBOARD_RELAY_TOKEN" in capsys.readouterr().err


def test_serve_delegate_without_tokens_exits_78(monkeypatch, capsys):
    for key, value in {
        "TASKBOARD_DATABASE_URL": "postgresql://u:p@db/taskboard",
        "TASKBOARD_ORCHESTRATOR_URL": "http://orch.invalid",
        "TASKBOARD_ORCHESTRATOR_TOKEN": "t",
        "TASKBOARD_PUBLIC_BASE_URL": "https://tasks.example.test",
        "TASKBOARD_RELAY_TOKEN": "relay-secret",
    }.items():
        monkeypatch.setenv(key, value)
    assert main(["serve-delegate"]) == EXIT_CONFIG
    assert "TASKBOARD_DELEGATE_TOKENS" in capsys.readouterr().err
