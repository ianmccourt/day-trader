"""Token headroom: abort to no_action before a mandated cycle would blow the budget."""

from __future__ import annotations

from tests.fakes import FakeAnthropic, FakeBroker, text_response
from trader.constants import MAX_PROMPT_TOKENS
from trader.db import connect, open_cycle, utcnow
from trader.llm import HEADROOM_RESERVE_TOKENS, AnthropicAgent
from trader.risk.config import RiskConfig
from trader.state import build_cycle_context, trading_day_for

CONFIG = RiskConfig(
    max_position_notional=5_000.0,
    max_total_exposure=25_000.0,
    max_daily_loss=2_000.0,
    max_orders_per_hour=6,
    max_orders_per_day=20,
    symbol_allowlist=frozenset({"AAPL", "SPY"}),
)


def _agent(tmp_path, token_counts: list[int]) -> AnthropicAgent:
    conn = connect(tmp_path / "t.sqlite3")
    broker = FakeBroker()
    client = FakeAnthropic([text_response("hi")], token_counts=token_counts)
    return AnthropicAgent(conn, broker, CONFIG, client=client)


def test_headroom_passes_well_below_the_reserve(tmp_path) -> None:
    agent = _agent(tmp_path, [1_000])
    assert agent._check_headroom("system", [{"role": "user", "content": "x"}]) is True
    assert HEADROOM_RESERVE_TOKENS == 2_000
    assert MAX_PROMPT_TOKENS - HEADROOM_RESERVE_TOKENS == 14_000


def test_headroom_fails_inside_the_reserve(tmp_path) -> None:
    agent = _agent(tmp_path, [MAX_PROMPT_TOKENS - HEADROOM_RESERVE_TOKENS])
    assert agent._check_headroom("system", [{"role": "user", "content": "x"}]) is False


def test_a_fresh_cycle_prompt_is_under_the_headroom_band(tmp_path) -> None:
    """The playbook + a typical state block must not already consume the reserve."""
    conn = connect(tmp_path / "t.sqlite3")
    broker = FakeBroker()
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    ctx = build_cycle_context(conn, broker, cycle_id=cycle_id)
    client = FakeAnthropic([text_response("hi")], token_counts=[3_000, 3_000])
    agent = AnthropicAgent(conn, broker, CONFIG, client=client)
    result = agent.run(ctx)
    assert result.stop_reason != "budget_headroom"
    assert result.action == "no_action"
