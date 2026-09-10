"""The two values every risk check reads: a Proposal and a RiskState.

Both are frozen. A check receives them and returns a verdict; it cannot reach
the database, the broker, or the clock, which is what makes the checks unit
testable in isolation (SPEC.md constraint #3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from trader.risk.config import RiskConfig

if TYPE_CHECKING:
    from trader.state import CycleContext

Action = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class Proposal:
    """An order the agent wants placed. Descriptive only — holds no broker handle."""

    action: Action
    symbol: str
    qty: float
    #: Price used to size the order for risk purposes. Fetched from the broker
    #: by the execution layer, never supplied by the model — otherwise the model
    #: could understate notional and walk straight through the notional caps.
    reference_price: float
    reasoning: str = ""
    #: Optional protective prices, submitted with the entry as an Alpaca OTO
    #: (stop or take-profit alone) or bracket (both). Evaluated by the risk
    #: layer; never trusted as a way around notional caps.
    stop_price: float | None = None
    take_profit_price: float | None = None

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.reference_price

    @property
    def signed_qty(self) -> float:
        return self.qty if self.action == "buy" else -self.qty

    @property
    def is_finite(self) -> bool:
        return math.isfinite(self.qty) and math.isfinite(self.reference_price)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "symbol": self.symbol,
            "qty": self.qty,
            "reference_price": self.reference_price,
            "notional": self.notional if self.is_finite else None,
            "reasoning": self.reasoning,
            "stop_price": self.stop_price,
            "take_profit_price": self.take_profit_price,
        }


@dataclass(frozen=True, slots=True)
class PositionState:
    symbol: str
    qty: float
    market_value: float


@dataclass(frozen=True, slots=True)
class RiskState:
    """Everything the checks are allowed to know, captured at one instant."""

    config: RiskConfig
    now: datetime
    market_open: bool
    kill_switch: bool
    equity: float
    #: Prior session's closing equity. `max_daily_loss` measures against this.
    start_of_day_equity: float | None
    positions: dict[str, PositionState] = field(default_factory=dict)
    orders_last_hour: int = 0
    orders_today: int = 0
    orders_this_cycle: int = 0
    #: True once max_daily_loss has fired today. Latched in the DB by the
    #: execution layer so an intraday recovery does not re-enable trading.
    daily_loss_halted: bool = False

    @property
    def total_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions.values())

    @property
    def daily_pl(self) -> float | None:
        if self.start_of_day_equity is None:
            return None
        return self.equity - self.start_of_day_equity

    def position(self, symbol: str) -> PositionState:
        return self.positions.get(
            symbol.upper(), PositionState(symbol=symbol.upper(), qty=0.0, market_value=0.0)
        )

    @classmethod
    def from_context(
        cls,
        ctx: CycleContext,
        config: RiskConfig,
        *,
        kill_switch: bool | None = None,
        daily_loss_halted: bool = False,
    ) -> RiskState:
        return cls(
            config=config,
            now=ctx.now,
            market_open=ctx.clock.is_open,
            kill_switch=ctx.kill_switch if kill_switch is None else kill_switch,
            equity=float(ctx.account.get("equity") or 0.0),
            start_of_day_equity=(
                None if ctx.account.get("last_equity") is None
                else float(ctx.account["last_equity"])
            ),
            positions={
                p["symbol"].upper(): PositionState(
                    symbol=p["symbol"].upper(),
                    qty=float(p["qty"]),
                    market_value=float(p.get("market_value") or 0.0),
                )
                for p in ctx.positions
            },
            orders_last_hour=ctx.orders_last_hour,
            orders_today=ctx.orders_today,
            orders_this_cycle=ctx.orders_this_cycle,
            daily_loss_halted=daily_loss_halted,
        )
