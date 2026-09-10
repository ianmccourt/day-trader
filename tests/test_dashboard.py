"""The control panel is loopback-only, never submits orders, and never leaks secrets."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.fakes import position
from trader.config import Settings
from trader.constants import ALPACA_PAPER_BASE_URL, BROKER_ROBINHOOD_AGENTIC
from trader.dashboard import (
    DashboardApp,
    LoopProcess,
    cycle_detail,
    make_handler,
    session_snapshot,
    tail_log,
)
from trader.db import connect, open_cycle, record_decision, utcnow
from trader.risk.config import RiskConfig
from trader.robinhood_mcp import ROBINHOOD_MCP_URL
from trader.state import trading_day_for

VALID_TOML = """
[limits]
max_position_notional = 5000.0
max_total_exposure = 25000.0
max_daily_loss = 2000.0
max_orders_per_hour = 6
max_orders_per_day = 20
max_orders_per_cycle = 1

[universe]
symbol_allowlist = ["AAPL", "SPY"]

[session]
regular_trading_hours_only = true
allow_shorts = false
"""


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "t.sqlite3")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        alpaca_api_key="",
        alpaca_secret_key="",
        db_path=tmp_path / "t.sqlite3",
        cycle_minutes=5,
        log_level="INFO",
    )


@pytest.fixture
def risk_path(tmp_path: Path) -> Path:
    path = tmp_path / "risk.toml"
    path.write_text(VALID_TOML)
    return path


def test_snapshot_is_db_only_and_omits_prompts(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    conn.execute(
        "UPDATE cycles SET status = 'ok', ended_at = ?, duration_ms = 12, "
        "full_prompt = 'SECRET_PROMPT', full_response = 'hold' WHERE cycle_id = ?",
        (utcnow().isoformat(), cycle_id),
    )
    record_decision(conn, cycle_id=cycle_id, action="no_action", reasoning="n")
    loop = LoopProcess(pid_path=Path("/tmp/does-not-exist.pid"), log_path=Path("/tmp/no.log"))
    snap = session_snapshot(
        conn,
        settings,
        loop=loop,
        risk=RiskConfig(
            max_position_notional=1,
            max_total_exposure=1,
            max_daily_loss=1,
            max_orders_per_hour=1,
            max_orders_per_day=1,
            symbol_allowlist=frozenset({"AAPL"}),
        ),
        risk_error=None,
        risk_source="risk.toml",
    )
    assert snap["paper"] is True
    assert snap["broker"] == "paper"
    assert snap["broker_base_url"] == ALPACA_PAPER_BASE_URL
    assert snap["cycles_today"] == 1
    assert snap["last_cycle"]["cycle_id"] == cycle_id
    assert "SECRET_PROMPT" not in json.dumps(snap)
    assert snap["loop"]["running"] is False


def test_snapshot_shows_the_robinhood_broker(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = Settings(
        alpaca_api_key="",
        alpaca_secret_key="",
        db_path=tmp_path / "t.sqlite3",
        cycle_minutes=5,
        log_level="INFO",
        broker=BROKER_ROBINHOOD_AGENTIC,
        robinhood_token_path=tmp_path / "rh.json",
    )
    snap = session_snapshot(
        conn,
        settings,
        loop=LoopProcess(pid_path=tmp_path / "x.pid", log_path=tmp_path / "x.log"),
        risk=None,
        risk_error=None,
        risk_source="risk.toml",
    )
    assert snap["paper"] is False
    assert snap["broker"] == BROKER_ROBINHOOD_AGENTIC
    assert snap["broker_base_url"] == ROBINHOOD_MCP_URL


def test_cycle_detail_strips_the_prompt(conn: sqlite3.Connection) -> None:
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day="2026-09-09")
    conn.execute(
        "UPDATE cycles SET status = 'ok', full_prompt = 'KEEP_OUT', full_response = 'hi' "
        "WHERE cycle_id = ?",
        (cycle_id,),
    )
    detail = cycle_detail(conn, cycle_id)
    assert detail is not None
    assert detail["full_prompt"] == "<omitted>"
    assert detail["full_response"] == "hi"


def test_tail_log_parses_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"msg":"cycle_end","level":"INFO"}\nnot-json\n')
    rows = tail_log(path, n=10)
    assert rows[0]["msg"] == "cycle_end"
    assert rows[1]["msg"] == "not-json"


def test_next_scheduled_cycle_follows_start_and_stop() -> None:
    from trader.dashboard import next_scheduled_cycle

    rows = [
        {"msg": "scheduler_start", "next_cycle": "first"},
        {"msg": "scheduler_stopped"},
        {"msg": "scheduler_start", "next_cycle": "2026-09-10T09:00:00-04:00"},
    ]
    assert next_scheduled_cycle(rows) == "2026-09-10T09:00:00-04:00"
    rows.append({"msg": "scheduler_stopped"})
    assert next_scheduled_cycle(rows) is None


def test_loop_process_start_and_stop(tmp_path: Path) -> None:
    loop = LoopProcess(pid_path=tmp_path / "t.pid", log_path=tmp_path / "t.jsonl")
    started = loop.start(
        [],
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    assert started["ok"] is True
    assert loop.running()
    again = loop.start([], command=[sys.executable, "-c", "pass"])
    assert again["ok"] is False
    stopped = loop.stop()
    assert stopped["ok"] is True
    assert loop.running() is False


def test_kill_and_snapshot_round_trip_over_http(
    tmp_path: Path, settings: Settings, risk_path: Path
) -> None:
    connect(settings.db_path).close()
    app = DashboardApp(
        settings,
        loop=LoopProcess(pid_path=tmp_path / "t.pid", log_path=tmp_path / "t.jsonl"),
        risk_config_path=risk_path,
        run_argv=["--stub"],
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        home = urlopen(f"{base}/", timeout=2).read().decode()
        assert "Start loop" in home
        snap = json.loads(urlopen(f"{base}/api/snapshot", timeout=2).read())
        assert snap["kill_switch"] is False
        assert snap["risk"]["symbol_allowlist"] == ["AAPL", "SPY"]
        req = Request(
            f"{base}/api/kill",
            data=json.dumps({"state": "on", "note": "test"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        killed = json.loads(urlopen(req, timeout=2).read())
        assert killed["kill_switch"] is True
        snap = json.loads(urlopen(f"{base}/api/snapshot", timeout=2).read())
        assert snap["kill_switch"] is True
        assert snap["kill_note"] == "test"
    finally:
        server.shutdown()
        server.server_close()


def test_http_404(tmp_path: Path, settings: Settings, risk_path: Path) -> None:
    connect(settings.db_path).close()
    app = DashboardApp(
        settings,
        loop=LoopProcess(pid_path=tmp_path / "t.pid", log_path=tmp_path / "t.jsonl"),
        risk_config_path=risk_path,
        run_argv=[],
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    host, port = server.server_address[:2]
    try:
        with pytest.raises(HTTPError) as exc:
            urlopen(f"http://{host}:{port}/nope", timeout=2)
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_snapshot_never_includes_env_secrets(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPACA_SECRET_KEY", "super-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    snap = session_snapshot(
        conn,
        settings,
        loop=LoopProcess(pid_path=Path("/tmp/x.pid"), log_path=Path("/tmp/x.log")),
        risk=None,
        risk_error=None,
        risk_source="risk.toml",
    )
    dumped = json.dumps(snap)
    assert "super-secret-value" not in dumped
    assert "sk-ant-secret" not in dumped


def test_positions_render_from_the_latest_snapshot(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    p = position("AAPL", 10, 100.0, 101.0)
    conn.execute(
        "INSERT INTO positions_snapshot(cycle_id, symbol, qty, avg_price, current_price, "
        "market_value, unrealized_pl, captured_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            cycle_id,
            p["symbol"],
            p["qty"],
            p["avg_price"],
            p["current_price"],
            p["market_value"],
            p["unrealized_pl"],
            utcnow().isoformat(),
        ),
    )
    snap = session_snapshot(
        conn,
        settings,
        loop=LoopProcess(pid_path=Path("/tmp/x.pid"), log_path=Path("/tmp/x.log")),
        risk=None,
        risk_error=None,
        risk_source="risk.toml",
    )
    assert snap["positions"][0]["symbol"] == "AAPL"
    assert snap["positions"][0]["qty"] == 10
