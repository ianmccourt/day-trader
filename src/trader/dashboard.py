"""Local control panel for the trading loop.

stdlib only: a ThreadingHTTPServer bound to loopback, plus a PID file so this
process can start and stop `trader run` without being the loop itself. The
dashboard never submits orders — start/stop, kill switch, and read-only views.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import webbrowser
from contextlib import suppress
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from trader.brokers import broker_endpoint, make_broker
from trader.config import MissingCredential, Settings
from trader.evaluate import EvaluationError, evaluate
from trader.constants import BROKER_PAPER, MARKET_TZ
from trader.db import (
    KILL_SWITCH,
    connect,
    current_positions,
    cycles_on,
    kill_switch_engaged,
    last_cycle,
    set_flag,
    unreconciled_orders,
    utcnow,
)
from trader.risk.checks import CHECK_NAMES
from trader.risk.config import RiskConfig, RiskConfigError, load_risk_config
from trader.scheduler import rth_trigger
from trader.state import trading_day_for

PAGE = Path(__file__).with_name("dashboard.html")
DEFAULT_PID_PATH = Path("logs/trader.pid")
DEFAULT_LOG_PATH = Path("logs/trader.jsonl")

_OMIT = "<omitted>"


class LoopProcess:
    """Owns the `trader run` child via a PID file. The dashboard is not the loop."""

    def __init__(
        self,
        pid_path: Path = DEFAULT_PID_PATH,
        log_path: Path = DEFAULT_LOG_PATH,
    ) -> None:
        self.pid_path = pid_path
        self.log_path = log_path
        self._proc: subprocess.Popen[bytes] | None = None

    def pid(self) -> int | None:
        if not self.pid_path.is_file():
            return None
        raw = self.pid_path.read_text(encoding="utf-8").strip()
        if not raw.isdigit():
            return None
        return int(raw)

    def running(self) -> bool:
        pid = self.pid()
        return pid is not None and _pid_alive(pid)

    def start(self, argv: list[str], *, command: list[str] | None = None) -> dict[str, Any]:
        if self.running():
            return {"ok": False, "running": True, "message": "loop is already running"}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = command or [sys.executable, "-m", "trader.cli", *argv, "run"]
        with self.log_path.open("a", encoding="utf-8") as log_f:
            proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=os.getcwd(),
            )
        self.pid_path.write_text(str(proc.pid), encoding="utf-8")
        self._proc = proc
        time.sleep(0.25)
        if proc.poll() is not None:
            return {
                "ok": False,
                "running": False,
                "message": (
                    f"loop exited immediately (code {proc.returncode}). "
                    "Check the log for the reason."
                ),
            }
        return {"ok": True, "running": True, "pid": proc.pid, "message": "loop started"}

    def stop(self) -> dict[str, Any]:
        pid = self.pid()
        if pid is None or not _pid_alive(pid):
            self._clear_pid()
            return {"ok": True, "running": False, "message": "already stopped"}
        os.kill(pid, signal.SIGTERM)
        with suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                self._clear_pid()
                return {"ok": True, "running": False, "message": "loop stopped"}
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            self._clear_pid()
            return {"ok": True, "running": False, "message": "loop stopped"}
        time.sleep(0.1)
        if _pid_alive(pid):
            return {
                "ok": False,
                "running": True,
                "message": "process did not exit after SIGTERM; stop it from the shell",
            }
        self._clear_pid()
        return {"ok": True, "running": False, "message": "loop stopped"}

    def _clear_pid(self) -> None:
        self._proc = None
        self.pid_path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """True if `pid` is a live process. Reaps a child we spawned so zombies
    do not look alive — Unix `kill(pid, 0)` succeeds on a zombie.
    """
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tail_log(path: Path, n: int = 60) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 65_536))
        text = fh.read().decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()][-n:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            out.append({"msg": line, "level": "INFO"})
            continue
        if not isinstance(row, dict):
            out.append({"msg": line, "level": "INFO"})
            continue
        out.append(row)
    return out


def next_scheduled_cycle(rows: list[dict[str, Any]]) -> str | None:
    """Newest `scheduler_start.next_cycle`, or None after a stop.

    Only the first fire after process start is in that log line, so the
    snapshot prefers `upcoming_cycle_iso` (wall-clock) over this.
    """
    for row in reversed(rows):
        msg = row.get("msg")
        if msg == "scheduler_stopped":
            return None
        if msg == "scheduler_start":
            nxt = row.get("next_cycle")
            return str(nxt) if nxt else None
    return None


def equity_daily(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Last equity snapshot per trading day — the curve the chart draws."""
    rows = conn.execute(
        "SELECT c.trading_day, a.equity, a.captured_at "
        "FROM account_snapshot a JOIN cycles c USING (cycle_id) "
        "WHERE a.id IN ("
        "  SELECT MAX(a2.id) FROM account_snapshot a2 "
        "  JOIN cycles c2 USING (cycle_id) GROUP BY c2.trading_day"
        ") ORDER BY c.trading_day"
    ).fetchall()
    return [dict(r) for r in rows]


def equity_recent(conn: sqlite3.Connection, *, limit: int = 120) -> list[dict[str, Any]]:
    """Recent cycle-level equity for intraday detail when history is short."""
    rows = conn.execute(
        "SELECT c.trading_day, a.equity, a.captured_at, c.cycle_id "
        "FROM account_snapshot a JOIN cycles c USING (cycle_id) "
        "ORDER BY a.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def model_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT model, COUNT(*) AS cycles, "
        "AVG(duration_ms) AS avg_duration_ms, "
        "SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens, "
        "SUM(COALESCE(completion_tokens, 0)) AS completion_tokens, "
        "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors "
        "FROM cycles WHERE model IS NOT NULL AND TRIM(model) != '' "
        "GROUP BY model ORDER BY cycles DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def build_performance(
    conn: sqlite3.Connection,
    settings: Settings,
) -> dict[str, Any]:
    """Evaluate over the full logged span. Broker optional (SPY benchmark)."""
    bounds = conn.execute(
        "SELECT MIN(trading_day) AS start_day, MAX(trading_day) AS end_day FROM cycles"
    ).fetchone()
    if bounds is None or bounds["start_day"] is None:
        return {"available": False, "reason": "no cycles logged yet"}
    start = str(bounds["start_day"])
    end = str(bounds["end_day"])
    window = {"start": start, "end": end}

    broker = None
    if settings.alpaca_api_key and settings.alpaca_secret_key:
        from trader.broker import AlpacaBroker

        broker = AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key)

    try:
        report = evaluate(conn, start, end, broker=broker)
    except EvaluationError as exc:
        return {"available": False, "reason": str(exc), "window": window}

    data = report.to_dict()
    trips = data.pop("round_trips", [])
    data["recent_round_trips"] = trips[-10:]
    data["round_trip_count"] = len(trips)
    data["available"] = True
    data["window"] = window
    return data


def upcoming_cycle_iso(cycle_minutes: int, *, running: bool, now: datetime | None = None) -> str | None:
    """Next RTH grid fire from *now*, not from the process-start log line."""
    if not running:
        return None
    current = (now or utcnow()).astimezone(MARKET_TZ)
    nxt = rth_trigger(cycle_minutes).get_next_fire_time(None, current)
    return nxt.isoformat() if nxt else None


def session_snapshot(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    loop: LoopProcess,
    risk: RiskConfig | None,
    risk_error: str | None,
    risk_source: str,
) -> dict[str, Any]:
    """Everything the panel paints. DB + PID file only — no broker, no secrets."""
    day = trading_day_for(utcnow())
    rows = cycles_on(conn, day)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    last = last_cycle(conn)
    decisions = conn.execute(
        "SELECT action, COUNT(*) AS n FROM decisions d "
        "JOIN cycles c USING (cycle_id) WHERE c.trading_day = ? GROUP BY action",
        (day,),
    ).fetchall()
    pos_cycle_id, pos_captured_at, positions = current_positions(conn)
    account = conn.execute(
        "SELECT equity, last_equity, cash, buying_power, captured_at "
        "FROM account_snapshot ORDER BY cycle_id DESC LIMIT 1"
    ).fetchone()
    last_decision = conn.execute(
        "SELECT cycle_id, timestamp, action, symbol, qty, reasoning, "
        "risk_result, outcome FROM decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    recent_cycles = conn.execute(
        "SELECT cycle_id, started_at, trading_day, status, duration_ms, "
        "prompt_tokens, completion_tokens, error, model "
        "FROM cycles ORDER BY cycle_id DESC LIMIT 40"
    ).fetchall()
    recent_decisions = conn.execute(
        "SELECT cycle_id, timestamp, action, symbol, qty, risk_result, outcome, "
        "broker_order_id, reference_price "
        "FROM decisions ORDER BY id DESC LIMIT 25"
    ).fetchall()
    rejection_breakdown = conn.execute(
        "SELECT check_name, COUNT(*) AS n FROM risk_events "
        "GROUP BY check_name ORDER BY n DESC"
    ).fetchall()
    recent_rejections = conn.execute(
        "SELECT timestamp, cycle_id, check_name, reason FROM risk_events "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    flag = conn.execute(
        "SELECT value, note, updated_at FROM flags WHERE key = ?", (KILL_SWITCH,)
    ).fetchone()
    pending = unreconciled_orders(conn)
    last_payload = None
    if last is not None:
        last_payload = {
            k: last[k]
            for k in (
                "cycle_id",
                "started_at",
                "ended_at",
                "trading_day",
                "status",
                "model",
                "prompt_tokens",
                "completion_tokens",
                "duration_ms",
                "error",
            )
        }

    risk_payload: dict[str, Any] | None = None
    if risk is not None:
        risk_payload = {
            "source": risk.source,
            "max_position_notional": risk.max_position_notional,
            "max_total_exposure": risk.max_total_exposure,
            "max_daily_loss": risk.max_daily_loss,
            "max_orders_per_hour": risk.max_orders_per_hour,
            "max_orders_per_day": risk.max_orders_per_day,
            "max_orders_per_cycle": risk.max_orders_per_cycle,
            "symbol_allowlist": sorted(risk.symbol_allowlist),
            "regular_trading_hours_only": risk.regular_trading_hours_only,
            "allow_shorts": risk.allow_shorts,
            "checks": list(CHECK_NAMES),
        }

    log_rows = tail_log(loop.log_path)
    running = loop.running()
    perf = build_performance(conn, settings)
    return {
        "paper": settings.broker == BROKER_PAPER,
        "broker": settings.broker,
        "broker_base_url": broker_endpoint(settings),
        "db": str(settings.db_path),
        "cycle_minutes": settings.cycle_minutes,
        "trading_day": day,
        "now": utcnow().isoformat(),
        "loop": {
            "running": running,
            "pid": loop.pid() if running else None,
            "log_path": str(loop.log_path),
            "next_cycle": upcoming_cycle_iso(settings.cycle_minutes, running=running),
        },
        "kill_switch": kill_switch_engaged(conn),
        "kill_note": None if flag is None else flag["note"],
        "kill_updated_at": None if flag is None else flag["updated_at"],
        "cycles_today": len(rows),
        "cycles_by_status": by_status,
        "decisions_today": {d["action"]: d["n"] for d in decisions},
        "unreconciled_orders": len(pending),
        "account": dict(account) if account else None,
        "last_cycle": last_payload,
        "positions": [dict(p) for p in positions],
        "positions_as_of_cycle_id": pos_cycle_id,
        "positions_captured_at": pos_captured_at,
        "last_decision": dict(last_decision) if last_decision else None,
        "cycles": [dict(r) for r in recent_cycles],
        "decisions": [dict(r) for r in recent_decisions],
        "rejection_breakdown": [dict(r) for r in rejection_breakdown],
        "rejections": [dict(r) for r in recent_rejections],
        "risk": risk_payload,
        "risk_error": risk_error,
        "risk_source": risk_source,
        "log": log_rows,
        "performance": perf,
        "equity_daily": equity_daily(conn),
        "equity_recent": equity_recent(conn),
        "model_stats": model_stats(conn),
    }


def cycle_detail(conn: sqlite3.Connection, cycle_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["full_prompt"] = _OMIT
    return d


def _json_bytes(payload: Any, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, default=str).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


class DashboardApp:
    def __init__(
        self,
        settings: Settings,
        *,
        loop: LoopProcess,
        risk_config_path: Path,
        run_argv: list[str],
    ) -> None:
        self.settings = settings
        self.loop = loop
        self.risk_config_path = risk_config_path
        self.run_argv = run_argv

    def _risk(self) -> tuple[RiskConfig | None, str | None]:
        try:
            return load_risk_config(self.risk_config_path), None
        except RiskConfigError as exc:
            return None, str(exc)

    def snapshot(self) -> dict[str, Any]:
        risk, err = self._risk()
        conn = connect(self.settings.db_path)
        try:
            return session_snapshot(
                conn,
                self.settings,
                loop=self.loop,
                risk=risk,
                risk_error=err,
                risk_source=str(self.risk_config_path),
            )
        finally:
            conn.close()

    def dispatch(
        self, method: str, path: str, body: dict[str, Any]
    ) -> tuple[int, bytes, str]:
        if method == "GET" and path == "/":
            return 200, PAGE.read_bytes(), "text/html; charset=utf-8"
        if method == "GET" and path == "/api/snapshot":
            return _json_bytes(self.snapshot())
        if method == "GET" and path.startswith("/api/cycles/"):
            raw = path.rsplit("/", 1)[-1]
            if not raw.isdigit():
                return _json_bytes({"error": "cycle_id must be an integer"}, 400)
            conn = connect(self.settings.db_path)
            try:
                detail = cycle_detail(conn, int(raw))
            finally:
                conn.close()
            if detail is None:
                return _json_bytes({"error": f"no such cycle: {raw}"}, 404)
            return _json_bytes(detail)
        if method == "POST" and path == "/api/loop":
            action = str(body.get("action", "")).strip().lower()
            if action == "start":
                risk, err = self._risk()
                if risk is None:
                    return _json_bytes({"ok": False, "message": err}, 400)
                started = self.loop.start(self.run_argv)
                started["broker"] = self.settings.broker
                started["paper"] = self.settings.broker == BROKER_PAPER
                return _json_bytes(started, 200)
            if action == "stop":
                return _json_bytes(self.loop.stop())
            return _json_bytes({"error": "action must be start or stop"}, 400)
        if method == "POST" and path == "/api/kill":
            state = str(body.get("state", "")).strip().lower()
            if state not in {"on", "off"}:
                return _json_bytes({"error": "state must be on or off"}, 400)
            note = body.get("note")
            conn = connect(self.settings.db_path)
            try:
                set_flag(
                    conn,
                    KILL_SWITCH,
                    "1" if state == "on" else "0",
                    note=str(note) if note else None,
                )
                engaged = kill_switch_engaged(conn)
            finally:
                conn.close()
            return _json_bytes({"ok": True, "kill_switch": engaged})
        if method == "POST" and path == "/api/reconcile":
            return self._reconcile()
        return _json_bytes({"error": "not found"}, 404)

    def _reconcile(self) -> tuple[int, bytes, str]:
        from trader.execution import reconcile_fills

        try:
            broker = make_broker(self.settings)
        except MissingCredential as exc:
            return _json_bytes({"ok": False, "message": str(exc)}, 400)
        conn = connect(self.settings.db_path)
        try:
            results = reconcile_fills(conn, broker)
            done = sum(1 for e in results if e["status"] == "reconciled")
            skipped = sum(1 for e in results if e["status"] == "open")
            fills = [
                {
                    "symbol": e["symbol"],
                    "status": e.get("final_status"),
                    "filled_qty": e.get("filled_qty"),
                    "filled_avg_price": e.get("filled_avg_price"),
                }
                for e in results
                if e["status"] == "reconciled"
            ]
        finally:
            conn.close()
        return _json_bytes(
            {"ok": True, "reconciled": done, "still_open": skipped, "fills": fills}
        )


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("JSON body must be an object")
    return parsed


def make_handler(app: DashboardApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path != "/" and path != "/api/snapshot" and not path.startswith("/api/cycles/"):
                self._send(*_json_bytes({"error": "not found"}, 404))
                return
            self._send(*app.dispatch("GET", path, {}))

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                body = _read_json(self)
            except (json.JSONDecodeError, ValueError) as exc:
                self._send(*_json_bytes({"error": f"invalid JSON: {exc}"}, 400))
                return
            self._send(*app.dispatch("POST", parsed.path, body))

    return Handler


def serve(
    app: DashboardApp,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"warning: binding to {host} exposes start/stop/kill on that interface",
            file=sys.stderr,
        )
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}/"
    label = "PAPER" if app.settings.broker == BROKER_PAPER else "Robinhood Agentic"
    print(f"dashboard on {url}  ({label}, loopback control panel)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped")
    finally:
        httpd.server_close()
