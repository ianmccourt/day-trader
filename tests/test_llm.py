"""The tool loop, the context budget assertion, and the one-write-tool rule."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.fakes import (
    FakeAnthropic,
    FakeBroker,
    FakeResponse,
    position,
    text_response,
    tool_response,
)
from trader.constants import MAX_PROMPT_TOKENS
from trader.db import connect, open_cycle, utcnow
from trader.llm import MAX_TOOL_ITERATIONS, AnthropicAgent, ContextBudgetExceeded
from trader.risk.config import RiskConfig
from trader.state import build_cycle_context, trading_day_for
from trader.tools import TOOL_SCHEMAS, WRITE_TOOL

CONFIG = RiskConfig(
    max_position_notional=5_000.0,
    max_total_exposure=25_000.0,
    max_daily_loss=2_000.0,
    max_orders_per_hour=6,
    max_orders_per_day=20,
    symbol_allowlist=frozenset({"AAPL", "SPY"}),
)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.sqlite3")


@pytest.fixture
def broker():
    return FakeBroker(prices={"AAPL": 100.0, "SPY": 500.0})


def make(conn, broker, client, **kwargs):
    config = kwargs.pop("config", CONFIG)
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    ctx = build_cycle_context(conn, broker, cycle_id=cycle_id)
    agent = AnthropicAgent(conn, broker, config, client=client, **kwargs)
    return agent, ctx


# --- the loop --------------------------------------------------------------


def test_a_cycle_with_no_tool_use_is_no_action(conn, broker) -> None:
    client = FakeAnthropic([text_response("Nothing to do this cycle.")])
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    assert result.action == "no_action"
    assert result.symbol is None and result.qty is None
    assert result.full_response == "Nothing to do this cycle."
    assert result.tool_calls == []
    assert len(client.requests) == 1


def test_read_tools_run_and_their_results_go_back(conn, broker) -> None:
    client = FakeAnthropic(
        [
            tool_response(("get_risk_limits", {}), ("get_quote", {"symbols": ["AAPL"]})),
            text_response("Read the limits; holding."),
        ]
    )
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    assert [t["name"] for t in result.tool_calls] == ["get_risk_limits", "get_quote"]
    assert not any(t["is_error"] for t in result.tool_calls)
    assert "5000" in result.tool_calls[0]["result"]
    assert json.loads(result.tool_calls[1]["result"]) == {"AAPL": 100.0}

    # Both results came back in a single user message, not split across two.
    second_request = client.requests[1]["messages"]
    assert second_request[-1]["role"] == "user"
    assert len(second_request[-1]["content"]) == 2
    assert all(b["type"] == "tool_result" for b in second_request[-1]["content"])


def test_the_assistant_turn_is_echoed_back_verbatim(conn, broker) -> None:
    client = FakeAnthropic([tool_response(("get_risk_limits", {})), text_response("done")])
    agent, ctx = make(conn, broker, client)
    agent.run(ctx)

    echoed = client.requests[1]["messages"][1]
    assert echoed["role"] == "assistant"
    assert any(getattr(b, "type", None) == "tool_use" for b in echoed["content"])


def test_an_unknown_tool_is_an_error_result_not_a_crash(conn, broker) -> None:
    client = FakeAnthropic([tool_response(("get_the_future", {})), text_response("ok")])
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    assert result.tool_calls[0]["is_error"] is True
    assert "no such tool" in result.tool_calls[0]["result"]


def test_a_malformed_tool_argument_is_an_error_result(conn, broker) -> None:
    client = FakeAnthropic([tool_response(("get_quote", {"symbols": []})), text_response("ok")])
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)
    assert result.tool_calls[0]["is_error"] is True
    assert "non-empty list" in result.tool_calls[0]["result"]


def test_the_loop_terminates_when_the_model_never_stops(conn, broker) -> None:
    client = FakeAnthropic([tool_response(("get_risk_limits", {})) for _ in range(50)])
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    assert len(client.requests) == MAX_TOOL_ITERATIONS
    assert len(result.tool_calls) == MAX_TOOL_ITERATIONS


def test_a_refusal_is_recorded_not_treated_as_a_decision(conn, broker) -> None:
    refusal = FakeResponse(content=[], stop_reason="refusal")
    refusal.stop_details = type("D", (), {"category": "cyber", "explanation": "nope"})()
    agent, ctx = make(conn, broker, FakeAnthropic([refusal]))
    result = agent.run(ctx)

    assert result.action == "no_action"
    assert "model refused" in result.reasoning
    assert result.stop_reason == "refusal"


# --- the single write tool -------------------------------------------------


def test_an_approved_order_executes_and_stores_a_thesis(conn, broker) -> None:
    client = FakeAnthropic(
        [
            tool_response(
                (
                    WRITE_TOOL,
                    {
                        "action": "buy",
                        "symbol": "AAPL",
                        "qty": 10,
                        "reasoning": "because the harness works",
                        "invalidation_condition": "it stops working",
                    },
                )
            ),
            text_response("Order placed."),
        ]
    )
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    assert result.action == "buy" and result.symbol == "AAPL" and result.qty == 10
    assert broker.submitted == [{"symbol": "AAPL", "qty": 10.0, "side": "buy"}]
    assert len(result.executions) == 1 and result.executions[0].executed

    thesis = conn.execute("SELECT * FROM theses").fetchone()
    assert thesis["symbol"] == "AAPL" and thesis["status"] == "open"
    assert thesis["rationale"] == "because the harness works"
    assert thesis["invalidation_condition"] == "it stops working"

    decision = conn.execute("SELECT * FROM decisions").fetchone()
    assert decision["risk_result"] == "approved"
    assert decision["broker_order_id"] == "fake-order-1"


def test_a_rejected_order_returns_the_verdict_and_writes_no_thesis(conn, broker) -> None:
    client = FakeAnthropic(
        [
            tool_response(
                (
                    WRITE_TOOL,
                    {
                        "action": "buy",
                        "symbol": "DOGE",
                        "qty": 10,
                        "reasoning": "r",
                        "invalidation_condition": "i",
                    },
                )
            ),
            text_response("Understood, rejected."),
        ]
    )
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    payload = json.loads(result.tool_calls[0]["result"])
    assert payload["approved"] is False and payload["executed"] is False
    assert "symbol_allowlist" in payload["failed_checks"]
    assert "REJECTED" in payload["message"]
    assert broker.submitted == []
    assert conn.execute("SELECT COUNT(*) FROM theses").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] >= 1


def test_only_one_order_may_be_proposed_per_cycle(conn, broker) -> None:
    order = (
        WRITE_TOOL,
        {
            "action": "buy",
            "symbol": "AAPL",
            "qty": 1,
            "reasoning": "r",
            "invalidation_condition": "i",
        },
    )
    client = FakeAnthropic(
        [tool_response(order), tool_response(order), text_response("stopping")]
    )
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    assert len(broker.submitted) == 1
    assert result.tool_calls[1]["is_error"] is True
    assert "already proposed" in result.tool_calls[1]["result"]
    assert "at most 1" in result.tool_calls[1]["result"]


def test_two_orders_in_one_assistant_turn_still_yield_one(conn, broker) -> None:
    """Parallel tool calls must not be a way around the one-order rule."""
    order = (
        WRITE_TOOL,
        {
            "action": "buy",
            "symbol": "AAPL",
            "qty": 1,
            "reasoning": "r",
            "invalidation_condition": "i",
        },
    )
    client = FakeAnthropic([tool_response(order, order), text_response("done")])
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    assert len(broker.submitted) == 1
    assert result.tool_calls[1]["is_error"] is True


def test_two_orders_execute_when_the_per_cycle_cap_allows(conn, broker) -> None:
    order = (
        WRITE_TOOL,
        {
            "action": "buy",
            "symbol": "AAPL",
            "qty": 1,
            "reasoning": "r",
            "invalidation_condition": "i",
        },
    )
    client = FakeAnthropic([tool_response(order, order), text_response("done")])
    agent, ctx = make(conn, broker, client, config=replace(CONFIG, max_orders_per_cycle=3))
    result = agent.run(ctx)

    assert len(broker.submitted) == 2
    assert not any(t["is_error"] for t in result.tool_calls if t["name"] == WRITE_TOOL)
    assert len(result.executions) == 2


def test_the_second_order_sees_the_fill_from_the_first(conn, broker) -> None:
    """40 sh @ $100 = $4,000; a further 20 would be $6,000, over the $5,000 cap."""
    first = (
        WRITE_TOOL,
        {
            "action": "buy",
            "symbol": "AAPL",
            "qty": 40,
            "reasoning": "scale in",
            "invalidation_condition": "i",
        },
    )
    second = (
        WRITE_TOOL,
        {
            "action": "buy",
            "symbol": "AAPL",
            "qty": 20,
            "reasoning": "too much",
            "invalidation_condition": "i",
        },
    )
    client = FakeAnthropic([tool_response(first, second), text_response("done")])
    agent, ctx = make(conn, broker, client, config=replace(CONFIG, max_orders_per_cycle=3))
    result = agent.run(ctx)

    assert broker.submitted == [{"symbol": "AAPL", "qty": 40.0, "side": "buy"}]
    payload = json.loads(result.tool_calls[1]["result"])
    assert payload["approved"] is False
    assert "max_position_notional" in payload["failed_checks"]


def test_a_stop_rides_along_with_an_executed_order(conn, broker) -> None:
    client = FakeAnthropic(
        [
            tool_response(
                (
                    WRITE_TOOL,
                    {
                        "action": "buy",
                        "symbol": "AAPL",
                        "qty": 10,
                        "stop_price": 95.0,
                        "take_profit_price": 110.0,
                        "reasoning": "with protection",
                        "invalidation_condition": "stop hits",
                    },
                )
            ),
            text_response("Protected."),
        ]
    )
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    assert broker.submitted == [
        {
            "symbol": "AAPL",
            "qty": 10.0,
            "side": "buy",
            "stop_price": 95.0,
            "take_profit_price": 110.0,
        }
    ]
    assert "stop=95" in result.executions[0].as_model_message()
    assert "take_profit=110" in result.executions[0].as_model_message()


def test_selling_a_whole_position_closes_its_thesis(conn, broker) -> None:
    broker.positions = [position("AAPL", 10, 100.0, 100.0)]
    conn.execute(
        "INSERT INTO theses(symbol, opened_at, rationale, invalidation_condition, status) "
        "VALUES ('AAPL', datetime('now'), 'why', 'when', 'open')"
    )
    client = FakeAnthropic(
        [
            tool_response(
                (
                    WRITE_TOOL,
                    {
                        "action": "sell",
                        "symbol": "AAPL",
                        "qty": 10,
                        "reasoning": "closing",
                        "invalidation_condition": "n/a",
                    },
                )
            ),
            text_response("Closed."),
        ]
    )
    agent, ctx = make(conn, broker, client)
    agent.run(ctx)
    assert conn.execute("SELECT status FROM theses").fetchone()["status"] == "closed"


def test_a_partial_sell_leaves_the_thesis_open(conn, broker) -> None:
    broker.positions = [position("AAPL", 10, 100.0, 100.0)]
    conn.execute(
        "INSERT INTO theses(symbol, opened_at, rationale, invalidation_condition, status) "
        "VALUES ('AAPL', datetime('now'), 'why', 'when', 'open')"
    )
    client = FakeAnthropic(
        [
            tool_response(
                (
                    WRITE_TOOL,
                    {
                        "action": "sell",
                        "symbol": "AAPL",
                        "qty": 4,
                        "reasoning": "trimming",
                        "invalidation_condition": "n/a",
                    },
                )
            ),
            text_response("Trimmed."),
        ]
    )
    agent, ctx = make(conn, broker, client)
    agent.run(ctx)
    assert conn.execute("SELECT status FROM theses").fetchone()["status"] == "open"


# --- the context budget ----------------------------------------------------


def test_the_budget_is_checked_before_the_first_api_call(conn, broker) -> None:
    client = FakeAnthropic([text_response("hi")], token_counts=[MAX_PROMPT_TOKENS + 1])
    agent, ctx = make(conn, broker, client)

    with pytest.raises(ContextBudgetExceeded, match="over the 10000 budget"):
        agent.run(ctx)
    assert client.requests == []  # nothing was sent


def test_the_budget_is_checked_on_every_iteration_not_just_the_first(conn, broker) -> None:
    """Tool results accumulate, so the last iteration is the dangerous one."""
    client = FakeAnthropic(
        [tool_response(("get_risk_limits", {})), text_response("done")],
        token_counts=[500, MAX_PROMPT_TOKENS + 1],
    )
    agent, ctx = make(conn, broker, client)

    with pytest.raises(ContextBudgetExceeded, match="iteration 1"):
        agent.run(ctx)
    assert len(client.requests) == 1  # the first call went out, the second did not


def test_the_budget_boundary_is_inclusive(conn, broker) -> None:
    client = FakeAnthropic([text_response("hi")], token_counts=[MAX_PROMPT_TOKENS])
    agent, ctx = make(conn, broker, client)
    assert agent.run(ctx).action == "no_action"  # exactly at the budget is allowed


def test_the_count_is_of_the_real_assembled_prompt(conn, broker) -> None:
    """System, messages and tools all go into the count — not just the user turn."""
    client = FakeAnthropic([text_response("hi")])
    agent, ctx = make(conn, broker, client)
    agent.run(ctx)

    call = client.count_calls[0]
    assert call["model"] == agent.model
    assert call["system"].startswith("You are the decision-making component")
    assert call["tools"] == TOOL_SCHEMAS
    assert call["messages"] == client.requests[0]["messages"]


def test_peak_prompt_tokens_are_logged_for_every_cycle(conn, broker) -> None:
    client = FakeAnthropic(
        [tool_response(("get_risk_limits", {})), text_response("done")],
        token_counts=[400, 900],
    )
    agent, ctx = make(conn, broker, client)
    logged = json.loads(agent.run(ctx).full_prompt)
    assert logged["peak_prompt_tokens"] == 900
    assert logged["budget"] == MAX_PROMPT_TOKENS


# --- request shape ---------------------------------------------------------


def test_the_write_tool_is_listed_last(conn, broker) -> None:
    """Read tools first, then the single write tool (SPEC.md Phase 3)."""
    assert TOOL_SCHEMAS[-1]["name"] == WRITE_TOOL
    assert sum(1 for t in TOOL_SCHEMAS if t["name"] == WRITE_TOOL) == 1


def test_the_tool_list_is_identical_every_cycle(conn, broker) -> None:
    """A varying tool block would silently break prompt caching."""
    first = FakeAnthropic([text_response("a")])
    agent, ctx = make(conn, broker, first)
    agent.run(ctx)
    second = FakeAnthropic([text_response("b")])
    agent2, ctx2 = make(conn, broker, second)
    agent2.run(ctx2)
    assert first.requests[0]["tools"] == second.requests[0]["tools"]


def test_thinking_and_effort_are_configured(conn, broker) -> None:
    client = FakeAnthropic([text_response("hi")])
    agent, ctx = make(conn, broker, client, effort="low")
    agent.run(ctx)
    request = client.requests[0]
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "low"}
    assert "budget_tokens" not in json.dumps(request.get("thinking"))


def test_thinking_can_be_disabled(conn, broker) -> None:
    client = FakeAnthropic([text_response("hi")])
    agent, ctx = make(conn, broker, client, thinking=False)
    agent.run(ctx)
    assert "thinking" not in client.requests[0]


def test_the_full_prompt_log_captures_the_whole_conversation(conn, broker) -> None:
    client = FakeAnthropic([tool_response(("get_risk_limits", {})), text_response("done")])
    agent, ctx = make(conn, broker, client)
    logged = json.loads(agent.run(ctx).full_prompt)

    assert "system" in logged
    assert [m["role"] for m in logged["messages"]] == ["user", "assistant", "user"]
    assert logged["tools"] == [t["name"] for t in TOOL_SCHEMAS]


def test_token_usage_accumulates_across_iterations(conn, broker) -> None:
    client = FakeAnthropic([tool_response(("get_risk_limits", {})), text_response("done")])
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)
    assert result.prompt_tokens == 200  # two calls at 100
    assert result.completion_tokens == 100


# --- the tool-result budget -------------------------------------------------
#
# Per-result truncation alone does not bound the loop: several iterations of
# parallel calls are still tens of thousands of characters, and every one of
# them is input tokens on the next request.


def test_a_single_tool_result_is_truncated(conn, broker) -> None:
    from trader.llm import MAX_TOOL_RESULT_CHARS

    broker.positions = [position(f"S{i:03d}", 1, 1.0) for i in range(200)]
    client = FakeAnthropic(
        [tool_response(("get_recent_decisions", {"limit": 20})), text_response("ok")]
    )
    agent, ctx = make(conn, broker, client)
    for i in range(200):
        conn.execute(
            "INSERT INTO decisions(cycle_id, timestamp, action, symbol, qty, reasoning) "
            "VALUES (?, ?, 'buy', 'AAPL', 1, ?)",
            (ctx.cycle_id, f"2026-09-09T10:00:0{i % 10}+00:00", "x" * 500),
        )
    agent.run(ctx)
    result_text = client.requests[1]["messages"][-1]["content"][0]["content"]
    assert len(result_text) <= MAX_TOOL_RESULT_CHARS + 20


def test_the_cumulative_tool_budget_stops_a_runaway_loop(conn, broker) -> None:
    from trader.llm import MAX_TOTAL_TOOL_RESULT_CHARS

    broker.positions = [position(f"S{i:03d}", 1, 1.0) for i in range(50)]
    # Six iterations, three calls each — well past the cumulative budget.
    client = FakeAnthropic(
        [tool_response(*[("get_risk_limits", {})] * 3) for _ in range(MAX_TOOL_ITERATIONS)]
    )
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)

    exhausted = [t for t in result.tool_calls if "tool-output budget is exhausted" in t["result"]]
    substantive = [t for t in result.tool_calls if t not in exhausted]

    # Real tool output stops at the budget...
    assert sum(len(t["result"]) for t in substantive) <= MAX_TOTAL_TOOL_RESULT_CHARS
    # ...and what replaces it is a short note, not silence, so the model knows
    # why its tool stopped answering. Notes are counted against the budget too,
    # so the total stays bounded by a small constant per remaining call.
    assert exhausted and all(t["is_error"] for t in exhausted)
    assert all(len(t["result"]) < 200 for t in exhausted)
    assert sum(len(t["result"]) for t in result.tool_calls) < 2 * MAX_TOTAL_TOOL_RESULT_CHARS


def test_the_budget_does_not_fire_on_a_normal_cycle(conn, broker) -> None:
    """The cap must be generous enough that ordinary use never sees it."""
    client = FakeAnthropic(
        [
            tool_response(("get_risk_limits", {}), ("get_quote", {"symbols": ["AAPL"]})),
            tool_response(("get_bars", {"symbol": "AAPL", "timeframe": "1Day", "limit": 10})),
            text_response("holding"),
        ]
    )
    agent, ctx = make(conn, broker, client)
    result = agent.run(ctx)
    assert not any("budget is exhausted" in t["result"] for t in result.tool_calls)
    assert not any(t["is_error"] for t in result.tool_calls)
