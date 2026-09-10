"""In-memory broker double. Implements the full Broker protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trader.broker import BrokerError, Clock, OrderFill, OrderReceipt
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

    def get_bars(self, symbol: str, *, timeframe: str, limit: int) -> list[dict[str, Any]]:
        self._check("get_bars")
        price = self.prices.get(symbol.upper(), self.default_price)
        return [
            {
                "t": f"2026-09-{(i % 28) + 1:02d}T04:00:00+00:00",
                "o": price,
                "h": price * 1.01,
                "l": price * 0.99,
                "c": price,
                "v": 1_000_000.0,
            }
            for i in range(min(limit, 30))
        ]

    def get_order(self, order_id: str) -> OrderFill:
        self._check("get_order")
        return OrderFill(
            order_id=order_id,
            status="filled",
            filled_qty=1.0,
            filled_avg_price=self.default_price,
            filled_at=utcnow(),
        )

    def submit_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: str,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> OrderReceipt:
        self._check("submit_order")
        record: dict[str, Any] = {"symbol": symbol, "qty": qty, "side": side}
        if stop_price is not None:
            record["stop_price"] = stop_price
        if take_profit_price is not None:
            record["take_profit_price"] = take_profit_price
        self.submitted.append(record)
        self._apply_fill(symbol, qty, side)
        return OrderReceipt(
            order_id=f"fake-order-{len(self.submitted)}",
            status="accepted",
            submitted_at=utcnow(),
            filled_qty=qty,
        )

    def _apply_fill(self, symbol: str, qty: float, side: str) -> None:
        """Paper fills immediately so a later order in the same cycle sees the book."""
        signed = qty if side == "buy" else -qty
        price = self.prices.get(symbol.upper(), self.default_price)
        existing = next((p for p in self.positions if p["symbol"] == symbol.upper()), None)
        if existing is None:
            self.positions.append(position(symbol.upper(), signed, price, price))
            return
        new_qty = float(existing["qty"]) + signed
        if abs(new_qty) < 1e-9:
            self.positions = [p for p in self.positions if p["symbol"] != symbol.upper()]
            return
        existing["qty"] = new_qty
        last = float(existing.get("current_price") or price)
        existing["market_value"] = new_qty * last
        existing["unrealized_pl"] = new_qty * (last - float(existing["avg_price"]))


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


# --- Anthropic client double ------------------------------------------------


@dataclass
class FakeBlock:
    type: str
    text: str = ""
    name: str = ""
    id: str = ""
    input: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return {"type": self.type, "text": self.text, "name": self.name, "input": self.input}


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_details: Any = None


def text_response(text: str) -> FakeResponse:
    return FakeResponse(content=[FakeBlock("text", text=text)], stop_reason="end_turn")


def tool_response(*calls: tuple[str, dict[str, Any]], text: str = "") -> FakeResponse:
    blocks = [FakeBlock("text", text=text)] if text else []
    blocks += [
        FakeBlock("tool_use", name=name, id=f"toolu_{i}", input=args)
        for i, (name, args) in enumerate(calls)
    ]
    return FakeResponse(content=blocks, stop_reason="tool_use")


@dataclass
class FakeCount:
    input_tokens: int


class FakeMessages:
    def __init__(self, owner: FakeAnthropic) -> None:
        self._owner = owner

    def count_tokens(self, **kwargs: Any) -> FakeCount:
        self._owner.count_calls.append(kwargs)
        return FakeCount(self._owner.token_counts.pop(0) if self._owner.token_counts else 500)

    def create(self, **kwargs: Any) -> FakeResponse:
        self._owner.requests.append(kwargs)
        if not self._owner.responses:
            raise AssertionError("FakeAnthropic ran out of scripted responses")
        return self._owner.responses.pop(0)


class FakeAnthropic:
    """Returns scripted responses in order; records every request it was sent."""

    def __init__(
        self,
        responses: list[FakeResponse] | None = None,
        token_counts: list[int] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.token_counts = list(token_counts or [])
        self.requests: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []
        self.messages = FakeMessages(self)
