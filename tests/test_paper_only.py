"""SPEC.md constraint #1, enforced by the test suite rather than by vigilance."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from trader.broker import AlpacaBroker
from trader.constants import ALPACA_LIVE_BASE_URL_FORBIDDEN, ALPACA_PAPER_BASE_URL

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = sorted((ROOT / "src").rglob("*.py"))

# Anything that looks like a live Alpaca trading host.
LIVE_HOST = re.compile(r"https?://(?!paper-)api\.alpaca\.markets")


def test_paper_url_constant_is_the_paper_endpoint() -> None:
    assert ALPACA_PAPER_BASE_URL == "https://paper-api.alpaca.markets"


def test_no_live_endpoint_anywhere_in_src() -> None:
    offenders = []
    for path in SOURCE_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "ALPACA_LIVE_BASE_URL_FORBIDDEN" in line:
                continue  # the guard constant itself
            if LIVE_HOST.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "live trading endpoint referenced in source:\n" + "\n".join(offenders)


def test_base_url_is_not_configurable() -> None:
    """No env var, argument, or setting may influence the broker endpoint."""
    for path in SOURCE_FILES:
        text = path.read_text()
        if "ALPACA_PAPER_BASE_URL" not in text:
            continue
        # The constant may be read, never assigned outside its defining module.
        if path.name == "constants.py":
            continue
        assert not re.search(r"^\s*ALPACA_PAPER_BASE_URL\s*=", text, re.M), path


def test_broker_reports_paper_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = AlpacaBroker("key", "secret")
    assert broker.base_url == ALPACA_PAPER_BASE_URL
    assert ALPACA_LIVE_BASE_URL_FORBIDDEN not in broker.base_url


def test_settings_has_no_url_field() -> None:
    from trader.config import Settings

    assert not any("url" in f.lower() for f in Settings.__dataclass_fields__)


# Robinhood Agentic MCP is a separate Cursor-level live account (Path A).
# It must not be registered in this project, or the paper loop would see
# live write tools and skip the risk layer.
_RH_MCP = "agent.robinhood.com"


def test_project_cursor_config_does_not_register_robinhood_mcp() -> None:
    """User-level ~/.cursor/mcp.json is the RH connection. This repo is not."""
    project_cursor = ROOT / ".cursor"
    if not project_cursor.exists():
        return
    for path in project_cursor.rglob("*"):
        if path.is_file():
            assert _RH_MCP not in path.read_text(encoding="utf-8", errors="replace"), path


def test_src_references_robinhood_mcp_only_in_the_adapter() -> None:
    """The host string lives in the adapter, same pattern as the live Alpaca guard."""
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno}"
        for path in SOURCE_FILES
        if path.name != "robinhood_mcp.py"
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        if _RH_MCP in line
    ]
    assert not offenders, "Robinhood MCP host leaked outside the adapter:\n" + "\n".join(
        offenders
    )


def test_adapter_pins_the_official_robinhood_mcp_url() -> None:
    from trader.robinhood_mcp import ROBINHOOD_MCP_URL

    assert ROBINHOOD_MCP_URL == "https://agent.robinhood.com/mcp/trading"
