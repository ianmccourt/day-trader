"""Risk limits, loaded from risk.toml.

stdlib tomllib rather than YAML — no new dependency, and the file is flat
enough that TOML costs nothing in readability.

Loading is strict: an unrecognised key raises. A typo in a limit name that
silently left that limit at its default would be the worst possible failure
mode for this file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_RISK_CONFIG_PATH = Path("risk.toml")

_SCHEMA: dict[str, set[str]] = {
    "limits": {
        "max_position_notional",
        "max_total_exposure",
        "max_daily_loss",
        "max_orders_per_hour",
        "max_orders_per_day",
    },
    "universe": {"symbol_allowlist"},
    "session": {"regular_trading_hours_only", "allow_shorts"},
}


class RiskConfigError(ValueError):
    """risk.toml is malformed. Always fatal — never fall back to defaults."""


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_position_notional: float
    max_total_exposure: float
    max_daily_loss: float
    max_orders_per_hour: int
    max_orders_per_day: int
    symbol_allowlist: frozenset[str]
    regular_trading_hours_only: bool = True
    allow_shorts: bool = False
    source: str = "<literal>"

    def __post_init__(self) -> None:
        for name in (
            "max_position_notional",
            "max_total_exposure",
            "max_daily_loss",
            "max_orders_per_hour",
            "max_orders_per_day",
        ):
            value = getattr(self, name)
            if value < 0:
                raise RiskConfigError(f"{name} must be >= 0, got {value!r}")
        if not self.symbol_allowlist:
            # An empty allowlist means "nothing is tradeable". That is a safe
            # state, but it is almost always a mistake, so say so loudly.
            raise RiskConfigError(
                "symbol_allowlist is empty — nothing would be tradeable. "
                "If that is intentional, engage the kill switch instead."
            )
        if any(s != s.upper() or not s.isalpha() for s in self.symbol_allowlist):
            raise RiskConfigError(
                f"symbol_allowlist must be uppercase alphabetic tickers, got "
                f"{sorted(self.symbol_allowlist)}"
            )


def _validate_sections(raw: dict[str, Any], source: str) -> None:
    unknown_sections = set(raw) - set(_SCHEMA)
    if unknown_sections:
        raise RiskConfigError(f"{source}: unknown section(s) {sorted(unknown_sections)}")
    for section, allowed in _SCHEMA.items():
        unknown_keys = set(raw.get(section, {})) - allowed
        if unknown_keys:
            raise RiskConfigError(
                f"{source}: unknown key(s) {sorted(unknown_keys)} in [{section}]"
            )


def _require(raw: dict[str, Any], section: str, key: str, source: str) -> Any:
    try:
        return raw[section][key]
    except KeyError as exc:
        raise RiskConfigError(f"{source}: missing required key [{section}].{key}") from exc


def load_risk_config(path: Path = DEFAULT_RISK_CONFIG_PATH) -> RiskConfig:
    if not path.is_file():
        raise RiskConfigError(f"risk config not found at {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RiskConfigError(f"{path}: invalid TOML: {exc}") from exc

    source = str(path)
    _validate_sections(raw, source)
    session = raw.get("session", {})
    symbols = _require(raw, "universe", "symbol_allowlist", source)
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise RiskConfigError(f"{source}: symbol_allowlist must be a list of strings")

    return RiskConfig(
        max_position_notional=float(_require(raw, "limits", "max_position_notional", source)),
        max_total_exposure=float(_require(raw, "limits", "max_total_exposure", source)),
        max_daily_loss=float(_require(raw, "limits", "max_daily_loss", source)),
        max_orders_per_hour=int(_require(raw, "limits", "max_orders_per_hour", source)),
        max_orders_per_day=int(_require(raw, "limits", "max_orders_per_day", source)),
        symbol_allowlist=frozenset(symbols),
        regular_trading_hours_only=bool(session.get("regular_trading_hours_only", True)),
        allow_shorts=bool(session.get("allow_shorts", False)),
        source=source,
    )
