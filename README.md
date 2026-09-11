# day-trader

Agentic paper-trading harness. **Paper trading only** — the Alpaca endpoint is a
single constant in [`src/trader/constants.py`](src/trader/constants.py) and
there is no config flag, env var, or argument in this repo that can change it.
`tests/test_paper_only.py` fails the build if a live endpoint appears anywhere
in `src/`.

A Robinhood Agentic account can be used in two ways:

- **Cursor MCP (Path A).** User-level `~/.cursor/mcp.json` only — never this
  project's `.cursor/`. Those live writes skip this harness's risk layer.
- **`trader run` (Path B).** `TRADER_BROKER=robinhood_agentic` sends approved
  orders through the same risk layer to the Agentic MCP account. Default is
  still Alpaca paper. There is no Alpaca live URL in this repo.

`trader evaluate`'s SPY benchmark still uses Alpaca paper market data when
those keys are present. Treat Robinhood Activity as the live blotter.

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
uv run trader dashboard            # local browser panel (start/stop/monitor)
uv run trader rh-login             # authorize Robinhood Agentic (browser OAuth)
uv run trader --text-logs ...      # human-readable logs instead of JSON lines
```

Set `TRADER_BROKER=robinhood_agentic` in `.env` to route `run` / `cycle` /
`reconcile` at the Agentic account. Run `rh-login` once on the desktop first —
a nohup'd loop cannot complete the browser handshake. Tokens land in
`data/robinhood_tokens.json` (gitignored). If the MCP write tool cannot attach
a requested stop or take-profit, the adapter refuses the order instead of
submitting unprotected.

`status`, `cycles`, `show`, `rejections`, `kill` and `evaluate` read only the DB
and work without any Alpaca credentials (`evaluate` drops the SPY benchmark and
says so).

`--stub` runs the Phase 1 stub agent instead of the model (no Anthropic key
needed). `--effort low|medium|high|xhigh|max` and `--no-thinking` tune the model call.

Risk limits live in [`risk.toml`](risk.toml) (stdlib `tomllib`, no YAML
dependency). Loading is strict — an unknown or misspelled key is a fatal error,
because a typo that silently left a limit at its default is the worst thing this
file could do.

**Two configs ship.** `risk.toml` is currently the high-risk one; the original
conservative limits are in `risk.conservative.toml`. Switch per-invocation:

```bash
uv run trader --risk-config risk.conservative.toml run
```

| | conservative | high-risk (current) |
| --- | ---: | ---: |
| `max_position_notional` | $5,000 | **$25,000** |
| `max_total_exposure` | $25,000 | **$200,000** |
| `max_daily_loss` | $2,000 | **$10,000** |
| `max_orders_per_hour` / `per_day` | 6 / 20 | **20 / 100** |
| `max_orders_per_cycle` | 1 | **4** |
| allowlist | 5 large caps | **30 names incl. sector + 3x ETFs** |
| `allow_shorts` | false | **true** |

Against ~$100k paper equity with 4x day-trading buying power (~$399k), that is
25% of equity in one name and 2x equity across the book.

### What raising the limits actually changed

`allow_shorts = true` is the largest single reduction in safety — it removes
the `no_unintended_short` check entirely, so a sell larger than the position is
no longer rejected as such. Only the notional caps bound short size:

```
holding 10 AAPL @ $100          conservative              high-risk
  sell    50 -> net    -40 sh   rejected (short)          ALLOWED
  sell   100 -> net    -90 sh   rejected (short)          ALLOWED
  sell   260 -> net   -250 sh   rejected (short)          ALLOWED  ($25,000, at the cap)
  sell 5,000 -> net -4,990 sh   rejected                  rejected (notional)
```

Losses on a short are unbounded to the upside; `max_daily_loss` is what stops
the day, and it latches.

Everything structural still holds — the paper endpoint, the single gated order
path, the one write tool, proposal sanity, the allowlist, RTH, and the kill
switch are all unchanged. These are limits, not mechanisms.

### Order rate is cadence × per-cycle cap, bounded by risk.toml

The agent may place up to `max_orders_per_cycle` orders per wake (high-risk: 4;
conservative: 1). Stops and take-profits ride on the entry as an Alpaca OTO or
bracket, so they live at the broker between cycles. The cycle interval is no
longer a 1-order ceiling:

| `TRADER_CYCLE_MINUTES` | cycles/hour | max orders/hour at 4/cycle | tokens/week (idle) | cost/week |
| ---: | ---: | ---: | ---: | ---: |
| 15 | 4 | 16 | 535K | $1.36 |
| 5 (default) | 12 | 48 | 1.6M | $4.07 |
| 3 | 20 | 80 | 2.7M | $6.79 |
| 2 | 30 | 120 | 4.0M | $10.18 |

Hourly and daily caps in `risk.toml` are what actually bind. Token cost assumes
an idle cycle; a cycle that places several orders and reads bars will cost more.

At the default 5 minutes with 4 orders/cycle, `max_orders_per_hour = 20` is the
real ceiling.

## Running and monitoring

### Control panel

A local browser UI starts, stops, and monitors the loop. It binds to loopback
only and never submits orders — Start/Stop spawn `trader run`, and the kill
switch is the same DB flag as the CLI.

```bash
uv run trader dashboard
```

Opens `http://127.0.0.1:8765/`. `--no-browser` if you just want the URL.
`--port 9000` if 8765 is taken. Status, cycles, rejections, and the log tail
refresh every few seconds. The header badge shows `paper` or `robinhood
agentic`; Start on Robinhood asks for confirmation. Reconcile uses whichever
broker `TRADER_BROKER` selected.

### Start it

```bash
mkdir -p logs
nohup uv run trader run >> logs/trader.jsonl 2>&1 &
echo $! > logs/trader.pid
```

`trader run` blocks. It handles SIGINT and SIGTERM cleanly — the in-flight
cycle finishes, the DB is consistent either way, and anything left `running`
is reconciled to `interrupted` on the next start.

```bash
kill -TERM $(cat logs/trader.pid)     # graceful stop
```

Startup logs the next fire time and the kill-switch state, so a loop started
outside market hours is visibly waiting rather than hung:

```json
{"msg":"scheduler_start","cycle_minutes":5,"risk_config":"risk.toml",
 "next_cycle":"2026-09-10T09:00:00-04:00","kill_switch":false,"window":"09:30-16:00 ET"}
```

### Watch it

Logs are one JSON object per line, so `jq` works directly:

```bash
tail -f logs/trader.jsonl | jq -c '{ts,msg,cycle_id,status,action,error}'
```

```bash
jq -c 'select(.level=="ERROR" or .level=="WARNING")' logs/trader.jsonl
```

The events worth knowing by name:

| `msg` | meaning |
| --- | --- |
| `cycle_start` / `cycle_end` | every cycle, with status and duration |
| `order_submitted` | an order reached the broker |
| `order_rejected` | the risk layer stopped one, with the checks that fired |
| `broker_error` / `agent_error` | the cycle failed; the loop continues |
| `kill_switch_engaged` | halted, no broker calls made |
| `state_render_truncated` | a state section grew past its cap — investigate |
| `tool_result_budget_exhausted` | the model over-used tools this cycle |
| `reconciled_orphan_cycles` | expected once after an unclean shutdown, not routinely |
| `scan_failed` | the market scan degraded to a note; the cycle still ran |
| `auto_reconciled_fills` | cycle start read fills back from the broker |
| `closed_orphan_theses` | a stop-out flattened a position between cycles |
| `canceled_resting_orders` | a reducing order cleared the symbol's exits first |

### Check on it

```bash
uv run trader status                  # today, reconstructed from the DB
uv run trader cycles --limit 30       # one line per cycle
uv run trader rejections              # which checks are firing, and why
uv run trader show <cycle_id>         # the full prompt and response
```

`trader status` makes no broker call, so it is safe to run while the loop is
live. So is everything else in that list.

### Stop it

```bash
uv run trader kill on --note "why"    # halts trading, loop keeps running
uv run trader kill off
```

The kill switch lives in the DB, so it survives a restart, can be thrown from
another shell while the loop is running, and is re-read immediately before
every order — not taken from the cycle context. There is no override.
`kill -TERM` stops the process; the kill switch stops the *trading*.

### End of day

```bash
uv run trader reconcile               # read fills back from the broker
uv run trader evaluate --start 2026-09-14 --end 2026-09-18
```

Reconciliation also runs automatically at the top of every cycle (up to 50
pending orders, failures logged per order and skipped), so forgetting the
manual command no longer leaves the evaluation data incomplete. The command
remains for ad-hoc runs and for orders still working when the loop last
looked.

### What to actually watch for

- **`error` cycles in `trader cycles`.** One is a transient broker blip. Several
  in a row is a real problem — the loop keeps going either way, and nothing
  alerts you.
- **`orders_today` climbing toward `max_orders_per_day`.** Visible in
  `trader status` and in every prompt.
- **`max_daily_loss` in `trader rejections`.** That one latches for the rest of
  the day; an intraday recovery does not clear it.
- **`state_render_truncated`.** Should never appear. If it does, a section grew
  past its cap and the context budget is at risk.
- **`reconciled_orphan_cycles` on every start.** Means the process is dying
  uncleanly rather than being stopped.

### Gaps you should know about

- **No log rotation.** Redirect to a file and rotate it yourself
  (`newsyslog`/`logrotate`), or the file grows unbounded.
- **No alerting.** Errors are logged, not pushed. Check `trader cycles`.
- **No supervision.** If the process dies, nothing restarts it. A launchd
  `KeepAlive` job would fix that; nothing in this repo does it for you.
- **`prompts/` is read once per process.** Editing the system prompt while the
  loop is live has no effect until restart.


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

- **The engine never short-circuits.** All registered checks run on every proposal, so a
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
| `max_orders_per_hour` / `max_orders_per_day` / `max_orders_per_cycle` | Counted from our own `decisions` rows — the broker has no idea which of its orders came from this harness. An approved order the broker then rejects gets no order id, so it does not consume budget. `max_orders_per_cycle` is the per-wake cap; the tool layer also refuses extra *attempts*. |
| `protective_exits` | Stop/take-profit must sit on the correct side of the entry. Missing exits are allowed. |
| `stop_loss_budget` | A stop whose dollar risk exceeds the remaining daily-loss budget is rejected. |

`uv run pytest` — 46 per-check units (a passing, failing and boundary case
each), and 22 adversarial cases run through the real execution path against a
real SQLite file.


## Phase 3 — the agent

`claude-sonnet-5` via the `anthropic` SDK, in a **manual** tool loop —
not the SDK's tool runner. The loop has to interpose the risk layer between the
model's proposal and the broker and hand the verdict back as a tool result, and
spec.MD asks to own every failure mode.

Prompt text lives in [`prompts/`](prompts) as plain `.txt` files; a test fails
the build if prompt text appears inline in `src/`. `prompts/system.txt` is the
operator playbook: 15-minute opening-range continuation, SPY/QQQ regime filter,
mandatory `get_quote`/`get_bars` before a new order, size from the stop, and a
hard stop plus take-profit on every entry. The risk layer still does not pick
trades. Editing the prompt while the loop is live has no effect until restart.

### The market scan: breadth lives in code, not in the tool budget

The tool budget can afford roughly two `get_bars` calls per cycle, which used
to cap how much of the market the agent could *see* at two symbols. Breadth now
comes precomputed: `trader/scanner.py` sweeps the entire allowlist each cycle
through one batched broker method (`get_scan_data` — three vendor requests
total, regardless of universe size) and renders a compact table into the state
block: last price, % change vs prior close, volume pace vs the 10-day average
adjusted for time of day, the 09:30–09:45 opening range with the symbol's
position relative to it (judged from the last *closed* 5-minute bar — wicks do
not count), and distance from the rough session VWAP. The QQQ regime
(`up`/`down`/`chop`) is classified in code on the first line. Held names sort
first, then names outside their opening range, then by |% change|; rendered
rows are capped at 20.

Volume note, measured live: on these keys the snapshot's daily bar is IEX-only
(~1% of SPY's consolidated volume) while historical daily bars are
consolidated, so today's volume is read from the daily-bars response and the
ratio is pro-rated by session time elapsed — `vol_x` ≈ 1 means "normal pace
for this time of day".

The scan is advisory: if it fails, the state says `scan unavailable`, the
cycle proceeds, and the playbook falls back to tools for the one or two names
that matter. The Robinhood MCP adapter has no batched data endpoint, so it
degrades this way by design. Everything in `scanner.py` is a pure function
over plain dicts — no vendor SDK imports (test_no_bypass.py keeps alpaca-py
confined to `broker.py`).

The state block also gained `## Open orders` — the stops and take-profits
actually resting at the broker — so "does this position have a stop on file"
is finally answerable, which the playbook's orphan rule needs.

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

At most `max_orders_per_cycle` orders per cycle. A further call comes back as an
error result, not an order. Optional `stop_price` and `take_profit_price` ride
on the entry as a broker-side OTO or bracket.

**Reducing orders cancel the resting exits first.** Alpaca reserves the shares
held by bracket legs, so a flatten used to bounce with "insufficient qty
available" — the agent could enter protected positions but not exit them.
`execution.place_order` now cancels the symbol's open orders before submitting
any order that shrinks, flattens, or reverses a position. Only after the risk
verdict approves (a rejected proposal cannot strip a position's protection),
and never for orders that open or add.

### Theses follow the position, not the verb

Thesis lifecycle is keyed on the *resulting signed position*, not on buy/sell:
a sell that opens a short stores a thesis (without one, the next cycle's
playbook would flatten it on sight); a buy-to-cover that flattens closes the
thesis and creates none; a partial exit keeps the original entry rationale
rather than overwriting it with "trimming half". And because a broker-side
stop can flatten a position between cycles with nobody to close its thesis,
every cycle starts with an orphan sweep: open theses with no matching position
(and older than a 10-minute grace window) are closed and logged.

### The context budget

`MAX_PROMPT_TOKENS = 12_000`, asserted before **every** request in the loop, not
just the first — tool results accumulate, so the last iteration is the one that
would blow it. Counted through the API's own `count_tokens` with the real
system, messages, and tools; a local estimate that drifts from the server's
count would make the assertion meaningless. Over budget raises
`ContextBudgetExceeded` and the cycle aborts *before* the API call.

The budget was 10,000 before the market scan and open-orders sections joined
the state block; their render caps are worth roughly 1,700 chars (~1,100
tokens of dense table), and the scan exists to *reduce* tool round-trips, not
to squeeze the state. `MAX_STATE_CHARS` grew 5,000 → 7,000 for the same
reason, and the worst-case bounding test now renders both new sections at
their caps.

Measured, not estimated (pre-scan numbers; add ~1,100 tokens for a full scan):

| | tokens |
| --- | --- |
| Simple cycle, no tools (live) | 2,019-2,538 |
| Three tool calls over three round trips, live (peak) | 3,062 |
| Worst case: 25 positions, 20 theses, 30 decisions | 4,172 |
| ...plus a fully-spent tool-result budget | 6,824 |
| ...adversarial (800 repeated chars per thesis) | 8,280 |
| Budget | 12,000 |

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

`uv run pytest` — 300 tests, ruff clean. The structural invariants are worth
knowing about, because they fail the build rather than relying on review:

| File | Guards |
| --- | --- |
| `test_paper_only.py` | no live endpoint in `src/`, endpoint not configurable |
| `test_no_bypass.py` | one caller of `submit_order`, `evaluate` dominates it, no skip argument, one write tool, no shell/HTTP in the tool layer |
| `test_risk_checks.py` | every check: passing, failing, boundary |
| `test_risk_adversarial.py` | the hostile battery through the real execution path |
| `test_llm.py` | tool loop, one-order rule, budget asserted every iteration, thesis lifecycle incl. shorts |
| `test_prompts.py` | no prompt text inline in `src/`, playbook present in `prompts/system.txt` |
| `test_evaluate.py` | FIFO matching, window boundaries, and every case the report refuses to guess |
| `test_state.py` | the rendering stays bounded by bytes as theses, scan rows and open orders accumulate |
| `test_scanner.py` | opening range, closed-bar OR position, VWAP, prorated volume, regime |
| `test_execution.py` | reducing orders cancel resting exits (and only then); auto-reconcile records terminal fills |

## What has actually been run

Against the live paper account: single cycles (reads, tool loop, budget), one
1-share SPY order end to end through the risk layer to a confirmed fill,
reconciliation, and evaluation. **A full unattended session has not been run
yet** — that is the obvious next step, and `trader run` is what does it.
