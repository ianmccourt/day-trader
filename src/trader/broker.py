"""Alpaca broker adapter. Paper endpoint only.

`submit_order` is the single mutating call in this repo. It is private by
convention — nothing calls it but `trader.execution.place_order`, which runs the
risk layer first, unconditionally. `tests/test_no_bypass.py` enforces that: any
new caller fails the build.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from trader.constants import ALPACA_PAPER_BASE_URL
from trader.db import iso, utcnow

#: Bar granularities the agent may request. Deliberately coarse — this is a
#: read-only data tool, not a place to encode a strategy.
BAR_TIMEFRAMES = {
    "1Min": (TimeFrame(1, TimeFrameUnit.Minute), timedelta(minutes=1)),
    "5Min": (TimeFrame(5, TimeFrameUnit.Minute), timedelta(minutes=5)),
    "15Min": (TimeFrame(15, TimeFrameUnit.Minute), timedelta(minutes=15)),
    "1Hour": (TimeFrame(1, TimeFrameUnit.Hour), timedelta(hours=1)),
    "1Day": (TimeFrame(1, TimeFrameUnit.Day), timedelta(days=1)),
}

#: Hard cap, because bars land in the prompt and the prompt has a token budget.
MAX_BARS = 30


class BrokerError(RuntimeError):
    """Any broker failure. Raised loudly so the cycle fails and gets logged."""


@dataclass(frozen=True, slots=True)
class Clock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True, slots=True)
class OrderReceipt:
    order_id: str
    status: str
    submitted_at: datetime | None
    filled_qty: float | None


@dataclass(frozen=True, slots=True)
class OrderFill:
    """An order's terminal state, read back from the broker for evaluation."""

    order_id: str
    status: str
    filled_qty: float
    filled_avg_price: float | None
    filled_at: datetime | None

    @property
    def is_terminal(self) -> bool:
        """Whether the broker is done with this order and it will not change."""
        return self.status in {"filled", "canceled", "expired", "rejected", "done_for_day"}


class Broker(Protocol):
    """The surface the harness depends on. The test fake implements this too."""

    def get_clock(self) -> Clock: ...
    def get_account(self) -> dict[str, Any]: ...
    def get_positions(self) -> list[dict[str, Any]]: ...
    def get_latest_price(self, symbol: str) -> float: ...
    def get_bars(self, symbol: str, *, timeframe: str, limit: int) -> list[dict[str, Any]]: ...
    def submit_order(self, *, symbol: str, qty: float, side: str) -> OrderReceipt: ...
    def get_order(self, order_id: str) -> OrderFill: ...


def _f(value: Any) -> float | None:
    """Alpaca returns numerics as strings; None stays None."""
    return None if value is None else float(value)


class AlpacaBroker:
    """Thin wrapper over alpaca-py's TradingClient, pinned to the paper endpoint."""

    def __init__(self, api_key: str, secret_key: str) -> None:
        # paper=True and url_override are redundant with each other by design:
        # if a future alpaca-py changes the meaning of `paper`, the explicit
        # constant still wins. Neither value is configurable (SPEC.md #1).
        self._client = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=True,
            url_override=ALPACA_PAPER_BASE_URL,
        )
        # Market data has no paper/live distinction — it is read-only and
        # shares the same keys.
        self._data = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)

    @property
    def base_url(self) -> str:
        return ALPACA_PAPER_BASE_URL

    def get_clock(self) -> Clock:
        try:
            raw = self._client.get_clock()
        except Exception as exc:
            raise BrokerError(f"get_clock failed: {exc}") from exc
        return Clock(
            timestamp=raw.timestamp,
            is_open=bool(raw.is_open),
            next_open=raw.next_open,
            next_close=raw.next_close,
        )

    def get_account(self) -> dict[str, Any]:
        try:
            acct = self._client.get_account()
        except Exception as exc:
            raise BrokerError(f"get_account failed: {exc}") from exc
        if getattr(acct, "trading_blocked", False) or getattr(acct, "account_blocked", False):
            raise BrokerError(
                f"account {acct.account_number} is blocked by the broker "
                f"(trading_blocked={acct.trading_blocked}, account_blocked={acct.account_blocked})"
            )
        return {
            "equity": _f(acct.equity) or 0.0,
            "last_equity": _f(acct.last_equity),
            "cash": _f(acct.cash) or 0.0,
            "buying_power": _f(acct.buying_power),
            "long_market_value": _f(acct.long_market_value),
            "short_market_value": _f(acct.short_market_value),
            "captured_at": iso(utcnow()),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        """Always read fresh. Positions are never trusted from memory (SPEC.md)."""
        try:
            raw = self._client.get_all_positions()
        except Exception as exc:
            raise BrokerError(f"get_all_positions failed: {exc}") from exc
        captured_at = iso(utcnow())
        return [
            {
                "symbol": p.symbol,
                "qty": _f(p.qty) or 0.0,
                "avg_price": _f(p.avg_entry_price) or 0.0,
                "current_price": _f(p.current_price),
                "market_value": _f(p.market_value),
                "unrealized_pl": _f(p.unrealized_pl),
                "captured_at": captured_at,
            }
            for p in raw
        ]

    def get_latest_price(self, symbol: str) -> float:
        """Last trade price, used to size a proposal for the risk checks.

        Sourced from the broker rather than from the model. If the model could
        supply the price it could understate notional and walk straight through
        every notional cap.
        """
        try:
            request = StockLatestTradeRequest(symbol_or_symbols=symbol)
            trades = self._data.get_stock_latest_trade(request)
        except Exception as exc:
            raise BrokerError(f"get_latest_price({symbol}) failed: {exc}") from exc
        trade = trades.get(symbol)
        if trade is None or trade.price is None:
            raise BrokerError(f"no latest trade for {symbol}")
        price = float(trade.price)
        if price <= 0:
            raise BrokerError(f"non-positive last price for {symbol}: {price}")
        return price

    def get_bars(self, symbol: str, *, timeframe: str, limit: int) -> list[dict[str, Any]]:
        """Recent OHLCV bars. Read-only market data, no strategy attached."""
        spec = BAR_TIMEFRAMES.get(timeframe)
        if spec is None:
            raise BrokerError(
                f"unsupported timeframe {timeframe!r}; "
                f"expected one of {', '.join(sorted(BAR_TIMEFRAMES))}"
            )
        unit, span = spec
        limit = max(1, min(int(limit), MAX_BARS))
        # Two gotchas, both load-bearing. Without an explicit `start` Alpaca
        # returns only the newest bar; and its `limit` takes the *oldest* N in
        # the window, not the newest. So: reach back generously (x4, to absorb
        # weekends and holidays), ask for the whole window, and trim to the
        # newest `limit` ourselves.
        start = datetime.now(UTC) - span * limit * 4
        try:
            request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=unit, start=start)
            bars = self._data.get_stock_bars(request)
        except Exception as exc:
            raise BrokerError(f"get_bars({symbol}, {timeframe}) failed: {exc}") from exc
        return [
            {
                "t": bar.timestamp.isoformat(),
                "o": float(bar.open),
                "h": float(bar.high),
                "l": float(bar.low),
                "c": float(bar.close),
                "v": float(bar.volume),
            }
            for bar in bars.data.get(symbol, [])[-limit:]
        ]

    def submit_order(self, *, symbol: str, qty: float, side: str) -> OrderReceipt:
        """The only mutating broker call in the repo.

        Do not call this directly. `trader.execution.place_order` is the sole
        caller and runs the risk layer first; a second caller would be a
        bypass, which is why test_no_bypass.py checks for one.
        """
        if side not in ("buy", "sell"):
            raise BrokerError(f"side must be 'buy' or 'sell', got {side!r}")
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            # DAY, never GTC: an order that outlives the session would execute
            # against a state no cycle ever evaluated.
            time_in_force=TimeInForce.DAY,
        )
        try:
            order = self._client.submit_order(request)
        except Exception as exc:
            raise BrokerError(f"submit_order({side} {qty} {symbol}) failed: {exc}") from exc
        return OrderReceipt(
            order_id=str(order.id),
            status=str(getattr(order.status, "value", order.status)),
            submitted_at=order.submitted_at,
            filled_qty=_f(order.filled_qty),
        )

    def get_order(self, order_id: str) -> OrderFill:
        """Read one order back. Used by `trader reconcile`, never in a cycle."""
        try:
            order = self._client.get_order_by_id(order_id)
        except Exception as exc:
            raise BrokerError(f"get_order({order_id}) failed: {exc}") from exc
        return OrderFill(
            order_id=str(order.id),
            status=str(getattr(order.status, "value", order.status)),
            filled_qty=_f(order.filled_qty) or 0.0,
            filled_avg_price=_f(order.filled_avg_price),
            filled_at=order.filled_at,
        )
