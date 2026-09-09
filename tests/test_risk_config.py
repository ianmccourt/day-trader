"""risk.toml must load exactly, or fail. Never fall back to defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from trader.risk.config import (
    DEFAULT_RISK_CONFIG_PATH,
    RiskConfig,
    RiskConfigError,
    load_risk_config,
)

VALID = """
[limits]
max_position_notional = 5000.0
max_total_exposure = 25000.0
max_daily_loss = 2000.0
max_orders_per_hour = 6
max_orders_per_day = 20

[universe]
symbol_allowlist = ["AAPL", "SPY"]

[session]
regular_trading_hours_only = true
allow_shorts = false
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "risk.toml"
    path.write_text(body)
    return path


def test_loads_the_shipped_config() -> None:
    """The risk.toml in the repo must actually parse."""
    config = load_risk_config(DEFAULT_RISK_CONFIG_PATH)
    assert config.max_position_notional > 0
    assert "SPY" in config.symbol_allowlist
    assert config.allow_shorts is False


def test_loads_a_valid_file(tmp_path: Path) -> None:
    config = load_risk_config(write(tmp_path, VALID))
    assert config.max_orders_per_hour == 6
    assert config.symbol_allowlist == frozenset({"AAPL", "SPY"})
    assert config.regular_trading_hours_only is True


def test_session_defaults_are_the_safe_ones(tmp_path: Path) -> None:
    body = VALID[: VALID.index("[session]")]
    config = load_risk_config(write(tmp_path, body))
    assert config.regular_trading_hours_only is True
    assert config.allow_shorts is False


def test_a_typo_in_a_limit_name_is_fatal(tmp_path: Path) -> None:
    """The worst failure mode: a misspelled limit silently not applying."""
    body = VALID.replace("max_daily_loss", "max_dialy_loss")
    with pytest.raises(RiskConfigError, match="unknown key"):
        load_risk_config(write(tmp_path, body))


def test_an_unknown_section_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(RiskConfigError, match="unknown section"):
        load_risk_config(write(tmp_path, VALID + "\n[extras]\nfoo = 1\n"))


def test_a_missing_limit_is_fatal(tmp_path: Path) -> None:
    body = "\n".join(ln for ln in VALID.splitlines() if "max_total_exposure" not in ln)
    with pytest.raises(RiskConfigError, match="missing required key"):
        load_risk_config(write(tmp_path, body))


def test_a_missing_file_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(RiskConfigError, match="not found"):
        load_risk_config(tmp_path / "nope.toml")


def test_malformed_toml_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(RiskConfigError, match="invalid TOML"):
        load_risk_config(write(tmp_path, "[limits\n"))


def test_negative_limits_are_rejected(tmp_path: Path) -> None:
    body = VALID.replace("max_daily_loss = 2000.0", "max_daily_loss = -1.0")
    with pytest.raises(RiskConfigError, match="must be >= 0"):
        load_risk_config(write(tmp_path, body))


def test_zero_limits_are_allowed(tmp_path: Path) -> None:
    """Boundary: zero is a legitimate 'no trading' setting, not a mistake."""
    body = VALID.replace("max_orders_per_day = 20", "max_orders_per_day = 0")
    assert load_risk_config(write(tmp_path, body)).max_orders_per_day == 0


def test_an_empty_allowlist_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace('["AAPL", "SPY"]', "[]")
    with pytest.raises(RiskConfigError, match="allowlist is empty"):
        load_risk_config(write(tmp_path, body))


def test_lowercase_allowlist_entries_are_rejected(tmp_path: Path) -> None:
    """Checks uppercase the proposal, so a lowercase entry would never match."""
    body = VALID.replace('["AAPL", "SPY"]', '["aapl"]')
    with pytest.raises(RiskConfigError, match="uppercase"):
        load_risk_config(write(tmp_path, body))


def test_config_is_immutable() -> None:
    config = load_risk_config(DEFAULT_RISK_CONFIG_PATH)
    with pytest.raises(AttributeError):
        config.max_daily_loss = 1e9  # type: ignore[misc]
    assert isinstance(config, RiskConfig)
