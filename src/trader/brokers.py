"""Broker factory. Callers never construct an adapter themselves except evaluate's
optional Alpaca SPY benchmark (paper market data, not order submission).
"""

from __future__ import annotations

from trader.broker import Broker
from trader.config import MissingCredential, Settings
from trader.constants import ALPACA_PAPER_BASE_URL, BROKER_PAPER, BROKER_ROBINHOOD_AGENTIC


def broker_endpoint(settings: Settings) -> str:
    """Display/log the active broker endpoint. Not a configuration input."""
    if settings.broker == BROKER_PAPER:
        return ALPACA_PAPER_BASE_URL
    from trader.robinhood_mcp import ROBINHOOD_MCP_URL

    return ROBINHOOD_MCP_URL


def make_broker(settings: Settings) -> Broker:
    """The only construction path used by `run`, `cycle`, `reconcile`, dashboard."""
    if settings.broker == BROKER_PAPER:
        from trader.broker import AlpacaBroker

        if not settings.alpaca_api_key or not settings.alpaca_secret_key:
            raise MissingCredential("Alpaca paper keys are not set")
        return AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key)
    if settings.broker == BROKER_ROBINHOOD_AGENTIC:
        from trader.robinhood_mcp import RobinhoodMcpBroker

        if not settings.robinhood_token_path.is_file():
            raise MissingCredential(
                "Robinhood tokens are not present. Run `uv run trader rh-login` first."
            )
        return RobinhoodMcpBroker(settings.robinhood_token_path)
    raise ValueError(f"unknown broker {settings.broker!r}")
