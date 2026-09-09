"""Deterministic cycle context: everything the agent sees, built from DB + broker.

No LLM output feeds back into this module. Cycle 200's context is assembled by
exactly the same code as cycle 1's, from a fixed number of rows, which is what
keeps the prompt bounded (SPEC.md constraint #4).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trader.broker import Broker, Clock
from trader.constants import MARKET_TZ
from trader.db import kill_switch_engaged, orders_since, utcnow

#: Hard caps on anything variable-length that reaches the prompt. These are the
#: only reason context stays flat over a long session.
#
#: Capping the *count* of theses is not enough on its own: a thesis rationale is
#: up to 800 chars and its invalidation condition is unbounded in the schema, so
#: 20 verbose theses were worth ~32k chars of prompt. Both are truncated here
#: as well. The full text is never lost — `get_theses` returns it untruncated,
#: which is what that tool is for.
MAX_RENDERED_POSITIONS = 15
MAX_RENDERED_THESES = 8
MAX_RENDERED_THESIS_RATIONALE = 160
MAX_RENDERED_THESIS_INVALIDATION = 100
MAX_RECENT_DECISIONS = 10

#: Hard ceiling on the whole rendered state, enforced at the bottom of
#: render_state. Sized from measured token counts, not guessed: with every
#: section at its cap this lands the first request near 4,000 tokens on prose
#: and under 5,000 even on adversarial input (a model writing 800 repeated
#: characters into a thesis tokenizes far worse than prose). Sized so the
#: per-section caps above come in
#: under it with room to spare — if this one ever fires, a new section grew
#: without a cap of its own. Anything past it is cut with a visible marker and
#: a logged warning: a degraded state block beats a failed cycle, and the token
#: assertion in trader.llm is still the backstop.
MAX_STATE_CHARS = 5_000

log = logging.getLogger("trader.state")


def trading_day_for(ts: datetime) -> str:
    """Exchange-local calendar date. All daily counters key off this."""
    return ts.astimezone(MARKET_TZ).date().isoformat()


@dataclass(frozen=True, slots=True)
class CycleContext:
    """Immutable snapshot handed to the agent and, later, to the risk layer."""

    cycle_id: int
    now: datetime
    trading_day: str
    clock: Clock
    account: dict[str, Any]
    positions: list[dict[str, Any]]
    theses: list[dict[str, Any]]
    recent_decisions: list[dict[str, Any]]
    orders_last_hour: int
    orders_today: int
    kill_switch: bool
    equity_delta_today: float | None
    previous_cycle: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def total_exposure(self) -> float:
        return sum(abs(p.get("market_value") or 0.0) for p in self.positions)

    def position_for(self, symbol: str) -> dict[str, Any] | None:
        return next((p for p in self.positions if p["symbol"] == symbol.upper()), None)


def _rows(cur: sqlite3.Cursor | list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in cur]


def _start_of_trading_day(now: datetime) -> datetime:
    local_midnight = now.astimezone(MARKET_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight


def build_cycle_context(
    conn: sqlite3.Connection,
    broker: Broker,
    *,
    cycle_id: int,
    now: datetime | None = None,
) -> CycleContext:
    """Assemble the full state for one cycle. Broker calls are allowed to raise."""
    now = now or utcnow()
    day = trading_day_for(now)

    clock = broker.get_clock()
    account = broker.get_account()
    positions = broker.get_positions()

    theses = _rows(
        conn.execute(
            "SELECT symbol, opened_at, rationale, invalidation_condition, status "
            "FROM theses WHERE status = 'open' ORDER BY opened_at DESC LIMIT ?",
            (MAX_RENDERED_THESES,),
        ).fetchall()
    )
    recent_decisions = _rows(
        conn.execute(
            "SELECT cycle_id, timestamp, action, symbol, qty, risk_result, outcome "
            "FROM decisions ORDER BY id DESC LIMIT ?",
            (MAX_RECENT_DECISIONS,),
        ).fetchall()
    )
    previous = conn.execute(
        "SELECT cycle_id, started_at, status, error FROM cycles "
        "WHERE cycle_id < ? AND status != 'running' ORDER BY cycle_id DESC LIMIT 1",
        (cycle_id,),
    ).fetchone()

    last_equity = account.get("last_equity")
    equity_delta = None if last_equity in (None, 0) else account["equity"] - last_equity

    return CycleContext(
        cycle_id=cycle_id,
        now=now,
        trading_day=day,
        clock=clock,
        account=account,
        positions=positions[:MAX_RENDERED_POSITIONS],
        theses=theses,
        recent_decisions=list(reversed(recent_decisions)),
        orders_last_hour=orders_since(conn, now - timedelta(hours=1)),
        orders_today=orders_since(conn, _start_of_trading_day(now)),
        kill_switch=kill_switch_engaged(conn),
        equity_delta_today=equity_delta,
        previous_cycle=dict(previous) if previous else None,
        notes=(
            [f"{len(positions) - MAX_RENDERED_POSITIONS} further positions not rendered"]
            if len(positions) > MAX_RENDERED_POSITIONS
            else []
        ),
    )


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _clip(text: str | None, limit: int) -> str:
    """Truncate for display only. Storage and get_theses keep the full text."""
    value = (text or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _render_decision(d: dict[str, Any]) -> str:
    parts = [f"- [{d['timestamp']}] cycle {d['cycle_id']}: {d['action']}"]
    if d.get("symbol"):
        parts.append(str(d["symbol"]))
    if d.get("qty") is not None:
        parts.append(f"qty={float(d['qty']):g}")
    parts.append(f"-> {d.get('risk_result') or 'n/a'}")
    if d.get("outcome"):
        parts.append(f"({d['outcome']})")
    return " ".join(parts)


def render_state(ctx: CycleContext) -> str:
    """Fixed-shape plain-text rendering. This becomes the user turn in Phase 3."""
    local = ctx.now.astimezone(MARKET_TZ)
    lines: list[str] = [
        "## Cycle",
        f"cycle_id: {ctx.cycle_id}",
        f"time: {local:%Y-%m-%d %H:%M:%S %Z} (trading day {ctx.trading_day})",
        f"market_open: {ctx.clock.is_open}",
        f"next_close: {ctx.clock.next_close.astimezone(MARKET_TZ):%Y-%m-%d %H:%M %Z}",
        f"kill_switch: {'ENGAGED' if ctx.kill_switch else 'off'}",
        "",
        "## Account",
        f"equity: {_money(ctx.account.get('equity'))}",
        f"cash: {_money(ctx.account.get('cash'))}",
        f"buying_power: {_money(ctx.account.get('buying_power'))}",
        f"equity_change_since_prior_close: {_money(ctx.equity_delta_today)}",
        f"total_exposure: {_money(ctx.total_exposure)}",
        "",
        "## Positions",
    ]
    if not ctx.positions:
        lines.append("(none)")
    else:
        lines.append("symbol | qty | avg_price | last | market_value | unrealized_pl")
        lines += [
            f"{p['symbol']} | {p['qty']:g} | {_money(p['avg_price'])} | "
            f"{_money(p.get('current_price'))} | {_money(p.get('market_value'))} | "
            f"{_money(p.get('unrealized_pl'))}"
            for p in ctx.positions
        ]

    lines += ["", "## Open theses"]
    if not ctx.theses:
        lines.append("(none)")
    else:
        lines += [
            f"- {t['symbol']} (opened {t['opened_at']}): "
            f"{_clip(t['rationale'], MAX_RENDERED_THESIS_RATIONALE)}\n"
            f"  invalidated_if: "
            f"{_clip(t['invalidation_condition'], MAX_RENDERED_THESIS_INVALIDATION)}"
            for t in ctx.theses
        ]
        lines.append("(theses are abbreviated here; call get_theses for the full text)")

    lines += ["", f"## Recent decisions (last {MAX_RECENT_DECISIONS})"]
    if not ctx.recent_decisions:
        lines.append("(none)")
    else:
        lines += [_render_decision(d) for d in ctx.recent_decisions]

    lines += [
        "",
        "## Rate usage",
        f"orders_last_hour: {ctx.orders_last_hour}",
        f"orders_today: {ctx.orders_today}",
    ]
    if ctx.previous_cycle:
        lines += [
            "",
            "## Previous cycle",
            f"cycle {ctx.previous_cycle['cycle_id']} at {ctx.previous_cycle['started_at']}: "
            f"{ctx.previous_cycle['status']}"
            + (f" ({ctx.previous_cycle['error']})" if ctx.previous_cycle["error"] else ""),
        ]
    if ctx.notes:
        lines += ["", "## Notes", *[f"- {n}" for n in ctx.notes]]

    rendered = "\n".join(lines)
    if len(rendered) > MAX_STATE_CHARS:
        # Should be unreachable given the per-section caps above. If it fires,
        # a new section grew without a cap — say so loudly rather than quietly
        # spending the prompt budget.
        log.warning(
            "state_render_truncated",
            extra={"cycle_id": ctx.cycle_id, "chars": len(rendered), "cap": MAX_STATE_CHARS},
        )
        marker = "\n\n[state truncated to fit the context budget]"
        rendered = rendered[: MAX_STATE_CHARS - len(marker)] + marker
    return rendered
