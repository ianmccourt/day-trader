# day-trader

Agentic paper-trading harness. **Paper trading only** — the Alpaca endpoint is a
single constant in [`src/trader/constants.py`](src/trader/constants.py) and
there is no config flag, env var, or argument in this repo that can change it.
`tests/test_paper_only.py` fails the build if a live endpoint appears anywhere
in `src/`.

Built in the phases laid out in `spec.MD`. **Phase 1 is complete.**

## Setup

```bash
uv sync
cp .env.example .env    # then fill in your Alpaca *paper* keys
```

## Commands

```bash
uv run trader run                  # start the scheduled loop (blocking)
uv run trader cycle [--force]      # run one cycle now; --force ignores market-closed
uv run trader status               # today's session, reconstructed from the DB
uv run trader cycles --limit 25    # recent cycles, one line each
uv run trader show <cycle_id>      # full prompt + response for one cycle
uv run trader kill on|off|status   # kill switch (persisted in the DB)
uv run trader --text-logs ...      # human-readable logs instead of JSON lines
```

`status`, `cycles`, `show` and `kill` read only the DB and work without any
Alpaca credentials.

## Phase 1 — loop without intelligence

The agent is a stub that always returns `no_action`; there is no order
submission code anywhere yet, so "no order path outside the risk layer" holds by
construction rather than by discipline.

Run the end-to-end proof (no credentials needed — uses the fake broker):

```bash
uv run python scripts/simulate_session.py
```

It runs a full 26-cycle session, hard-kills the process mid-cycle, restarts
against the same SQLite file, reconciles the orphaned cycle, finishes the
session, and prints state rebuilt from the DB alone.

What that demonstrates:

| Requirement | Evidence |
| --- | --- |
| Loop runs a full session | 26 `ok` cycles, contiguous ids |
| Survives a restart mid-day | orphan cycle → `interrupted`; ids continue, history intact |
| State reconstructed from the DB | `trader status` makes zero broker calls |
| Context stays flat | prompt grows 573 → 1552 chars, then plateaus within 1 char |
| Full logging | `full_prompt`, `full_response`, `tool_calls`, tokens, duration per cycle |

`uv run pytest` — 33 tests, covering the paper-only guard, schema and kill
switch, cycle outcomes (market closed, broker failure, agent exception, restart),
context bounding, and the RTH trigger.

## Architecture notes

- **Short independent invocations, not one conversation.** APScheduler fires
  every N minutes on weekday RTH hours; each cycle builds its context from
  scratch and tears it down. Nothing carries over in memory.
- **The broker clock is authoritative.** The cron trigger is deliberately wider
  than the session; `run_cycle` asks Alpaca whether the market is actually open,
  which makes holidays and early closes correct without a local calendar.
- **Positions are never trusted from memory.** Read fresh from the broker every
  cycle and snapshotted with the cycle id.
- **Cycle rows are written before the work happens**, so a crash always leaves
  evidence. Startup reconciles anything left `running` to `interrupted`.
- **Rate limits count our own submitted orders**, from the `decisions` table —
  the broker has no idea which of its orders came from this harness.

## Schema deviations from spec.MD

Nothing was dropped or renamed. Additions:

- `positions_snapshot` gains `cycle_id`, `current_price`, `market_value` — the
  Phase 2 exposure checks need notional, and Phase 4 needs to join a snapshot to
  the cycle that saw it.
- `cycles` gains `trading_day`, `ended_at`, `status` — so a restart can ask
  "what happened today" with an index hit instead of a date-parsing scan.
- `account_snapshot` is new — `max_daily_loss` needs an equity series, and
  positions alone do not give one.
- `flags` is new — the kill switch needs somewhere that survives a restart and
  can be flipped from outside the process.

## Not yet built

Phase 2 (risk layer), Phase 3 (the agent), Phase 4 (evaluation harness). The
`prompts/` directory is empty until Phase 3.
