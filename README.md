# day-trader

Agentic paper-trading harness. **Paper trading only** — the Alpaca endpoint is a
single constant in [`src/trader/constants.py`](src/trader/constants.py) and
there is no config flag, env var, or argument in this repo that can change it.
`tests/test_paper_only.py` fails the build if a live endpoint appears anywhere
in `src/`.

Built in the phases laid out in `spec.MD`. **All four phases are complete.**

## Setup

```bash
uv sync
cp .env.example .env    # then fill in your Alpaca *paper* keys
```

## Commands

```bash
uv run trader run                  # start the scheduled loop (blocking)
uv run trader cycle [--force]      # run one cycle now; --force ignores market-closed
uv run trader cycle --stub         # run a cycle with the Phase 1 stub, no model call
uv run trader status               # today's session, reconstructed from the DB
uv run trader cycles --limit 25    # recent cycles, one line each
uv run trader show <cycle_id>      # full prompt + response for one cycle
uv run trader risk                 # the loaded limits and the checks that will run
uv run trader rejections           # risk rejections, by check and most recent
uv run trader reconcile            # read submitted orders back from the broker
uv run trader evaluate --start ... --end ...   # did it beat doing nothing?
uv run trader kill on|off|status   # kill switch (persisted in the DB)
uv run trader --text-logs ...      # human-readable logs instead of JSON lines
```

`status`, `cycles`, `show`, `rejections`, `kill` and `evaluate` read only the DB
and work without any Alpaca credentials (`evaluate` drops the SPY benchmark and
says so).

`--stub` runs the Phase 1 stub agent instead of the model (no Anthropic key
needed). `--effort low|medium|high|xhigh|max` and `--no-thinking` tune the model call.

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

`uv run pytest` — 46 per-check units (a passing, failing and boundary case
each), and 22 adversarial cases run through the real execution path against a
real SQLite file.


## Phase 3 — the agent

`claude-sonnet-5` via the `anthropic` SDK, in a **manual** tool loop —
not the SDK's tool runner. The loop has to interpose the risk layer between the
model's proposal and the broker and hand the verdict back as a tool result, and
spec.MD asks to own every failure mode.

Prompt text lives in [`prompts/`](prompts) as plain `.txt` files; a test fails
the build if prompt text appears inline in `src/`. The system prompt is a
**placeholder** — it states what the agent is, what it can call, and what will
stop it, and contains no strategy. A test asserts no strategy hints leaked in.

### The tool surface

Read tools first, then the single write tool — in that order in the tool list,
which is also fixed across cycles so the tool block stays cacheable.

| Tool | |
| --- | --- |
| `get_risk_limits` | the limits, the checks that will run, rate budget used |
| `get_quote` | latest trade price, up to 5 symbols |
| `get_bars` | recent OHLCV, capped at 30 bars |
| `get_theses` | stored theses in full (the rendered state truncates) |
| `get_recent_decisions` | decision history beyond the rendered window |
| **`place_order`** | **the only tool that writes anything** |

`place_order` calls `execution.place_order` — the same single gated path from
Phase 2. Enforced structurally: `tests/test_no_bypass.py` fails the build if a
second tool calls it, if any read tool contains an `INSERT`/`UPDATE`/`DELETE`,
if `tools.py` imports `subprocess`/`requests`/`httpx`/`socket`, or if anything
rebinds the risk config.

At most one order per cycle. A second call — including a second `place_order`
block in the *same* assistant turn — comes back as an error result, not an
order.

### The context budget

`MAX_PROMPT_TOKENS = 10_000`, asserted before **every** request in the loop, not
just the first — tool results accumulate, so the last iteration is the one that
would blow it. Counted through the API's own `count_tokens` with the real
system, messages, and tools; a local estimate that drifts from the server's
count would make the assertion meaningless. Over budget raises
`ContextBudgetExceeded` and the cycle aborts *before* the API call.

Measured, not estimated. Live runs against the paper account, plus a
worst-case synthetic state with every section at its cap:

| | tokens |
| --- | --- |
| Simple cycle, no tools (live) | 2,019-2,538 |
| Three tool calls over three round trips, live (peak) | 3,062 |
| Worst case: 25 positions, 20 theses, 30 decisions | 4,172 |
| ...plus a fully-spent tool-result budget | 6,824 |
| ...adversarial (800 repeated chars per thesis) | 8,280 |
| Budget | 10,000 |

Every variable-length section is capped **by bytes, not just by count** — see
"Long horizons" below for why that distinction cost a bug.

Every cycle logs its own `peak_prompt_tokens` alongside the budget in
`cycles.full_prompt`, so drift is visible without re-running anything.

### Theses

Thesis writes ride along with an executed order rather than getting their own
tool — spec.MD allows exactly one write tool, and a thesis with no position
behind it is not worth storing. An executed buy opens or updates the thesis; a
sell that leaves the position flat closes it; a partial sell leaves it open.


## Phase 4 — evaluation harness

```bash
uv run trader evaluate --start 2026-09-01 --end 2026-09-05
uv run python scripts/demo_evaluation.py     # the same report over synthetic data
```

```
VERDICT: beat cash (+0.69%); lost to buy-and-hold SPY (-1.22% excess)

Return
  strategy              +0.69%   ($100,000.00 -> $100,690.00)
  doing nothing         +0.00%   (cash)
  buy-and-hold SPY      +1.91%   ($760.00 -> $774.50)

Trading
  orders submitted           5      round trips                2
  confirmed filled           5      win rate               50.0%   (1W / 1L)
  unreconciled               0      avg holding            48.5h
                                    realized P&L         $106.50
                                    still open             QQQ 8

Risk rejections   4      2 max_orders_per_hour, 1 max_position_notional,
                         1 symbol_allowlist
Cycles           12      11 ok, 1 error      28,800 in / 2,160 out
```

`--json` gives the machine-readable form. The report reads only logged history
plus one benchmark series — it never re-runs a cycle, and evaluating the same
window twice gives the same answer (there's a test for that).

### Things it deliberately refuses to guess

- **A sell with no matching buy inside the window is skipped**, not paired with
  an invented entry price. The position was opened before the window; a
  fabricated entry would go straight into the win rate.
- **A window with fewer than two SPY sessions says so** instead of reporting a
  0% benchmark, which would read as a real result. It falls back to
  open-to-close and labels it.
- **No broker means no benchmark**, not a zero.
- **Canceled and rejected orders are not fills.** Orders that are merely
  unreconciled fall back to the risk layer's reference price, and the report
  counts them separately as `prices_estimated` so the win rate carries its
  caveat.
- **A flat return "matched" cash**; it did not lose to it.
- **When nothing was traded, the verdict says so** rather than crediting the
  agent with a drift it had no part in.

Round trips are matched **FIFO per symbol** — the convention, and it needs no
configuration.

### Fills

Win rate and holding period need real entry and exit prices, and the broker's
fill is the only truth for those. `trader reconcile` reads submitted orders back
and records `final_status`, `filled_qty`, `filled_avg_price`, `filled_at` on the
decision row. Orders still working are left alone for a later run.


## Long horizons

The design goal is that cycle 1,000 costs what cycle 1 cost. Measured over
1,560 cycles (26/day x 60 trading days, roughly three months) with theses
accumulating the whole way:

| | |
| --- | --- |
| Rendered prompt, cycle 100 -> 1,560 | 3,496 -> 4,787 chars |
| Spread across cycles 101-1,560 | 1,289 chars |
| Peak RSS growth | 2.8 MB |
| SQLite growth | ~7 KB/cycle -> ~45 MB/year at 15-min cadence |
| API cost | ~$0.025/cycle -> ~$0.65/day, ~$165/year |

### The bug this found

The Phase 1 flatness claim was **wrong**, and the test that "proved" it was
too weak. It accumulated only `decisions`, which are capped by count. It never
accumulated `theses` — and `MAX_RENDERED_THESES` capped how many theses were
rendered, not how many *bytes* they were worth. A thesis rationale is up to 800
chars and its invalidation condition had no cap at all, so twenty verbose
theses were ~32,000 chars of prompt, and the same soak that "proved" flatness
showed the context growing 1,024 -> 10,607 chars over 500 cycles.

That would not have lost money — the token assertion fails closed, so cycles
would have errored out rather than silently overspending — but it would have
stopped the harness dead a few weeks in.

Fixed by capping every variable-length section by bytes:

- per-thesis rationale and invalidation truncated **in the rendering only**;
  `get_theses` still returns the full stored text, which is what that tool is for
- `MAX_STATE_CHARS` as a hard backstop with a visible marker and a logged
  warning, so a future uncapped section shows up instead of quietly spending
  the budget
- `MAX_TOTAL_TOOL_RESULT_CHARS`, a cumulative ceiling across the cycle.
  Per-result truncation alone does not bound the loop: six iterations of
  parallel calls is still tens of thousands of characters, every one of them
  input tokens on the next request. Past the ceiling the model gets a short
  note explaining why, not silence.

All three sizes came from measured `count_tokens` output, not from guesses —
dense JSON tokenizes at roughly 1.5 chars/token, not the ~4 you would assume.

### What is still unproven

- **No unattended multi-day run.** Everything above is single cycles plus
  synthetic soaks. `trader run` has not been left going for a week.
- **`prompts/` is read once per process** (`lru_cache`), so editing the system
  prompt while `trader run` is live has no effect until restart.
- **No stdout log rotation.** Redirect to a file and rotate it externally.
- **No alerting.** A cycle that errors is logged and the loop continues, which
  is correct, but nothing tells you it happened — check `trader cycles`.


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
- `decisions` gains `reference_price` plus four nullable fill columns (schema
  v2), applied to existing databases by an **additive-only** migration in
  `db.MIGRATIONS` — it may add a nullable column and nothing else. No migration
  drops, renames, or rewrites, because the logged history is the point.

## Tests

`uv run pytest` — 211 tests, ruff clean. The structural invariants are worth
knowing about, because they fail the build rather than relying on review:

| File | Guards |
| --- | --- |
| `test_paper_only.py` | no live endpoint in `src/`, endpoint not configurable |
| `test_no_bypass.py` | one caller of `submit_order`, `evaluate` dominates it, no skip argument, one write tool, no shell/HTTP in the tool layer |
| `test_risk_checks.py` | every check: passing, failing, boundary |
| `test_risk_adversarial.py` | the hostile battery through the real execution path |
| `test_llm.py` | tool loop, one-order rule, budget asserted every iteration |
| `test_prompts.py` | no prompt text inline in `src/`, no strategy in the placeholder |
| `test_evaluate.py` | FIFO matching, window boundaries, and every case the report refuses to guess |
| `test_state.py` | the rendering stays bounded by bytes as theses accumulate |

## What has actually been run

Against the live paper account: single cycles (reads, tool loop, budget), one
1-share SPY order end to end through the risk layer to a confirmed fill,
reconciliation, and evaluation. **A full unattended session has not been run
yet** — that is the obvious next step, and `trader run` is what does it.
