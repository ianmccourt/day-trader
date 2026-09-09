"""Runtime configuration. Secrets come from the environment only (SPEC.md #5)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# A .env loader is ~20 lines of stdlib, so we own it rather than take a
# dependency the spec did not list.
_ENV_FILE = Path(".env")


def load_dotenv(path: Path = _ENV_FILE) -> None:
    """Populate os.environ from a KEY=VALUE file. Real env vars always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class MissingCredential(RuntimeError):
    """Raised when a required secret is absent. Never carries the value."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingCredential(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"or export {name} in your shell."
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    db_path: Path
    cycle_minutes: int
    log_level: str

    @property
    def anthropic_api_key(self) -> str:
        """Read lazily: Phase 1 runs a stub agent and needs no Anthropic key."""
        return _require("ANTHROPIC_API_KEY")


def load_settings(*, require_broker: bool = True) -> Settings:
    """Read settings from the environment.

    `require_broker=False` lets the DB-only CLI commands (status, cycles, show,
    kill) run on a machine that has no Alpaca keys at all.
    """
    load_dotenv()
    cycle_minutes = int(os.environ.get("TRADER_CYCLE_MINUTES", "15"))
    if not 1 <= cycle_minutes <= 60:
        raise ValueError(
            f"TRADER_CYCLE_MINUTES must be between 1 and 60 (got {cycle_minutes}); "
            "the scheduler builds a cron minute-step from it."
        )
    return Settings(
        alpaca_api_key=(
            _require("ALPACA_API_KEY")
            if require_broker
            else os.environ.get("ALPACA_API_KEY", "").strip()
        ),
        alpaca_secret_key=(
            _require("ALPACA_SECRET_KEY")
            if require_broker
            else os.environ.get("ALPACA_SECRET_KEY", "").strip()
        ),
        db_path=Path(os.environ.get("TRADER_DB_PATH", "data/trader.sqlite3")),
        cycle_minutes=cycle_minutes,
        log_level=os.environ.get("TRADER_LOG_LEVEL", "INFO").upper(),
    )
