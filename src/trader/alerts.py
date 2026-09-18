"""Minimal alerting: write to file and optional webhook.

Fires on:
- Consecutive error cycles (threshold configured)
- Kill switch engaged
- Max daily loss latched
- Heartbeat stale (if heartbeat tracking enabled)

Integrated into existing cycle end / kill / daily-loss paths.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from trader.db import iso, utcnow


#: Where alerts are logged
DEFAULT_ALERT_FILE = Path("data/alerts.jsonl")

#: Consecutive error cycle threshold
DEFAULT_ERROR_THRESHOLD = 3

#: Webhook timeout
WEBHOOK_TIMEOUT_SECONDS = 5


class AlertSink:
    """Minimal alert sink: file + optional webhook."""
    
    def __init__(
        self,
        alert_file: Path = DEFAULT_ALERT_FILE,
        webhook_url: str | None = None,
        error_threshold: int = DEFAULT_ERROR_THRESHOLD,
    ):
        self.alert_file = alert_file
        self.webhook_url = webhook_url or os.environ.get("TRADER_ALERT_WEBHOOK")
        self.error_threshold = error_threshold
        
        # Ensure alert file directory exists
        if self.alert_file:
            self.alert_file.parent.mkdir(parents=True, exist_ok=True)
    
    def alert(self, severity: str, event: str, details: dict[str, Any] | None = None) -> None:
        """Fire an alert: write to file and optionally POST to webhook."""
        alert = {
            "timestamp": iso(utcnow()),
            "severity": severity,
            "event": event,
            "details": details or {},
        }
        
        # Write to file
        if self.alert_file:
            with self.alert_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(alert) + "\n")
        
        # POST to webhook if configured
        if self.webhook_url:
            self._send_webhook(alert)
    
    def _send_webhook(self, alert: dict[str, Any]) -> None:
        """Send alert to webhook (best-effort, never raises)."""
        if not HAS_REQUESTS:
            return
        
        try:
            requests.post(
                self.webhook_url,
                json=alert,
                timeout=WEBHOOK_TIMEOUT_SECONDS,
            )
        except Exception:
            # Best-effort: webhook failures don't stop the harness
            pass
    
    def check_consecutive_errors(self, conn: sqlite3.Connection) -> None:
        """Fire alert if error cycles exceed threshold."""
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
        """Fire alert when kill switch is engaged."""
        self.alert(
            "warning",
            "kill_switch_engaged",
            {"note": note} if note else {},
        )
    
    def alert_daily_loss_latched(self, trading_day: str, loss: float, threshold: float) -> None:
        """Fire alert when max daily loss is latched."""
        self.alert(
            "critical",
            "max_daily_loss_latched",
            {
                "trading_day": trading_day,
                "loss": loss,
                "threshold": threshold,
            },
        )
    
    def alert_heartbeat_stale(self, heartbeat_age_seconds: float) -> None:
        """Fire alert when heartbeat file is stale."""
        self.alert(
            "critical",
            "heartbeat_stale",
            {"age_seconds": heartbeat_age_seconds},
        )


def get_recent_alerts(alert_file: Path = DEFAULT_ALERT_FILE, limit: int = 20) -> list[dict[str, Any]]:
    """Read recent alerts from the log file."""
    if not alert_file.exists():
        return []
    
    alerts: list[dict[str, Any]] = []
    with alert_file.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                alerts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    
    return alerts[-limit:]


def update_heartbeat(heartbeat_file: Path = Path("data/heartbeat.json")) -> None:
    """Update heartbeat timestamp (called at cycle end)."""
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat = {
        "timestamp": iso(utcnow()),
        "pid": os.getpid(),
    }
    heartbeat_file.write_text(json.dumps(heartbeat, indent=2), encoding="utf-8")


def check_heartbeat_stale(
    heartbeat_file: Path = Path("data/heartbeat.json"),
    max_age_seconds: float = 600,
) -> float | None:
    """Check if heartbeat is stale. Returns age in seconds if stale, None otherwise."""
    if not heartbeat_file.exists():
        return None
    
    try:
        heartbeat = json.loads(heartbeat_file.read_text(encoding="utf-8"))
        heartbeat_time = datetime.fromisoformat(heartbeat["timestamp"])
        age = (utcnow() - heartbeat_time).total_seconds()
        
        if age > max_age_seconds:
            return age
    except (json.JSONDecodeError, KeyError, ValueError):
        pass
    
    return None
