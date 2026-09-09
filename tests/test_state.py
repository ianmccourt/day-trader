"""The context must not grow with session length. This is SPEC.md constraint #4's
precondition: if rendering is bounded, the token assertion in Phase 3 can hold."""

from __future__ import annotations

import pytest

from tests.fakes import FakeBroker, position
from trader.agent import StubAgent
from trader.cycle import run_cycle
from trader.db import connect, open_cycle, utcnow
from trader.state import (
    MAX_RECENT_DECISIONS,
    MAX_RENDERED_POSITIONS,
    build_cycle_context,
    render_state,
    trading_day_for,
)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.sqlite3")


def _ctx(conn, broker):
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    return build_cycle_context(conn, broker, cycle_id=cycle_id)


def test_render_is_stable_across_many_cycles(tmp_path) -> None:
    """Cycle 200 must look like cycle 1 — that is the whole architecture."""
    conn = connect(tmp_path / "t.sqlite3")
    broker = FakeBroker(positions=[position("AAPL", 10, 200.0, 205.0)])

    sizes: list[int] = []
    for _ in range(60):
        outcome = run_cycle(conn, broker, StubAgent())
        row = conn.execute(
            "SELECT full_prompt FROM cycles WHERE cycle_id = ?", (outcome.cycle_id,)
        ).fetchone()
        sizes.append(len(row["full_prompt"]))

    # After the recent-decisions window fills, the rendering stops growing.
    steady = sizes[MAX_RECENT_DECISIONS + 1 :]
    assert max(steady) - min(steady) < 200, (min(steady), max(steady))
    assert max(sizes) < 4000


def test_positions_are_capped_and_the_truncation_is_disclosed(conn) -> None:
    broker = FakeBroker(
        positions=[position(f"S{i:03d}", 1, 10.0) for i in range(MAX_RENDERED_POSITIONS + 5)]
    )
    ctx = _ctx(conn, broker)
    assert len(ctx.positions) == MAX_RENDERED_POSITIONS
    assert "5 further positions not rendered" in render_state(ctx)


def test_recent_decisions_are_capped_and_chronological(conn) -> None:
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    for i in range(MAX_RECENT_DECISIONS + 5):
        conn.execute(
            "INSERT INTO decisions(cycle_id, timestamp, action, symbol) VALUES (?, ?, ?, ?)",
            (cycle_id, f"2026-09-09T10:{i:02d}:00+00:00", "no_action", f"S{i}"),
        )
    ctx = _ctx(conn, FakeBroker())
    assert len(ctx.recent_decisions) == MAX_RECENT_DECISIONS
    # Oldest first, so the model reads them in the order they happened.
    symbols = [d["symbol"] for d in ctx.recent_decisions]
    assert symbols == sorted(symbols, key=lambda s: int(s[1:]))
    assert symbols[-1] == f"S{MAX_RECENT_DECISIONS + 4}"


def test_only_open_theses_are_rendered(conn) -> None:
    for symbol, status in [("AAPL", "open"), ("MSFT", "closed")]:
        conn.execute(
            "INSERT INTO theses(symbol, opened_at, rationale, invalidation_condition, status) "
            "VALUES (?, ?, 'why', 'when', ?)",
            (symbol, utcnow().isoformat(), status),
        )
    rendered = render_state(_ctx(conn, FakeBroker()))
    assert "AAPL" in rendered
    assert "MSFT" not in rendered


def test_total_exposure_uses_absolute_notional(conn) -> None:
    broker = FakeBroker(positions=[position("AAPL", 10, 100.0), position("TSLA", -5, 200.0)])
    ctx = _ctx(conn, broker)
    assert ctx.total_exposure == pytest.approx(1000.0 + 1000.0)


def test_position_lookup_is_case_insensitive(conn) -> None:
    ctx = _ctx(conn, FakeBroker(positions=[position("AAPL", 1, 1.0)]))
    assert ctx.position_for("aapl") is not None
    assert ctx.position_for("NVDA") is None


def test_kill_switch_state_is_visible_in_the_rendering(conn) -> None:
    from trader.db import KILL_SWITCH, set_flag

    set_flag(conn, KILL_SWITCH, "1")
    assert "kill_switch: ENGAGED" in render_state(_ctx(conn, FakeBroker()))
