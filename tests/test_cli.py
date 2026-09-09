"""The operator surface: DB-only commands need no keys, and bad config stops the process."""

from __future__ import annotations

from pathlib import Path

import pytest

from trader.cli import build_parser, main
from trader.db import KILL_SWITCH, connect, get_flag


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "t.sqlite3"
    connect(db).close()
    monkeypatch.setenv("TRADER_DB_PATH", str(db))
    monkeypatch.chdir(tmp_path)
    return db


def test_db_only_commands_need_no_credentials(env, monkeypatch, capsys) -> None:
    for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for command in (["status"], ["cycles"], ["rejections"], ["kill", "status"]):
        assert main([*command]) == 0
    capsys.readouterr()


def test_the_kill_switch_round_trips_through_the_cli(env, monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    main(["kill", "on", "--note", "cli test"])
    assert get_flag(connect(env), KILL_SWITCH) == "1"
    main(["kill", "off"])
    assert get_flag(connect(env), KILL_SWITCH) == "0"


def test_missing_broker_credentials_stop_a_trading_command(env, monkeypatch, capsys) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert main(["cycle"]) == 2
    assert "ALPACA_API_KEY is not set" in capsys.readouterr().err


def test_a_bad_risk_config_stops_the_process(env, monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    (tmp_path / "broken.toml").write_text("[limits]\nmax_dialy_loss = 1\n")
    assert main(["--risk-config", str(tmp_path / "broken.toml"), "cycle"]) == 2
    assert "unknown key" in capsys.readouterr().err


def test_a_missing_risk_config_stops_the_process(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert main(["cycle"]) == 2  # tmp_path has no risk.toml
    assert "risk config not found" in capsys.readouterr().err


def test_no_secret_is_ever_printed(env, monkeypatch, capsys) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "super-secret-value")
    main(["cycle"])
    out = capsys.readouterr()
    assert "super-secret-value" not in out.err + out.out


def test_the_parser_exposes_the_documented_commands() -> None:
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert set(actions[0].choices) == {
        "run",
        "cycle",
        "status",
        "cycles",
        "show",
        "risk",
        "rejections",
        "reconcile",
        "evaluate",
        "kill",
    }
