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


# --- long-horizon bounding --------------------------------------------------
#
# The original flatness test above only accumulated *decisions*, which are
# capped by count. It missed theses, whose rationale is up to 800 chars and
# whose invalidation condition has no schema cap at all — 20 verbose theses
# were worth ~32k chars of prompt. These tests cover that class of growth.


def _seed_theses(conn, count: int, rationale: str, invalidation: str) -> None:
    for i in range(count):
        conn.execute(
            "INSERT INTO theses(symbol, opened_at, rationale, invalidation_condition, status) "
            "VALUES (?, datetime('now'), ?, ?, 'open')",
            (f"S{i:03d}", rationale, invalidation),
        )


def test_the_rendered_state_is_bounded_with_everything_at_its_cap(conn) -> None:
    from trader.state import MAX_STATE_CHARS

    _seed_theses(conn, 30, "r" * 800, "i" * 800)
    broker = FakeBroker(positions=[position(f"S{i:03d}", 10, 300.0, 314.0) for i in range(40)])
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    for i in range(50):
        conn.execute(
            "INSERT INTO decisions(cycle_id, timestamp, action, symbol, qty) VALUES (?,?,?,?,?)",
            (cycle_id, f"2026-09-09T1{i % 10}:00:00+00:00", "buy", f"S{i:03d}", 10),
        )
    rendered = render_state(build_cycle_context(conn, broker, cycle_id=cycle_id))
    assert len(rendered) <= MAX_STATE_CHARS, len(rendered)


def test_a_verbose_thesis_is_truncated_in_the_rendering(conn) -> None:
    from trader.state import MAX_RENDERED_THESIS_INVALIDATION, MAX_RENDERED_THESIS_RATIONALE

    _seed_theses(conn, 1, "R" * 800, "I" * 800)
    rendered = render_state(_ctx(conn, FakeBroker()))
    assert "R" * (MAX_RENDERED_THESIS_RATIONALE + 1) not in rendered
    assert "I" * (MAX_RENDERED_THESIS_INVALIDATION + 1) not in rendered
    assert "…" in rendered
    # And the model is told where the full text lives.
    assert "call get_theses" in rendered


def test_the_full_thesis_text_is_still_reachable_through_the_tool(conn) -> None:
    """Truncation is a display concern; nothing is lost from storage."""
    from trader.risk.config import RiskConfig
    from trader.tools import ToolContext, dispatch

    _seed_theses(conn, 1, "R" * 800, "I" * 800)
    ctx = _ctx(conn, FakeBroker())
    config = RiskConfig(
        max_position_notional=1.0,
        max_total_exposure=1.0,
        max_daily_loss=1.0,
        max_orders_per_hour=1,
        max_orders_per_day=1,
        symbol_allowlist=frozenset({"AAPL"}),
    )
    tc = ToolContext(conn=conn, broker=FakeBroker(), ctx=ctx, config=config)
    text, is_error = dispatch(tc, "get_theses", {})
    assert not is_error
    assert "R" * 800 in text  # untruncated


def test_context_stays_flat_while_theses_accumulate(conn) -> None:
    """The regression the original flatness test missed."""
    broker = FakeBroker(positions=[position("AAPL", 10, 300.0, 314.0)])
    sizes: list[int] = []
    for day in range(40):
        _seed_theses(conn, 1, "r" * 800, "i" * 800)
        cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
        conn.execute(
            "INSERT INTO decisions(cycle_id, timestamp, action, symbol, qty) VALUES (?,?,?,?,?)",
            (cycle_id, f"2026-09-{(day % 28) + 1:02d}T14:00:00+00:00", "buy", "AAPL", 1),
        )
        sizes.append(len(render_state(build_cycle_context(conn, broker, cycle_id=cycle_id))))

    # Once the caps engage, the rendering stops growing entirely.
    steady = sizes[10:]
    assert max(steady) - min(steady) < 100, (min(steady), max(steady))
