import os

import pytest

from agents_taskboard.cli import EXIT_CONFIG, main


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("TASKBOARD_"):
            monkeypatch.delenv(key)


def test_migrate_with_bad_config_exits_78_naming_variables(capsys):
    assert main(["migrate"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "TASKBOARD_DATABASE_URL" in err
    assert "TASKBOARD_RELAY_TOKEN" in err


def test_migrate_applies_and_prints_names(monkeypatch, empty_db_url, capsys):
    for key, value in {
        "DATABASE_URL": empty_db_url,
        "ORCHESTRATOR_URL": "http://o.invalid",
        "ORCHESTRATOR_TOKEN": "t",
        "PUBLIC_BASE_URL": "https://tasks.example.test",
        "RELAY_TOKEN": "r",
    }.items():
        monkeypatch.setenv(f"TASKBOARD_{key}", value)
    assert main(["migrate"]) == 0
    assert capsys.readouterr().out.split() == [
        "0001_taskboard.sql",
        "0002_projects.sql",
        "0003_decision_withdrawal.sql",
        "0004_specs.sql",
    ]
    assert main(["migrate"]) == 0
    assert capsys.readouterr().out == ""


def test_notifier_with_bad_config_exits_78(capsys):
    assert main(["notifier"]) == EXIT_CONFIG
    assert "TASKBOARD_DATABASE_URL" in capsys.readouterr().err
