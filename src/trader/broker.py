"""Alpaca broker adapter. Paper endpoint only.

Phase 1 is read-only on purpose: order submission does not exist here yet, so
"no order path outside the risk layer" is true by construction rather than by
discipline. Phase 2 adds submission behind the risk gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from alpaca.trading.client import TradingClient

from trader.constants import ALPACA_PAPER_BASE_URL
from trader.db import iso, utcnow


class BrokerError(RuntimeError):
    """Any broker failure. Raised loudly so the cycle fails and gets logged."""


@dataclass(frozen=True, slots=True)
class Clock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class Broker(Protocol):
    """The surface the harness depends on. The Phase 2 fake implements this too."""

    def get_clock(self) -> Clock: ...
    def get_account(self) -> dict[str, Any]: ...
    def get_positions(self) -> list[dict[str, Any]]: ...


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
