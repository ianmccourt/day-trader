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
