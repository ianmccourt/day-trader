"""In-memory broker double. Implements the full Broker protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trader.broker import BrokerError, Clock, OrderReceipt
from trader.constants import MARKET_TZ
from trader.db import iso, utcnow


@dataclass
class FakeBroker:
    is_open: bool = True
    equity: float = 100_000.0
    last_equity: float = 100_000.0
    cash: float = 100_000.0
    positions: list[dict[str, Any]] = field(default_factory=list)
    prices: dict[str, float] = field(default_factory=dict)
    default_price: float = 100.0
    fail_on: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    submitted: list[dict[str, Any]] = field(default_factory=list)

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_on:
            raise BrokerError(f"simulated {name} failure")

    def get_clock(self) -> Clock:
        self._check("get_clock")
        now = utcnow()
        return Clock(
            timestamp=now,
            is_open=self.is_open,
            next_open=now + timedelta(hours=1),
            next_close=now.astimezone(MARKET_TZ).replace(
                hour=16, minute=0, second=0, microsecond=0
            ),
        )

    def get_account(self) -> dict[str, Any]:
        self._check("get_account")
        return {
            "equity": self.equity,
            "last_equity": self.last_equity,
            "cash": self.cash,
            "buying_power": self.cash * 2,
            "long_market_value": sum(p.get("market_value") or 0.0 for p in self.positions),
            "short_market_value": 0.0,
            "captured_at": iso(utcnow()),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        self._check("get_positions")
        captured_at = iso(utcnow())
        return [dict(p, captured_at=captured_at) for p in self.positions]

    def get_latest_price(self, symbol: str) -> float:
        self._check("get_latest_price")
        return self.prices.get(symbol.upper(), self.default_price)

    def submit_order(self, *, symbol: str, qty: float, side: str) -> OrderReceipt:
        self._check("submit_order")
        self.submitted.append({"symbol": symbol, "qty": qty, "side": side})
        return OrderReceipt(
            order_id=f"fake-order-{len(self.submitted)}",
            status="accepted",
            submitted_at=utcnow(),
            filled_qty=0.0,
        )


def position(
    symbol: str, qty: float, avg_price: float, last: float | None = None
) -> dict[str, Any]:
    last = avg_price if last is None else last
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_price": avg_price,
        "current_price": last,
        "market_value": qty * last,
        "unrealized_pl": qty * (last - avg_price),
    }


def make_clock(is_open: bool, now: datetime | None = None) -> Clock:
    now = now or utcnow()
    return Clock(now, is_open, now + timedelta(hours=1), now + timedelta(hours=6))
