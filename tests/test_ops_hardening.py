"""Tests for ops hardening: alerts, startup checks, token headroom."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trader.alerts import AlertSink, check_heartbeat_stale, get_recent_alerts, update_heartbeat
from trader.db import connect
from trader.startup import check_disk_space, check_log_file_size, StartupCheckError


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "test.db")


@pytest.fixture
def alert_sink(tmp_path: Path) -> AlertSink:
    return AlertSink(alert_file=tmp_path / "alerts.jsonl", error_threshold=3)


def test_alert_sink_writes_to_file(alert_sink: AlertSink) -> None:
    """Alerts are written to the alert file."""
    alert_sink.alert("warning", "test_event", {"detail": "value"})
    
    assert alert_sink.alert_file.exists()
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert len(alerts) == 1
    assert alerts[0]["event"] == "test_event"
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["details"]["detail"] == "value"


def test_consecutive_errors_triggers_alert(conn: sqlite3.Connection, alert_sink: AlertSink) -> None:
    """Alert fires when error cycles exceed threshold."""
    # Insert 3 consecutive error cycles
    for i in range(3):
        conn.execute(
            "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
            (f"2026-09-18T13:{i:02d}:00Z", "2026-09-18", "error"),
        )
    conn.commit()
    
    alert_sink.check_consecutive_errors(conn)
    
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert len(alerts) == 1
    assert alerts[0]["event"] == "consecutive_error_cycles"
    assert alerts[0]["severity"] == "critical"


def test_consecutive_errors_no_alert_below_threshold(
    conn: sqlite3.Connection, alert_sink: AlertSink
) -> None:
    """No alert if error count is below threshold."""
    # Insert 2 error cycles (below threshold of 3)
    for i in range(2):
        conn.execute(
            "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
            (f"2026-09-18T13:{i:02d}:00Z", "2026-09-18", "error"),
        )
    conn.commit()
    
    alert_sink.check_consecutive_errors(conn)
    
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert len(alerts) == 0


def test_kill_switch_alert(alert_sink: AlertSink) -> None:
    """Kill switch engagement triggers alert."""
    alert_sink.alert_kill_switch_engaged(note="manual halt")
    
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert len(alerts) == 1
    assert alerts[0]["event"] == "kill_switch_engaged"
    assert alerts[0]["details"]["note"] == "manual halt"


def test_daily_loss_alert(alert_sink: AlertSink) -> None:
    """Max daily loss latch triggers alert."""
    alert_sink.alert_daily_loss_latched("2026-09-18", 12500.0, 10000.0)
    
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert len(alerts) == 1
    assert alerts[0]["event"] == "max_daily_loss_latched"
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["details"]["trading_day"] == "2026-09-18"


def test_heartbeat_updates(tmp_path: Path) -> None:
    """Heartbeat file is updated with timestamp and PID."""
    heartbeat_file = tmp_path / "heartbeat.json"
    update_heartbeat(heartbeat_file)
    
    assert heartbeat_file.exists()
    heartbeat = json.loads(heartbeat_file.read_text())
    assert "timestamp" in heartbeat
    assert "pid" in heartbeat


def test_heartbeat_stale_detection(tmp_path: Path) -> None:
    """Stale heartbeat is detected."""
    heartbeat_file = tmp_path / "heartbeat.json"
    
    # Write old heartbeat
    heartbeat_file.write_text(
        json.dumps({"timestamp": "2026-09-18T12:00:00Z", "pid": 12345})
    )
    
    # Check with very short max age
    age = check_heartbeat_stale(heartbeat_file, max_age_seconds=1)
    assert age is not None
    assert age > 1


def test_heartbeat_fresh(tmp_path: Path) -> None:
    """Fresh heartbeat is not stale."""
    heartbeat_file = tmp_path / "heartbeat.json"
    update_heartbeat(heartbeat_file)
    
    # Check with reasonable max age
    age = check_heartbeat_stale(heartbeat_file, max_age_seconds=600)
    assert age is None


def test_disk_space_check_warns_on_low_space(tmp_path: Path) -> None:
    """Disk space check warns when free space is low."""
    # Can't easily simulate low disk space, but we can test the API
    ok, msg = check_disk_space(tmp_path, min_mb=999999999, strict=False)
    assert not ok
    assert "Low disk space" in msg


def test_disk_space_check_strict_mode_raises(tmp_path: Path) -> None:
    """Strict mode raises on low disk space."""
    with pytest.raises(StartupCheckError, match="Low disk space"):
        check_disk_space(tmp_path, min_mb=999999999, strict=True)


def test_log_file_size_check(tmp_path: Path) -> None:
    """Log file size check warns on large files."""
    log_file = tmp_path / "test.log"
    log_file.write_text("x" * (600 * 1024 * 1024))  # 600 MB
    
    ok, msg = check_log_file_size(log_file, max_mb=500, strict=False)
    assert not ok
    assert "Large log file" in msg
    assert "log rotation" in msg


def test_log_file_size_check_strict_mode_raises(tmp_path: Path) -> None:
    """Strict mode raises on large log file."""
    log_file = tmp_path / "test.log"
    log_file.write_text("x" * (600 * 1024 * 1024))
    
    with pytest.raises(StartupCheckError, match="Large log file"):
        check_log_file_size(log_file, max_mb=500, strict=True)


def test_log_file_size_ok_for_small_files(tmp_path: Path) -> None:
    """Small log files pass the check."""
    log_file = tmp_path / "test.log"
    log_file.write_text("small log\n")
    
    ok, msg = check_log_file_size(log_file, max_mb=500, strict=False)
    assert ok
    assert "OK" in msg


def test_alert_webhook_best_effort(alert_sink: AlertSink) -> None:
    """Webhook failures don't crash the harness."""
    # Set invalid webhook URL
    alert_sink.webhook_url = "http://invalid.local:99999/webhook"
    
    # Should not raise
    alert_sink.alert("info", "test_event", {})
    
    # Alert still written to file
    alerts = get_recent_alerts(alert_sink.alert_file, limit=10)
    assert len(alerts) == 1
