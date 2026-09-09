"""Risk layer. Pure predicates over (proposal, state), plus the engine that runs them."""

from trader.risk.checks import CHECK_NAMES, CHECKS, Check, CheckResult
from trader.risk.config import RiskConfig, RiskConfigError, load_risk_config
from trader.risk.engine import Failure, Verdict, evaluate
from trader.risk.models import PositionState, Proposal, RiskState

__all__ = [
    "CHECKS",
    "CHECK_NAMES",
    "Check",
    "CheckResult",
    "Failure",
    "PositionState",
    "Proposal",
    "RiskConfig",
    "RiskConfigError",
    "RiskState",
    "Verdict",
    "evaluate",
    "load_risk_config",
]
