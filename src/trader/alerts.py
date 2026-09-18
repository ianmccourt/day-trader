"""Minimal alerting: append-only file plus an optional webhook.

Fires on consecutive error cycles, kill-switch engagement, a latched daily
loss, and (via the watchdog) a stale heartbeat. Webhook delivery is
best-effort and never raises — an alert that cannot page still has to land
in the log.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.db import iso, utcnow

DEFAULT_ALERT_FILE = Path("data/alerts.jsonl")
DEFAULT_HEARTBEAT_FILE = Path("data/heartbeat.json")
DEFAULT_ERROR_THRESHOLD = 3
WEBHOOK_TIMEOUT_SECONDS = 5


class AlertSink:
    """File sink with an optional webhook POST."""

    def __init__(
        self,
        alert_file: Path = DEFAULT_ALERT_FILE,
        webhook_url: str | None = None,
        error_threshold: int = DEFAULT_ERROR_THRESHOLD,
    ) -> None:
        self.alert_file = alert_file
        self.webhook_url = webhook_url or os.environ.get("TRADER_ALERT_WEBHOOK")
        self.error_threshold = error_threshold
        if self.alert_file:
            self.alert_file.parent.mkdir(parents=True, exist_ok=True)

    def alert(self, severity: str, event: str, details: dict[str, Any] | None = None) -> None:
        payload = {
            "timestamp": iso(utcnow()),
            "severity": severity,
            "event": event,
            "details": details or {},
        }
        if self.alert_file:
            with self.alert_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
        if self.webhook_url:
            self._send_webhook(payload)

    def _send_webhook(self, alert: dict[str, Any]) -> None:
        try:
            request = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(alert).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            pass

    def check_consecutive_errors(self, conn: sqlite3.Connection) -> None:
        recent = conn.execute(
            "SELECT status FROM cycles ORDER BY cycle_id DESC LIMIT ?",
            (self.error_threshold,),
        ).fetchall()
        if len(recent) < self.error_threshold:
            return
        if all(row["status"] == "error" for row in recent):
            self.alert(
                "critical",
                "consecutive_error_cycles",
                {"count": self.error_threshold, "threshold": self.error_threshold},
            )

    def alert_kill_switch_engaged(self, note: str | None = None) -> None:
        self.alert("warning", "kill_switch_engaged", {"note": note} if note else {})

    def alert_daily_loss_latched(self, trading_day: str, loss: float, threshold: float) -> None:
        self.alert(
            "critical",
            "max_daily_loss_latched",
            {"trading_day": trading_day, "loss": loss, "threshold": threshold},
        )

    def alert_heartbeat_stale(self, heartbeat_age_seconds: float) -> None:
        self.alert("critical", "heartbeat_stale", {"age_seconds": heartbeat_age_seconds})


def get_recent_alerts(
    alert_file: Path = DEFAULT_ALERT_FILE, limit: int = 20
) -> list[dict[str, Any]]:
    if not alert_file.exists():
        return []
    alerts: list[dict[str, Any]] = []
    with alert_file.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                alerts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return alerts[-limit:]


def update_heartbeat(heartbeat_file: Path = DEFAULT_HEARTBEAT_FILE) -> None:
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_file.write_text(
        json.dumps({"timestamp": iso(utcnow()), "pid": os.getpid()}, indent=2),
        encoding="utf-8",
    )


def check_heartbeat_stale(
    heartbeat_file: Path = DEFAULT_HEARTBEAT_FILE,
    max_age_seconds: float = 600,
) -> float | None:
    if not heartbeat_file.exists():
        return None
    try:
        heartbeat = json.loads(heartbeat_file.read_text(encoding="utf-8"))
        heartbeat_time = datetime.fromisoformat(heartbeat["timestamp"])
        age = (utcnow() - heartbeat_time).total_seconds()
        if age > max_age_seconds:
            return age
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return None
