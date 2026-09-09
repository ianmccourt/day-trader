"""Runs every check against a proposal. The only way to get an order approved."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from trader.risk.checks import CHECKS, Check
from trader.risk.models import Proposal, RiskState

log = logging.getLogger("trader.risk")


@dataclass(frozen=True, slots=True)
class Failure:
    check: str
    reason: str


@dataclass(frozen=True, slots=True)
class Verdict:
    approved: bool
    failures: tuple[Failure, ...] = ()

    @property
    def summary(self) -> str:
        """Goes into decisions.risk_result. Greppable on purpose."""
        if self.approved:
            return "approved"
        return "rejected:" + ",".join(f.check for f in self.failures)

    def as_model_message(self) -> str:
        """Returned verbatim to the model as a tool result (SPEC.md #3)."""
        if self.approved:
            return "Order approved by the risk layer."
        lines = ["Order REJECTED by the risk layer. Nothing was sent to the broker."]
        lines += [f"- {f.check}: {f.reason}" for f in self.failures]
        return "\n".join(lines)


def evaluate(
    proposal: Proposal, state: RiskState, *, checks: tuple[Check, ...] = CHECKS
) -> Verdict:
    """Run every check. Never short-circuits; a rejection lists all failures.

    A check that raises is treated as a failure, not as an absence of one. A
    risk layer that fails open is worse than no risk layer.
    """
    failures: list[Failure] = []
    for check in checks:
        try:
            ok, reason = check(proposal, state)
        # Fail closed: a risk layer that fails open is worse than none.
        except Exception as exc:
            log.error(
                "risk_check_raised",
                extra={"check": check.__name__, "symbol": proposal.symbol},
                exc_info=True,
            )
            failures.append(
                Failure(check.__name__, f"check raised {type(exc).__name__}: {exc}")
            )
            continue
        if not ok:
            failures.append(Failure(check.__name__, reason))
    return Verdict(approved=not failures, failures=tuple(failures))
