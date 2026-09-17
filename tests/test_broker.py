"""Alpaca clock fallback and open-order flattening."""

from __future__ import annotations

from types import SimpleNamespace

from trader.broker import (
    AlpacaBroker,
    BrokerError,
    Clock,
    clock_with_fallback,
    flatten_working_orders,
    weekday_rth_clock,
)
from trader.constants import MARKET_TZ


def test_clock_retries_then_returns_the_broker_clock() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def fetch() -> Clock:
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerError("get_clock failed: Internal Server Error")
        return weekday_rth_clock()

    clock = clock_with_fallback(fetch, attempts=2, pause_s=0.4, sleeper=sleeps.append)
    assert calls["n"] == 2
    assert sleeps == [0.4]
    assert clock.note is None


def test_clock_falls_back_to_weekday_rth_after_retries() -> None:
    sleeps: list[float] = []

    def fetch() -> Clock:
        raise BrokerError('get_clock failed: {"message":"Internal Server Error"}')

    clock = clock_with_fallback(fetch, attempts=2, pause_s=0.5, sleeper=sleeps.append)
    assert sleeps == [0.5]
    assert clock.note is not None
    assert "weekday RTH fallback" in clock.note
    assert clock.is_open is weekday_rth_clock().is_open


def test_alpaca_get_clock_does_not_raise_on_500(monkeypatch) -> None:
    broker = AlpacaBroker("key", "secret")
    broker._client = SimpleNamespace(
        get_clock=lambda: (_ for _ in ()).throw(RuntimeError('{"message":"Internal Server Error"}'))
    )
    monkeypatch.setattr("trader.broker.time.sleep", lambda _s: None)
    clock = broker.get_clock()
    assert clock.note is not None
    assert clock.timestamp.tzinfo is not None
    # During a weekday session this must still report open so a held position
    # can be managed; the fallback is the same weekday window the scheduler uses.
    from datetime import datetime

    wed = datetime(2026, 9, 11, 12, 30, tzinfo=MARKET_TZ)
    assert weekday_rth_clock(wed).is_open is True


def _order(
    *,
    order_id: str,
    symbol: str = "NVDA",
    side: str = "sell",
    order_type: str = "limit",
    status: str = "new",
    qty: float = 456,
    stop_price: float | None = None,
    limit_price: float | None = None,
    legs: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        symbol=symbol,
        side=SimpleNamespace(value=side),
        order_type=SimpleNamespace(value=order_type),
        status=SimpleNamespace(value=status),
        qty=qty,
        stop_price=stop_price,
        limit_price=limit_price,
        legs=legs,
    )


def test_flatten_working_orders_includes_held_stop_nested_under_filled_parent() -> None:
    """Live Alpaca shape: filled bracket parent, TP `new`, stop `held`.

    `status=open` would return only the take-profit. Flattening legs is what
    puts the stop on `## Open orders`.
    """
    take_profit = _order(
        order_id="tp", order_type="limit", status="new", limit_price=221.45
    )
    stop = _order(
        order_id="stop", order_type="stop", status="held", stop_price=217.93
    )
    parent = _order(
        order_id="parent",
        side="buy",
        order_type="market",
        status="filled",
        qty=456,
        legs=[take_profit, stop],
    )
    # Vendor also lists the take-profit as a top-level open order.
    working = flatten_working_orders([take_profit, parent])
    by_id = {o["order_id"]: o for o in working}
    assert set(by_id) == {"tp", "stop"}
    assert by_id["stop"]["type"] == "stop"
    assert by_id["stop"]["stop_price"] == 217.93
    assert by_id["stop"]["status"] == "held"
    assert by_id["tp"]["limit_price"] == 221.45
    assert "parent" not in by_id


def test_flatten_working_orders_drops_filled_and_canceled_legs() -> None:
    filled_tp = _order(
        order_id="tp", order_type="limit", status="filled", limit_price=216.3
    )
    canceled_stop = _order(
        order_id="stop", order_type="stop", status="canceled", stop_price=213.84
    )
    parent = _order(
        order_id="parent",
        side="buy",
        order_type="market",
        status="filled",
        legs=[filled_tp, canceled_stop],
    )
    assert flatten_working_orders([parent]) == []


def test_get_open_orders_queries_all_nested_and_flattens_legs() -> None:
    take_profit = _order(
        order_id="tp", order_type="limit", status="new", limit_price=221.45
    )
    stop = _order(
        order_id="stop", order_type="stop", status="held", stop_price=217.93
    )
    parent = _order(
        order_id="parent",
        side="buy",
        order_type="market",
        status="filled",
        legs=[take_profit, stop],
    )
    captured: dict = {}

    def get_orders(*, filter):  # noqa: A002 — matches alpaca-py
        captured["filter"] = filter
        return [parent]

    broker = AlpacaBroker("key", "secret")
    broker._client = SimpleNamespace(get_orders=get_orders)
    orders = broker.get_open_orders("nvda")
    req = captured["filter"]
    assert req.status.value == "all"
    assert req.nested is True
    assert req.symbols == ["NVDA"]
    types = {o["type"] for o in orders}
    assert types == {"limit", "stop"}
