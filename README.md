# day-trader

Agentic paper-trading harness. **Paper trading only** — the Alpaca endpoint is a
single constant in [`src/trader/constants.py`](src/trader/constants.py) and
there is no config flag, env var, or argument in this repo that can change it.
`tests/test_paper_only.py` fails the build if a live endpoint appears anywhere
in `src/`.

Built in the phases laid out in `spec.MD`. **Phases 1 and 2 are complete.**

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
uv run trader risk                 # the loaded limits and the checks that will run
uv run trader rejections           # risk rejections, by check and most recent
uv run trader kill on|off|status   # kill switch (persisted in the DB)
uv run trader --text-logs ...      # human-readable logs instead of JSON lines
```

`status`, `cycles`, `show`, `rejections` and `kill` read only the DB and work
without any Alpaca credentials.

Risk limits live in [`risk.toml`](risk.toml) (stdlib `tomllib`, no YAML
dependency). Loading is strict — an unknown or misspelled key is a fatal error,
because a typo that silently left a limit at its default is the worst thing this
file could do.

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

## Phase 2 — the risk layer

Every order proposal passes through `evaluate()` before it can reach the broker.
The checks in [`src/trader/risk/checks.py`](src/trader/risk/checks.py) are pure
`(proposal, state) -> (bool, reason)` predicates — no DB, no broker, no clock —
so they are unit tested in isolation from everything else.

Run the malicious-proposal battery (no credentials needed):

```bash
uv run python scripts/adversarial_run.py
```

Result: of 12 hostile proposals plus an after-hours order, a kill-switch race,
and 30 rapid-fire attempts, exactly **one** order reached the broker — the
single legitimate one — plus the 5 remaining slots in the hourly rate budget.

| Attack | Rejected by |
| --- | --- |
| Oversized / absurd size | `max_position_notional`, `max_total_exposure` |
| Off-allowlist symbol (any casing) | `symbol_allowlist` |
| Negative / zero / NaN / infinite qty | `proposal_sanity` |
| Zero or negative reference price | `proposal_sanity` |
| Selling more than held | `no_unintended_short` |
| Order while the market is closed | `regular_trading_hours_only` |
| 30 orders in a minute | `max_orders_per_hour` |
| Kill switch thrown mid-cycle | `kill_switch` |
| Position built from legal slices | `max_position_notional` (caps the *result*) |

### How the layer cannot be bypassed

Structurally, not by discipline. [`tests/test_no_bypass.py`](tests/test_no_bypass.py)
AST-scans `src/` and fails the build if any of these stop holding:

- `Broker.submit_order` has exactly one caller: `execution.place_order`
- inside `place_order`, `evaluate()` is called before `submit_order()`
- `place_order` has no `skip`/`force`/`bypass`/`dry_run`/`override` parameter
- `evaluate` defaults to the full `CHECKS` tuple
- the Alpaca SDK is imported in exactly one module, so no second client exists

Three more properties worth knowing:

- **The engine never short-circuits.** All ten checks run on every proposal, so a
  rejection tells the model everything that is wrong in one round trip.
- **A check that raises counts as a failure.** A risk layer that fails open is
  worse than no risk layer.
- **The reference price comes from the broker, never from the model.** If the
  model supplied the price it could understate notional and walk straight
  through every notional cap.

### Checks

| Check | Notes |
| --- | --- |
| `kill_switch` | Re-read from the DB immediately before every order, not taken from the cycle context — a switch thrown mid-cycle still stops the order. |
| `proposal_sanity` | *Not in spec.MD.* Every other check does arithmetic on `qty` and `reference_price`; a NaN qty passes every numeric comparison silently. |
| `symbol_allowlist` | Case-normalised, so casing is not a way around the list. |
| `regular_trading_hours_only` | Reads the broker clock's verdict. `--force` does not override it. |
| `no_unintended_short` | *Not in spec.MD.* "Sell 1000" against 10 held is a 990-share naked short that only `max_position_notional` would catch, and only by accident. Off by default; `[session].allow_shorts` enables it. |
| `max_position_notional` | Caps the **resulting** position, not the order — otherwise an unbounded position can be built from individually legal slices. Sells that reduce an oversized position are still allowed, or we could never unwind one. |
| `max_total_exposure` | Recomputes the traded symbol at the reference price rather than reusing its snapshot, so a stale `market_value` cannot understate the result. |
| `max_daily_loss` | **Latches.** The first breach writes a `daily_loss_halt:<day>` flag; an intraday recovery does not re-enable trading. Fails closed if the prior close is unknown. |
| `max_orders_per_hour` / `max_orders_per_day` | Counted from our own `decisions` rows — the broker has no idea which of its orders came from this harness. An approved order the broker then rejects gets no order id, so it does not consume budget. |

`uv run pytest` — 132 tests. 46 are per-check units (a passing, failing and
boundary case each), 22 are the adversarial battery run through the real
execution path against a real SQLite file.


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
- `flags` is new — the kill switch and the latched daily-loss halt both need
  somewhere that survives a restart and can be flipped from outside the process.

## Not yet built

Phase 3 (the agent) and Phase 4 (evaluation harness). The `prompts/` directory is
empty until Phase 3, and the agent is still the Phase 1 stub — nothing has
proposed a real order yet.
