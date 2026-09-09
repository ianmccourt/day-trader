"""The trigger must only fire on weekday RTH; the broker clock does the rest."""

from __future__ import annotations

from datetime import datetime, timedelta

from trader.constants import MARKET_TZ
from trader.scheduler import rth_trigger


def _fire_times(cycle_minutes: int, start: datetime, hours: int) -> list[datetime]:
    trigger = rth_trigger(cycle_minutes)
    times: list[datetime] = []
    prev, now = None, start
    end = start + timedelta(hours=hours)
    while True:
        nxt = trigger.get_next_fire_time(prev, now)
        if nxt is None or nxt > end:
            return times
        times.append(nxt)
        prev, now = nxt, nxt + timedelta(seconds=1)


def test_fires_every_n_minutes_during_the_session() -> None:
    start = datetime(2026, 9, 9, 9, 0, tzinfo=MARKET_TZ)  # a Wednesday
    times = _fire_times(15, start, hours=8)
    assert times[0] == datetime(2026, 9, 9, 9, 0, tzinfo=MARKET_TZ)
    assert all(t.minute % 15 == 0 for t in times)
    # Last fire is inside the session; nothing after the close.
    assert max(times).hour == 15 and max(times).minute == 45


def test_never_fires_on_a_weekend() -> None:
    saturday = datetime(2026, 9, 12, 0, 0, tzinfo=MARKET_TZ)
    times = _fire_times(15, saturday, hours=72)
    assert all(t.weekday() < 5 for t in times)
    assert times and times[0].weekday() == 0  # rolls forward to Monday


def test_never_fires_overnight() -> None:
    start = datetime(2026, 9, 9, 0, 0, tzinfo=MARKET_TZ)
    for t in _fire_times(5, start, hours=24):
        assert 9 <= t.hour < 16, t


def test_cycle_minutes_changes_cadence() -> None:
    start = datetime(2026, 9, 9, 9, 0, tzinfo=MARKET_TZ)
    # Inclusive of both endpoints: 09:00 through 10:00.
    assert len(_fire_times(5, start, hours=1)) == 13
    assert len(_fire_times(30, start, hours=1)) == 3
