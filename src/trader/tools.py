"""Tool definitions and dispatch for the agent.

SPEC.md constraint #2: exactly one tool writes anything. `place_order` is that
tool, and it delegates to `execution.place_order`, which runs the risk layer
unconditionally. Every other tool here is a read: no shell, no arbitrary HTTP,
no DB writes outside the helpers in `trader.db`.

Tool *results* are capped in size. They land in the next request's message
array, so an uncapped result is a slow leak in the context budget.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trader.broker import BAR_TIMEFRAMES, MAX_BARS, Broker, BrokerError
from trader.constants import MAX_THESIS_RATIONALE_CHARS
from trader.execution import build_proposal, place_order
from trader.risk.checks import CHECK_NAMES
from trader.risk.config import RiskConfig
from trader.state import CycleContext

log = logging.getLogger("trader.tools")

WRITE_TOOL = "place_order"

MAX_DECISIONS_RETURNED = 20
MAX_QUOTE_SYMBOLS = 5


class ToolError(RuntimeError):
    """A tool call the model got wrong. Returned to it as an error result."""


@dataclass
class ToolContext:
    """Everything the tool implementations are allowed to touch."""

    conn: sqlite3.Connection
    broker: Broker
    ctx: CycleContext
    config: RiskConfig
    #: Set once `place_order` has been called. The spec allows at most one
    #: write proposal per cycle, and this is what enforces it.
    order_attempted: bool = False
    executions: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.executions is None:
            self.executions = []


# --- read tools ------------------------------------------------------------


def _get_risk_limits(tc: ToolContext, _args: dict[str, Any]) -> dict[str, Any]:
    c = tc.config
    return {
        "max_position_notional": c.max_position_notional,
        "max_total_exposure": c.max_total_exposure,
        "max_daily_loss": c.max_daily_loss,
        "max_orders_per_hour": c.max_orders_per_hour,
        "max_orders_per_day": c.max_orders_per_day,
        "symbol_allowlist": sorted(c.symbol_allowlist),
        "regular_trading_hours_only": c.regular_trading_hours_only,
        "allow_shorts": c.allow_shorts,
        "checks_that_will_run": list(CHECK_NAMES),
        "orders_used_this_hour": tc.ctx.orders_last_hour,
        "orders_used_today": tc.ctx.orders_today,
    }


def _get_quote(tc: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    symbols = args.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        raise ToolError("symbols must be a non-empty list of ticker strings")
    if len(symbols) > MAX_QUOTE_SYMBOLS:
        raise ToolError(f"at most {MAX_QUOTE_SYMBOLS} symbols per call, got {len(symbols)}")
    out: dict[str, Any] = {}
    for raw in symbols:
        symbol = str(raw).strip().upper()
        try:
            out[symbol] = tc.broker.get_latest_price(symbol)
        except BrokerError as exc:
            # One bad ticker must not fail the whole call; the model can read
            # the error and move on.
            out[symbol] = f"error: {exc}"
    return out


def _get_bars(tc: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    symbol = str(args.get("symbol", "")).strip().upper()
    timeframe = str(args.get("timeframe", "1Day"))
    limit = int(args.get("limit", 10))
    if not symbol:
        raise ToolError("symbol is required")
    if timeframe not in BAR_TIMEFRAMES:
        raise ToolError(f"timeframe must be one of {sorted(BAR_TIMEFRAMES)}")
    return {"symbol": symbol, "timeframe": timeframe, "bars": tc.broker.get_bars(
        symbol, timeframe=timeframe, limit=limit
    )}


def _get_theses(tc: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Full stored theses. The rendered state truncates; this does not."""
    symbol = args.get("symbol")
    if symbol:
        rows = tc.conn.execute(
            "SELECT symbol, opened_at, updated_at, rationale, invalidation_condition, status "
            "FROM theses WHERE symbol = ? ORDER BY opened_at DESC LIMIT 5",
            (str(symbol).strip().upper(),),
        ).fetchall()
    else:
        rows = tc.conn.execute(
            "SELECT symbol, opened_at, updated_at, rationale, invalidation_condition, status "
            "FROM theses WHERE status = 'open' ORDER BY opened_at DESC LIMIT 20"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_recent_decisions(tc: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    limit = max(1, min(int(args.get("limit", 10)), MAX_DECISIONS_RETURNED))
    rows = tc.conn.execute(
        "SELECT cycle_id, timestamp, action, symbol, qty, risk_result, outcome "
        "FROM decisions ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# --- the one write tool ----------------------------------------------------


def _place_order(tc: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """The only tool that changes anything. Routes through the risk layer."""
    if tc.order_attempted:
        # The spec allows at most one write proposal per cycle. Refused as a
        # tool result rather than an exception, so the model can wrap up.
        raise ToolError(
            "you have already proposed an order this cycle; at most one is allowed. "
            "End your turn."
        )
    tc.order_attempted = True

    action = str(args.get("action", "")).strip().lower()
    symbol = str(args.get("symbol", "")).strip().upper()
    reasoning = str(args.get("reasoning", "")).strip()
    invalidation = str(args.get("invalidation_condition", "")).strip()
    try:
        qty = float(args.get("qty"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ToolError(f"qty must be a number, got {args.get('qty')!r}") from None

    try:
        proposal = build_proposal(
            tc.broker, action=action, symbol=symbol, qty=qty, reasoning=reasoning
        )
    except BrokerError as exc:
        raise ToolError(f"could not price {symbol}: {exc}") from exc

    result = place_order(tc.conn, tc.broker, tc.ctx, proposal, tc.config)
    tc.executions.append(result)

    if result.executed:
        _record_thesis(tc, proposal.symbol, action, reasoning, invalidation)

    return {
        "approved": result.verdict.approved,
        "executed": result.executed,
        "message": result.as_model_message(),
        "reference_price": proposal.reference_price,
        "notional": round(proposal.notional, 2),
        "failed_checks": [f.check for f in result.verdict.failures],
    }


def _record_thesis(
    tc: ToolContext, symbol: str, action: str, reasoning: str, invalidation: str
) -> None:
    """Thesis writes ride along with an executed order.

    Deliberately not a second write tool: SPEC.md allows exactly one, and a
    thesis with no position behind it is not worth storing.
    """
    rationale = (reasoning or "(none given)")[:MAX_THESIS_RATIONALE_CHARS]
    condition = (invalidation or "(none given)")[:MAX_THESIS_RATIONALE_CHARS]
    existing = tc.conn.execute(
        "SELECT id FROM theses WHERE symbol = ? AND status = 'open'", (symbol,)
    ).fetchone()

    if action == "sell":
        position = tc.ctx.position_for(symbol)
        # Only close the thesis when the position is actually flat afterwards.
        remaining = (position["qty"] if position else 0.0) - tc.executions[-1].proposal.qty
        if existing and remaining <= 0:
            tc.conn.execute(
                "UPDATE theses SET status = 'closed', closed_at = datetime('now'), "
                "updated_at = datetime('now') WHERE id = ?",
                (existing["id"],),
            )
        return

    if existing:
        tc.conn.execute(
            "UPDATE theses SET rationale = ?, invalidation_condition = ?, "
            "updated_at = datetime('now'), cycle_id = ? WHERE id = ?",
            (rationale, condition, tc.ctx.cycle_id, existing["id"]),
        )
    else:
        tc.conn.execute(
            "INSERT INTO theses(symbol, opened_at, updated_at, cycle_id, rationale, "
            "invalidation_condition, status) "
            "VALUES (?, datetime('now'), datetime('now'), ?, ?, ?, 'open')",
            (symbol, tc.ctx.cycle_id, rationale, condition),
        )


# --- registry --------------------------------------------------------------

ToolFn = Callable[[ToolContext, dict[str, Any]], Any]

#: Read tools are listed first so the model reads before it writes, and the
#: order is fixed so the serialised tool block stays cacheable.
TOOL_IMPLS: dict[str, ToolFn] = {
    "get_risk_limits": _get_risk_limits,
    "get_quote": _get_quote,
    "get_bars": _get_bars,
    "get_theses": _get_theses,
    "get_recent_decisions": _get_recent_decisions,
    WRITE_TOOL: _place_order,
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_risk_limits",
        "description": (
            "Read the risk limits your orders will be checked against, which checks "
            "will run, and how much of the order rate budget is already used."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_quote",
        "description": "Latest trade price for up to 5 symbols.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_QUOTE_SYMBOLS,
                    "description": "Ticker symbols, e.g. [\"AAPL\", \"SPY\"].",
                }
            },
            "required": ["symbols"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_bars",
        "description": "Recent OHLCV price bars for one symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "timeframe": {"type": "string", "enum": sorted(BAR_TIMEFRAMES)},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_BARS,
                    "description": f"Number of most recent bars, up to {MAX_BARS}.",
                },
            },
            "required": ["symbol", "timeframe", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_theses",
        "description": (
            "Stored theses, in full. Omit `symbol` for all open theses, or pass one "
            "to include closed theses for that symbol."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recent_decisions",
        "description": "Decisions from previous cycles, oldest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_DECISIONS_RETURNED}
            },
            "required": ["limit"],
            "additionalProperties": False,
        },
    },
    {
        "name": WRITE_TOOL,
        "description": (
            "Propose one market order. This is the only tool that changes anything, "
            "and you may call it at most once per cycle. Every proposal is evaluated "
            "by an independent risk layer before anything reaches the broker; if a "
            "check fails you get the rejection back and nothing is sent. On an "
            "executed buy, `reasoning` and `invalidation_condition` are stored as the "
            "thesis for the position and shown to future cycles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["buy", "sell"]},
                "symbol": {"type": "string"},
                "qty": {"type": "number", "exclusiveMinimum": 0},
                "reasoning": {
                    "type": "string",
                    "description": "Why you are placing this order. Stored verbatim.",
                },
                "invalidation_condition": {
                    "type": "string",
                    "description": (
                        "What would have to become true for this position to be wrong. "
                        "Stored verbatim and shown to future cycles."
                    ),
                },
            },
            "required": ["action", "symbol", "qty", "reasoning", "invalidation_condition"],
            "additionalProperties": False,
        },
    },
]

#: Sanity check at import time rather than at 09:31 on a Tuesday.
assert {t["name"] for t in TOOL_SCHEMAS} == set(TOOL_IMPLS), "tool schema/impl mismatch"
assert TOOL_SCHEMAS[-1]["name"] == WRITE_TOOL, "the write tool must be listed last"


def dispatch(tc: ToolContext, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run one tool call. Returns (result_text, is_error).

    Every failure becomes an error *result* rather than an exception: the model
    should see what it got wrong and adjust, and a malformed tool call is not a
    reason to fail the cycle.
    """
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return f"error: no such tool {name!r}", True
    try:
        payload = impl(tc, args)
    except ToolError as exc:
        return f"error: {exc}", True
    except BrokerError as exc:
        log.warning("tool_broker_error", extra={"tool": name}, exc_info=True)
        return f"error: broker call failed: {exc}", True
    return json.dumps(payload, default=str, separators=(",", ":")), False
