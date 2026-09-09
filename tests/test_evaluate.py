"""Phase 4. The failure mode to guard against is a report that looks plausible
and is wrong — a win rate built from prices nobody checked, or a benchmark
measured over a different window than the strategy."""

from __future__ import annotations

import sqlite3

import pytest

from trader.db import close_cycle, connect, open_cycle
from trader.evaluate import BENCHMARK_SYMBOL, EvaluationError, evaluate


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.sqlite3")


class BarBroker:
    """Serves one fixed SPY daily series. Nothing else is needed for the benchmark."""

    def __init__(self, closes: list[tuple[str, float, float]]) -> None:
        # (date, open, close)
        self.closes = closes
        self.calls: list[tuple[str, str, int]] = []

    def get_bars(self, symbol: str, *, timeframe: str, limit: int) -> list[dict[str, object]]:
        self.calls.append((symbol, timeframe, limit))
        return [
            {"t": f"{d}T04:00:00+00:00", "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 1.0}
            for d, o, c in self.closes
        ]


def add_cycle(
    conn: sqlite3.Connection, day: str, equity: float, *, status: str = "ok"
) -> int:
    cycle_id = open_cycle(conn, started_at=_ts(day, 10), trading_day=day)
    conn.execute(
        "INSERT INTO account_snapshot(cycle_id, equity, last_equity, cash, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (cycle_id, equity, equity, equity, f"{day}T14:00:00+00:00"),
    )
    close_cycle(conn, cycle_id, status=status, duration_ms=1, prompt_tokens=100,
                completion_tokens=20)
    return cycle_id


def _ts(day: str, hour: int):
    from datetime import UTC, datetime

    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").astimezone(UTC)


def add_order(
    conn: sqlite3.Connection,
    cycle_id: int,
    day: str,
    hour: int,
    action: str,
    symbol: str,
    qty: float,
    price: float,
    *,
    status: str | None = "filled",
    reference_price: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO decisions(cycle_id, timestamp, action, symbol, qty, risk_result, "
        "broker_order_id, outcome, reference_price, filled_qty, filled_avg_price, "
        "filled_at, final_status) "
        "VALUES (?, ?, ?, ?, ?, 'approved', ?, 'accepted', ?, ?, ?, ?, ?)",
        (
            cycle_id,
            f"{day}T{hour:02d}:00:00+00:00",
            action,
            symbol,
            qty,
            f"order-{symbol}-{day}-{hour}",
            reference_price if reference_price is not None else price,
            qty if status == "filled" else None,
            price if status == "filled" else None,
            f"{day}T{hour:02d}:00:00+00:00" if status == "filled" else None,
            status,
        ),
    )


def add_rejection(conn: sqlite3.Connection, cycle_id: int, day: str, check: str) -> None:
    conn.execute(
        "INSERT INTO decisions(cycle_id, timestamp, action, symbol, qty, risk_result, outcome) "
        "VALUES (?, ?, 'buy', 'AAPL', 1, ?, 'rejected')",
        (cycle_id, f"{day}T11:00:00+00:00", f"rejected:{check}"),
    )
    conn.execute(
        "INSERT INTO risk_events(cycle_id, timestamp, check_name, reason, proposal) "
        "VALUES (?, ?, ?, 'because', '{}')",
        (cycle_id, f"{day}T11:00:00+00:00", check),
    )


# --- window handling -------------------------------------------------------


def test_an_empty_window_is_an_error_not_a_zeroed_report(conn) -> None:
    with pytest.raises(EvaluationError, match="no cycles logged"):
        evaluate(conn, "2026-09-01", "2026-09-05")


def test_a_backwards_window_is_rejected(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    with pytest.raises(EvaluationError, match="after end"):
        evaluate(conn, "2026-09-05", "2026-09-01")


def test_cycles_outside_the_window_are_excluded(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    add_cycle(conn, "2026-09-02", 101_000)
    add_cycle(conn, "2026-09-03", 102_000)
    report = evaluate(conn, "2026-09-02", "2026-09-02")
    assert report.cycles == 1
    assert report.start_equity == 101_000 and report.end_equity == 101_000


# --- return ----------------------------------------------------------------


def test_strategy_return_brackets_the_window(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    add_cycle(conn, "2026-09-02", 103_000)
    add_cycle(conn, "2026-09-03", 102_000)
    report = evaluate(conn, "2026-09-01", "2026-09-03")
    assert report.strategy_return_pct == pytest.approx(2.0)
    assert report.excess_vs_cash_pct == pytest.approx(2.0)


def test_the_benchmark_is_measured_over_the_same_window(conn) -> None:
    for day, equity in [("2026-09-01", 100_000), ("2026-09-02", 101_000)]:
        add_cycle(conn, day, equity)
    broker = BarBroker(
        [
            ("2026-08-31", 500.0, 500.0),  # before the window — must be ignored
            ("2026-09-01", 600.0, 610.0),
            ("2026-09-02", 610.0, 630.0),
            ("2026-09-03", 630.0, 900.0),  # after the window — must be ignored
        ]
    )
    report = evaluate(conn, "2026-09-01", "2026-09-02", broker=broker)
    assert report.benchmark_symbol == BENCHMARK_SYMBOL
    assert report.benchmark_start_price == 600.0
    assert report.benchmark_end_price == 630.0
    assert report.benchmark_return_pct == pytest.approx(5.0)
    assert report.excess_vs_benchmark_pct == pytest.approx(1.0 - 5.0)


def test_no_broker_means_no_benchmark_not_a_zero(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert report.benchmark_return_pct is None
    assert "benchmark unavailable" in (report.benchmark_note or "")
    assert report.excess_vs_benchmark_pct is None


def test_a_single_session_says_so_rather_than_reporting_zero(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    broker = BarBroker([("2026-09-01", 600.0, 606.0)])
    report = evaluate(conn, "2026-09-01", "2026-09-01", broker=broker)
    assert "needs at least 2" in (report.benchmark_note or "")
    assert report.benchmark_return_pct == pytest.approx(1.0)  # open-to-close fallback


def test_a_broker_failure_degrades_the_benchmark_not_the_report(conn) -> None:
    from trader.broker import BrokerError

    class Broken(BarBroker):
        def get_bars(self, *a, **k):
            raise BrokerError("data feed down")

    add_cycle(conn, "2026-09-01", 100_000)
    add_cycle(conn, "2026-09-02", 101_000)
    report = evaluate(conn, "2026-09-01", "2026-09-02", broker=Broken([]))
    assert "data feed down" in (report.benchmark_note or "")
    assert report.strategy_return_pct == pytest.approx(1.0)  # the rest still works


# --- round trips -----------------------------------------------------------


def test_a_buy_and_a_sell_make_one_round_trip(conn) -> None:
    c1 = add_cycle(conn, "2026-09-01", 100_000)
    c2 = add_cycle(conn, "2026-09-02", 101_000)
    add_order(conn, c1, "2026-09-01", 10, "buy", "AAPL", 10, 100.0)
    add_order(conn, c2, "2026-09-02", 16, "sell", "AAPL", 10, 110.0)

    report = evaluate(conn, "2026-09-01", "2026-09-02")
    assert len(report.round_trips) == 1
    trip = report.round_trips[0]
    assert trip.realized_pl == pytest.approx(100.0)
    assert trip.is_win
    assert trip.holding_hours == pytest.approx(30.0)
    assert report.win_rate_pct == 100.0
    assert report.avg_holding_hours == pytest.approx(30.0)
    assert report.realized_pl == pytest.approx(100.0)
    assert report.open_at_end == {}


def test_a_loss_is_counted_as_a_loss(conn) -> None:
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 10, 100.0)
    add_order(conn, c, "2026-09-01", 14, "sell", "AAPL", 10, 90.0)
    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert report.win_rate_pct == 0.0
    assert report.realized_pl == pytest.approx(-100.0)


def test_lots_are_matched_first_in_first_out(conn) -> None:
    """FIFO, so the first sell unwinds the first buy — not the cheapest one."""
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 5, 100.0)
    add_order(conn, c, "2026-09-01", 11, "buy", "AAPL", 5, 200.0)
    add_order(conn, c, "2026-09-01", 12, "sell", "AAPL", 5, 150.0)

    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert len(report.round_trips) == 1
    assert report.round_trips[0].entry_price == 100.0  # the older lot
    assert report.realized_pl == pytest.approx(250.0)
    assert report.open_at_end == {"AAPL": 5}


def test_a_sell_can_span_several_lots(conn) -> None:
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 3, 100.0)
    add_order(conn, c, "2026-09-01", 11, "buy", "AAPL", 7, 110.0)
    add_order(conn, c, "2026-09-01", 12, "sell", "AAPL", 10, 120.0)

    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert [t.qty for t in report.round_trips] == [3, 7]
    assert report.realized_pl == pytest.approx(3 * 20 + 7 * 10)
    assert report.open_at_end == {}


def test_a_partial_sell_leaves_the_rest_of_the_lot_open(conn) -> None:
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 10, 100.0)
    add_order(conn, c, "2026-09-01", 12, "sell", "AAPL", 4, 110.0)

    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert len(report.round_trips) == 1 and report.round_trips[0].qty == 4
    assert report.open_at_end == {"AAPL": 6}


def test_a_sell_with_no_matching_buy_is_skipped_not_guessed(conn) -> None:
    """The position predates the window; inventing an entry price would put a
    fabricated number straight into the win rate."""
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 12, "sell", "AAPL", 10, 110.0)
    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert report.round_trips == []
    assert report.win_rate_pct is None
    assert report.realized_pl == 0.0


def test_symbols_do_not_cross_match(conn) -> None:
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 10, 100.0)
    add_order(conn, c, "2026-09-01", 12, "sell", "MSFT", 10, 110.0)
    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert report.round_trips == []
    assert report.open_at_end == {"AAPL": 10}


def test_a_canceled_order_is_not_treated_as_a_fill(conn) -> None:
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 10, 100.0, status="canceled")
    add_order(conn, c, "2026-09-01", 12, "sell", "AAPL", 10, 110.0)
    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert report.orders_submitted == 2
    assert report.orders_filled == 1
    assert report.round_trips == []  # the buy never happened


def test_an_unreconciled_order_falls_back_to_the_reference_price(conn) -> None:
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 10, 0, status=None,
              reference_price=100.0)
    add_order(conn, c, "2026-09-01", 12, "sell", "AAPL", 10, 110.0)
    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert report.orders_unreconciled == 1
    assert report.prices_estimated == 1  # the caveat is surfaced, not hidden
    assert report.round_trips[0].entry_price == 100.0


# --- rejections and cycles -------------------------------------------------


def test_rejections_are_counted_and_broken_down(conn) -> None:
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_rejection(conn, c, "2026-09-01", "symbol_allowlist")
    add_rejection(conn, c, "2026-09-01", "symbol_allowlist")
    add_rejection(conn, c, "2026-09-01", "max_position_notional")
    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert report.rejections == 3
    assert report.rejections_by_check == {"symbol_allowlist": 2, "max_position_notional": 1}
    assert report.proposals == 3


def test_cycle_statuses_and_tokens_are_summarised(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    add_cycle(conn, "2026-09-01", 100_000, status="error")
    add_cycle(conn, "2026-09-01", 100_000, status="skipped_market_closed")
    report = evaluate(conn, "2026-09-01", "2026-09-01")
    assert report.cycles == 3
    assert report.error_cycles == 1
    assert report.cycles_by_status["skipped_market_closed"] == 1
    assert report.prompt_tokens == 300 and report.completion_tokens == 60


# --- the verdict -----------------------------------------------------------


def test_the_verdict_says_matched_when_the_return_is_flat(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 1, 100.0)
    assert "matched cash" in evaluate(conn, "2026-09-01", "2026-09-01").verdict


def test_the_verdict_is_honest_when_nothing_was_traded(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    add_cycle(conn, "2026-09-02", 110_000)
    verdict = evaluate(conn, "2026-09-01", "2026-09-02").verdict
    assert "placed no orders" in verdict  # a 10% drift is not the agent's doing


def test_the_verdict_reports_both_comparisons(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    c = add_cycle(conn, "2026-09-02", 110_000)
    add_order(conn, c, "2026-09-02", 10, "buy", "AAPL", 1, 100.0)
    broker = BarBroker([("2026-09-01", 600.0, 600.0), ("2026-09-02", 600.0, 630.0)])
    verdict = evaluate(conn, "2026-09-01", "2026-09-02", broker=broker).verdict
    # +10% strategy against a +5% benchmark: up on the day and ahead of SPY.
    assert "beat cash (+10.00%)" in verdict
    assert "beat buy-and-hold SPY (+5.00% excess)" in verdict


def test_the_verdict_admits_losing_to_the_benchmark(conn) -> None:
    """A profitable window can still be a worse outcome than holding SPY."""
    add_cycle(conn, "2026-09-01", 100_000)
    c = add_cycle(conn, "2026-09-02", 102_000)
    add_order(conn, c, "2026-09-02", 10, "buy", "AAPL", 1, 100.0)
    broker = BarBroker([("2026-09-01", 600.0, 600.0), ("2026-09-02", 600.0, 660.0)])
    verdict = evaluate(conn, "2026-09-01", "2026-09-02", broker=broker).verdict
    assert "beat cash (+2.00%)" in verdict
    assert "lost to buy-and-hold SPY (-8.00% excess)" in verdict


def test_the_verdict_admits_losing_money(conn) -> None:
    add_cycle(conn, "2026-09-01", 100_000)
    c = add_cycle(conn, "2026-09-02", 97_000)
    add_order(conn, c, "2026-09-02", 10, "buy", "AAPL", 1, 100.0)
    assert "lost to cash (-3.00%)" in evaluate(conn, "2026-09-01", "2026-09-02").verdict


def test_the_report_serialises_for_json_output(conn) -> None:
    import json

    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 10, 100.0)
    add_order(conn, c, "2026-09-01", 12, "sell", "AAPL", 10, 110.0)
    payload = json.loads(json.dumps(evaluate(conn, "2026-09-01", "2026-09-01").to_dict()))
    assert payload["win_rate_pct"] == 100.0
    assert payload["round_trips"][0]["realized_pl"] == 100.0
    assert "verdict" in payload


def test_evaluation_is_reproducible(conn) -> None:
    """Same window twice must give the same answer — it re-runs nothing."""
    c = add_cycle(conn, "2026-09-01", 100_000)
    add_order(conn, c, "2026-09-01", 10, "buy", "AAPL", 10, 100.0)
    add_order(conn, c, "2026-09-01", 12, "sell", "AAPL", 10, 110.0)
    first = evaluate(conn, "2026-09-01", "2026-09-01").to_dict()
    second = evaluate(conn, "2026-09-01", "2026-09-01").to_dict()
    assert first == second
