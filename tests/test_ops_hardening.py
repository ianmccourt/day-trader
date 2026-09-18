"""Tests for ops hardening: alerts, startup checks, heartbeat."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.fakes import FakeBroker, position
from trader.alerts import AlertSink, check_heartbeat_stale, get_recent_alerts, update_heartbeat
from trader.db import connect, open_cycle, utcnow
from trader.execution import place_order
from trader.risk.config import RiskConfig
from trader.risk.models import Proposal
from trader.startup import StartupCheckError, check_disk_space, check_log_file_size
from trader.state import build_cycle_context, trading_day_for


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "test.db")


@pytest.fixture
def alert_sink(tmp_path: Path) -> AlertSink:
    return AlertSink(alert_file=tmp_path / "alerts.jsonl", error_threshold=3)


def test_alert_sink_writes_to_file(alert_sink: AlertSink) -> None:
    alert_sink.alert("warning", "test_event", {"detail": "value"})
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert len(alerts) == 1
    assert alerts[0]["event"] == "test_event"
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["details"]["detail"] == "value"


def test_consecutive_errors_triggers_alert(conn: sqlite3.Connection, alert_sink: AlertSink) -> None:
    for i in range(3):
        conn.execute(
            "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
            (f"2026-09-18T13:{i:02d}:00Z", "2026-09-18", "error"),
        )
    conn.commit()
    alert_sink.check_consecutive_errors(conn)
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert alerts[0]["event"] == "consecutive_error_cycles"
    assert alerts[0]["severity"] == "critical"


def test_consecutive_errors_no_alert_below_threshold(
    conn: sqlite3.Connection, alert_sink: AlertSink
) -> None:
    for i in range(2):
        conn.execute(
            "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
            (f"2026-09-18T13:{i:02d}:00Z", "2026-09-18", "error"),
        )
    conn.commit()
    alert_sink.check_consecutive_errors(conn)
    assert get_recent_alerts(alert_sink.alert_file, limit=10) == []


def test_kill_switch_alert(alert_sink: AlertSink) -> None:
    alert_sink.alert_kill_switch_engaged(note="manual halt")
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert alerts[0]["event"] == "kill_switch_engaged"
    assert alerts[0]["details"]["note"] == "manual halt"


def test_daily_loss_alert(alert_sink: AlertSink) -> None:
    alert_sink.alert_daily_loss_latched("2026-09-18", 12500.0, 10000.0)
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert alerts[0]["event"] == "max_daily_loss_latched"
    assert alerts[0]["severity"] == "critical"


def test_place_order_alerts_when_daily_loss_latches(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    sink = AlertSink(alert_file=tmp_path / "alerts.jsonl")
    broker = FakeBroker(
        equity=88_000.0,
        last_equity=100_000.0,
        positions=[position("AAPL", 10, 100.0)],
        prices={"AAPL": 100.0},
    )
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    ctx = build_cycle_context(conn, broker, cycle_id=cycle_id)
    config = RiskConfig(
        max_position_notional=25_000.0,
        max_total_exposure=100_000.0,
        max_daily_loss=5_000.0,
        max_orders_per_hour=10,
        max_orders_per_day=50,
        symbol_allowlist=frozenset({"AAPL"}),
    )
    proposal = Proposal(action="buy", symbol="AAPL", qty=1, reference_price=100.0)
    result = place_order(conn, broker, ctx, proposal, config, alert_sink=sink)
    assert not result.verdict.approved
    assert "max_daily_loss" in {f.check for f in result.verdict.failures}
    alerts = get_recent_alerts(sink.alert_file)
    assert alerts[0]["event"] == "max_daily_loss_latched"


def test_heartbeat_updates(tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "heartbeat.json"
    update_heartbeat(heartbeat_file)
    heartbeat = json.loads(heartbeat_file.read_text())
    assert "timestamp" in heartbeat
    assert "pid" in heartbeat


def test_heartbeat_stale_detection(tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "heartbeat.json"
    heartbeat_file.write_text(json.dumps({"timestamp": "2020-01-01T00:00:00+00:00", "pid": 1}))
    age = check_heartbeat_stale(heartbeat_file, max_age_seconds=1)
    assert age is not None and age > 1


def test_heartbeat_fresh(tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "heartbeat.json"
    update_heartbeat(heartbeat_file)
    assert check_heartbeat_stale(heartbeat_file, max_age_seconds=600) is None


def test_disk_space_check_warns_on_low_space(tmp_path: Path) -> None:
    ok, msg = check_disk_space(tmp_path, min_mb=999_999_999, strict=False)
    assert not ok
    assert "Low disk space" in msg


def test_disk_space_check_strict_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(StartupCheckError, match="Low disk space"):
        check_disk_space(tmp_path, min_mb=999_999_999, strict=True)


def test_log_file_size_check(tmp_path: Path) -> None:
    log_file = tmp_path / "test.log"
    log_file.write_text("x" * 200)
    ok, msg = check_log_file_size(log_file, max_mb=0, strict=False)
    assert not ok
    assert "Large log file" in msg
    assert "log rotation" in msg


def test_log_file_size_check_strict_mode_raises(tmp_path: Path) -> None:
    log_file = tmp_path / "test.log"
    log_file.write_text("x" * 200)
    with pytest.raises(StartupCheckError, match="Large log file"):
        check_log_file_size(log_file, max_mb=0, strict=True)


def test_log_file_size_ok_for_small_files(tmp_path: Path) -> None:
    log_file = tmp_path / "test.log"
    log_file.write_text("small log\n")
    ok, msg = check_log_file_size(log_file, max_mb=500, strict=False)
    assert ok
    assert "OK" in msg


def test_alert_webhook_best_effort(alert_sink: AlertSink) -> None:
    alert_sink.webhook_url = "http://127.0.0.1:1/webhook"
    alert_sink.alert("info", "test_event", {})
    assert get_recent_alerts(alert_sink.alert_file, limit=10)[0]["event"] == "test_event"
