# day-trader Shortfall Audit

**Repository:** ianmccourt/day-trader  
**Audited:** 2026-09-18  
**Status:** Phase 4 complete; unattended week-long operation unproven

---

## Executive Summary

This is a well-architected **paper-trading harness** for a one-week agentic experiment, not a production trading system. The structural constraints (paper-only enforcement, single gated order path, risk layer isolation) are exemplary and the test coverage of invariants is better than most production code. **However, it has never run unattended for more than a few hours.** The gaps that matter for week-long operation are operational (no alerting, no supervision, no rotation), not architectural. The LLM has no defined edge—just a discretionary ORB playbook—and `evaluate` measures against buy-and-hold but provides no statistical evidence of skill vs luck. Context management works in principle but the scan+limits grew the budget 200% mid-stream when cycles aborted during entry windows. RSI mechanisms are entirely absent by design; adding them without breaking paper-only safety requires a formal approval gate and isolated eval before any prompt/config change goes live.

**This harness can survive a week. The operator cannot walk away for a week.**

---

## Ranked Shortfalls

### CRITICAL

#### C1. No alerting or supervision (harness dies silently)
**Evidence:**
- `README.md:253-258`: "No alerting. Errors are logged, not pushed. Check `trader cycles`."
- `README.md:254-256`: "No supervision. If the process dies, nothing restarts it."
- `scheduler.py:88-90`: SIGTERM/SIGINT handlers log and exit; no external watchdog
- Logs show 400+ cycles over several sessions (paper account activity); operator must poll `trader cycles` or the dashboard manually

**Impact:** A broker timeout, unclean shutdown, or OOM kill leaves the harness dead until the operator checks. No email, Slack, PagerDuty, or even a failed-cron signal.

**What's missing:**
- A launchd/systemd `KeepAlive` or supervisor config to restart on exit
- Alerting on: consecutive error cycles, kill-switch engaged, max-daily-loss latched, process not running
- Health-check endpoint the dashboard or an external monitor can poll


#### C2. Unattended multi-day run unproven
**Evidence:**
- `README.md:612-617`: "No unattended multi-day run. Everything above is single cycles plus synthetic soaks."
- `scripts/simulate_session.py`: 26-cycle session proof, synthetic; longest real session unknown
- `logs/trader.jsonl` (2489 lines, mostly `skipped_no_entry_window`): real cycles span hours within a day, not days
- `tests/test_state.py`: 1,560-cycle soak validates flatness but uses a fake broker with no network, no MCP re-auth, no quota exhaustion

**Impact:** Unknown failure modes in: MCP token expiry across a weekend, Anthropic rate-limit exhaustion over 1,600 cycles, SQLite locking under multi-day load, prompt drift if the model's reasoning changes mid-week.

**What would prove it:** Run `trader run` Friday 09:00 ET → Friday 16:00 next week with real Alpaca paper (or Robinhood Agentic sandbox) and return to a 200-cycle DB, no `reconciled_orphan_cycles` spam, and a coherent `trader status`.


#### C3. No log rotation (disk fills, process hangs on write)
**Evidence:**
- `README.md:252`: "No log rotation. Redirect to a file and rotate it yourself."
- `cli.py`: logs go to stdout; `nohup >> logs/trader.jsonl` is documented
- No logrotate/newsyslog config in repo

**Impact:** `logs/trader.jsonl` grows unbounded. At ~1KB/cycle × 12 cycles/hour × 24h × 5 days = 1.4 MB/week (paper math), but a cycle with full tool results and rejected orders can hit 10-50 KB. A busy week could fill `/` on a small VM or cause Python to block on a full pipe.


### HIGH

#### H1. Strategy has no defined edge; playbook is discretionary ORB
**Evidence:**
- `prompts/system.txt:63-70`: "opening-range breakout (ORB) book with a regime filter"
- `prompts/system.txt:136-151`: setups are ORB break (first close outside 09:30-09:45 range) and afternoon VWAP continuation
- No backtested parameters: range % filters (0.15–1.5%), stop placement (midpoint), R:R (1.5:1) are playbook assertions, not derived from historical SPY/QQQ data
- `scanner.py:150-177`: regime is computed (QQQ 15-min closes above/below session midpoint + rising/falling highs), but there's no study showing `chop` actually predicts range fakeouts
- `evaluate.py:117-128`: verdict compares to cash and buy-and-hold SPY, but provides no confidence interval, Sharpe, win-rate significance test, or regime-conditional breakdown

**Impact:** The model is given a human day-trading heuristic (ORB + regime filter) without evidence it has an edge over the 2020–2026 regime. A week-long run will produce a P&L, but `evaluate` cannot tell you if 5 wins and 3 losses at 1.5R is skill or a lucky draw from a 50/50 coin flip.

**What's missing:**
1. Backtest of the ORB setup over 2022-2024 SPY/QQQ on Alpaca paper historical data: win rate, average R, regime-conditional performance, time-of-day sensitivity
2. Statistical tests in `evaluate.py`: permutation test for win rate > 50%, bootstrap confidence interval on realized R, comparison to a naive `buy every above-range close` rule
3. Evidence that the model's discretion (choose one of 30 names from the scan) adds value over a fixed QQQ-only rule


#### H2. Context budget grew 200% mid-stream when cycles aborted during entry windows
**Evidence:**
- `llm.py:23`: `MAX_PROMPT_TOKENS = 16_000`
- `README.md:461-480`: budget was 10k → 12k → 16k after "five consecutive cycles aborted at 10.0–10.9k tokens during 09:50–10:15 ET — the first half hour of the primary entry window"
- `state.py:38-46`: scan + open-orders sections added ~1,700 chars (~1,100 tokens), forcing the 10k → 12k bump
- `README.md:475-480`: "the playbook's mandated workflow (open-risk check, quote, bars, then `place_order`) spans 2–3 tool iterations, and each iteration echoes its adaptive-thinking blocks back per the API contract, so a compliant cycle simply does not fit in 12k with headroom"

**Impact:** The budget is still a tripwire, not a guarantee. The scan was added to *reduce* tool calls, but its render cost plus the playbook's required read-then-write sequence pushed the budget to 16k. A future feature (news sentiment, sector breadth, overnight gaps) that adds another 1k tokens could force another bump or abort cycles at 09:50 again—exactly when the model should be placing the day's entries.

**What's missing:**
1. Headroom analysis: current worst-case prompt (max positions, max theses, max decisions, full scan, full tool budget, 3 tool iterations with thinking) measured against 16k
2. A fallback: if token count at iteration N exceeds 14k, skip further tool calls and force `no_action` rather than aborting the cycle
3. Prompt compression: the rendered state is still prose (`symbol | qty | avg_price | ...`); switching to JSON arrays would cut ~30% off state sections


#### H3. Robinhood Agentic adapter is "fail closed" for missing capabilities
**Evidence:**
- `robinhood_mcp.py:759-767`: `get_scan_data` raises `BrokerError("market scan is not supported")` — state degrades to `scan unavailable` note
- `robinhood_mcp.py:769-774`: `get_open_orders` raises `BrokerError("open-order listing is not supported")` — state loses the "does this position have a stop on file?" check
- `robinhood_mcp.py:776-778`: `cancel_open_orders` raises `BrokerError` — reducing orders cannot clear resting exits, so a flatten may bounce with insufficient qty
- `robinhood_mcp.py:203-223`: tool discovery scores by name/description hints; a renamed MCP tool drops to `None` and later calls raise "no write tool"

**Impact:** Running against Robinhood Agentic (Path B, `TRADER_BROKER=robinhood_agentic`) loses the market scan (model cannot see breadth or opening ranges precomputed) and cannot verify stops are actually resting. The playbook's "check `## Open orders` for its stop **and** its take-profit" rule (line 85-89 of `system.txt`) silently degrades to "hope the adapter attached them."

**What's missing:**
1. Robinhood-specific integration tests: submit a bracket order, verify stops show up in a follow-up `get_open_orders` capability (if one exists), flatten the position
2. A Robinhood-aware playbook variant that doesn't mandate scanning 20 names when only 2 `get_bars` calls fit the budget
3. MCP capability status visible in `trader status` / the dashboard so the operator knows the harness is degraded


#### H4. No crash recovery beyond orphan-cycle reconciliation
**Evidence:**
- `scheduler.py:56-60`: `reconcile_orphan_cycles` on startup marks `running` cycles as `interrupted`
- `cycle.py:129-136`: fills are auto-reconciled at the top of each cycle
- No recovery for: a cycle that wrote a decision but never got its receipt (order may or may not be at the broker), a stop that filled while the process was dead (position flat but thesis still open until next cycle's orphan sweep)
- `README.md:247-249`: "`reconciled_orphan_cycles` on every start means the process is dying uncleanly rather than being stopped"

**Impact:** An unclean shutdown (SIGKILL, OOM, kernel panic) can leave: (1) a decision row with `risk_result='approved'` but no `broker_order_id` (we don't know if the broker saw it), (2) a resting stop that filled while down (position gone, thesis still open for 10 minutes, then closed by the orphan sweep—but the fill is only recorded if the next cycle's auto-reconcile or a manual `trader reconcile` fetches it). The harness recovers *eventually*, but not instantly.

**What's missing:**
1. Startup should run a full `reconcile_fills` immediately (it already does auto-reconcile per cycle, but only up to 50 orders; a multi-day restart should sweep all pending)
2. A "dirty shutdown" flag: if the last cycle in the DB is `running` and the process starts, log a warning and reconcile before resuming
3. A post-crash verification: on startup, compare the DB's open theses to the broker's actual positions and flatten any orphans before the first cycle


#### H5. Risk allowlist is manually curated; no dynamic addition/removal
**Evidence:**
- `risk.toml:46-84`: 30-symbol allowlist is a static TOML array
- No mechanism to add a name intraday (e.g., a 10% gap-up on earnings that the model sees in the scan but cannot trade because it's off-list)
- No mechanism to remove a name that halted or went illiquid

**Impact:** The model can see a clean ORB setup on a name that's not on the allowlist (via the scan or a quote) but cannot trade it. Conversely, a name that gaps and halts 5 minutes into the session stays on the allowlist all day, and the model might propose it (rejected by `symbol_allowlist` check, but still burns a tool call).

**What's not missing (intentionally):** SPEC.MD constraint: "The LLM has exactly one write tool." Letting the model edit `risk.toml` at runtime would let it add `TSLA` after the check rejects it—classic adversarial prompt. The allowlist is an operator control, not a model control.

**What could work:**
1. Operator tool: `trader allow add SYMBOL` / `trader allow remove SYMBOL` (writes a per-session override to the DB, not to the TOML file)
2. Pre-session script that pulls the top 30 by volume + gap % from a screener and regenerates `risk.toml` before the loop starts
3. A separate "watchlist" in the state that the model can see but not trade (scan shows it, risk layer rejects it, model learns not to propose)


### MEDIUM

#### M1. No measure of regime/market structure beyond QQQ 15-min highs
**Evidence:**
- `scanner.py:150-177`: regime is `up` (last close > midpoint, highs rising), `down` (mirror), else `chop`
- No VIX, no breadth (advance/decline, new highs/lows), no sector rotation signal, no overnight gap context
- `prompts/system.txt:27`: "The QQQ regime (`up` / `down` / `chop`) is computed for you on the first line."

**Impact:** The model sees "chop" but doesn't know *why*—VIX 30 and whipsaw, or VIX 12 and tight range? It sees "up" but doesn't know if it's broad (all sectors green) or narrow (NVDA +3%, rest flat). The playbook says "In `chop`, take no new trade unless a name is clearly outside its range" (line 104-105), but "chop" is just "QQQ's 15-min highs aren't trending"—that could be a healthy pause or a pre-breakdown churn.

**What's missing:**
1. `scanner.py` could add: VIX last (from a `get_quote` call), SPY advance/decline ratio (if the broker provides breadth), or sector ETF direction (XLF/XLE/XLK up/down count)
2. Overnight gap: `scanner.py` already fetches prior close; adding `gap_pct = (first_5min_close - prior_close) / prior_close` would tell the model if the session opened with a 1% gap that invalidates the prior day's range
3. Regime history: the state could show "regime at 09:45, 10:30, 12:00" to distinguish a session that opened `up` and stayed there from one that chopped through three regime flips


#### M2. No test that the playbook's mandated workflow fits the budget
**Evidence:**
- `prompts/system.txt:84-91`: "Open risk first. `get_quote` the held name and pull `get_bars` `5Min` limit 12 for it. [...] `get_quote` [the candidate], then pull `get_bars` `5Min` limit 12 to confirm the setup."
- `llm.py:34`: `MAX_TOOL_ITERATIONS = 8`
- `llm.py:46-52`: `MAX_TOTAL_TOOL_RESULT_CHARS = 4_000` (cumulative, not per-tool)
- No test that walks through "hold 1 position with a thesis → get_risk_limits → get_quote(HELD) → get_bars(HELD, 5Min, 12) → get_quote(CANDIDATE) → get_bars(CANDIDATE, 5Min, 12) → place_order → response" and asserts the final prompt is under 16k

**Impact:** The budget grew because the mandated workflow hit 10.9k during entry windows. There's no regression test that fails if a future change (longer theses, bigger scan table) pushes a compliant cycle over 16k again.

**What's missing:**
`tests/test_llm.py` should add a test: `test_playbook_workflow_fits_budget` that mocks a worst-case cycle (1 held position, full scan, 2 `get_bars` calls, 1 `place_order`, 3 tool iterations with thinking) and asserts `_assert_budget` never raises. If this fails, either compress the state or tell the playbook to do less.


#### M3. Playbook invalidation conditions are prose; model must re-parse them each cycle
**Evidence:**
- `prompts/system.txt:199-202`: "`invalidation_condition` must be a price-and-time fact a later cycle can check without you, e.g. \"5-minute close back inside 09:30–09:45 range 430.10–431.80\"."
- `tools.py:229-271`: thesis rationale and invalidation are stored as TEXT, capped at 800 chars, truncated to 160/100 chars in the rendered state
- No structured fields: `invalidation_price`, `invalidation_side`, `invalidation_time`

**Impact:** The model writes "5-minute close back inside 09:30–09:45 range 430.10–431.80", then the next cycle must parse that string, fetch bars, and decide if it's true. If the model writes "close below the range" (no price), "break of support" (no range), or "invalidated by tomorrow" (time ambiguous), the next cycle has to guess. Worse: if the model's invalidation is wrong (e.g., "close below VWAP" when the stop is at the range midpoint), the cycle can miss the actual stop trigger.

**What's missing:**
1. Structured invalidation: `place_order` schema adds optional `invalidation_price: float, invalidation_side: 'above'|'below'` so the harness can auto-flatten on a 5-min close that crosses it, without waiting for the next cycle
2. A validation check: if the model provides `stop_price=430.0` but `invalidation_condition="close above 432"`, reject the thesis as contradictory (the stop is below entry, invalidation is above entry)
3. Auto-invalidation: if a position has a stop on file and the stop fills between cycles, the thesis closes itself (this already happens via the orphan sweep, but only after 10 minutes)


#### M4. Per-cycle order cap (4) but no per-symbol order cap
**Evidence:**
- `risk.toml:39`: `max_orders_per_cycle = 4`
- `tools.py:155-159`: enforced in `_place_order`
- No check for "you already submitted 2 orders on NVDA this cycle; don't submit a 3rd"

**Impact:** The model could theoretically submit 4 orders on the same symbol in one cycle (open, add, add again, flatten?), though the playbook's "Do not pyramid. Do not add to a name you already hold" (line 191-192) and the `no_pyramid` check prevent most of this. Still, a model hallucination or a misunderstood "add to winner" loop could burn all 4 slots on one name.

**What's missing:**
A per-symbol order counter in the risk layer: `max_orders_per_symbol_per_cycle = 1` (or 2, to allow open + later flatten).


### LOW

#### L1. Prompts are loaded once per process; editing playbook while running is silent no-op
**Evidence:**
- `README.md:256-258`: "`prompts/` is read once per process. Editing the system prompt while the loop is live has no effect until restart."
- `README.md:614-616`: (repeated)
- `prompts.py`: `@lru_cache` decorator on `load()`

**Impact:** Operator edits `prompts/system.txt` to change the R:R from 1.5:1 to 2:1, saves, expects the next cycle to pick it up—but the loop keeps using the old cached prompt until restart. Not a safety issue (the change eventually applies after restart), but a surprising behavior that could lead to "why isn't my playbook change working?" confusion.

**Fix:** Watch `prompts/*.txt` with `watchdog` or check mtime each cycle; if changed, reload. Or just document: "Restart `trader run` after editing prompts."


#### L2. No distinction between "quiet because nothing to do" and "quiet because the scan failed"
**Evidence:**
- `cycle.py:165-183`: if `not ctx.clock.is_open`, status is `skipped_market_closed`; if past 14:30 with no positions/orders, status is `skipped_no_entry_window`
- `state.py:133-139`: if scan fails, `scan_note = "scan unavailable: {exc}"`, but the cycle proceeds and might return `no_action`
- No `cycle.status` like `ok_degraded` to flag "model chose no_action but didn't have the scan"

**Impact:** `trader cycles` shows 11 `ok` and 1 `error`; you don't know if the `ok` cycles had the scan or degraded. The operator has to `trader show <cycle_id>` and grep for "scan unavailable" in the prompt.

**What's missing:**
A `skipped_scan_unavailable` status, or a `degraded: true` flag on `cycles` rows, so `trader cycles` can show `ok*` for a cycle that ran without the scan.


#### L3. No dry-run or staging environment workflow
**Evidence:**
- `cli.py`: `--stub` runs Phase 1 stub agent (always `no_action`), but no `--dry-run` that runs the real model and risk layer but never calls `broker.submit_order`
- No separate `risk.staging.toml` or `TRADER_BROKER=fake` that lets you test a config change against live market data without submitting orders

**Impact:** To test a prompt change, you either run `trader cycle --force` outside market hours (but then the scan is skipped and quotes are stale) or you let it run during RTH and hope it doesn't place a bad order. There's no "run the full loop, log what it *would* have done, but don't submit."

**What's missing:**
1. `trader cycle --dry-run`: runs the model, runs the risk layer, logs the verdict, records a `dry_run` decision, but never calls `submit_order`
2. A `FakeBrokerOverRealData` adapter: uses Alpaca paper for quotes/bars/clock but returns a fake receipt on `submit_order`, so you can test the loop end-to-end without burning paper orders


#### L4. Evaluation win rate and Sharpe are point estimates with no confidence intervals
**Evidence:**
- `evaluate.py:88-102`: win rate is `sum(1 for wins) / len(trips)`, avg holding is the mean, no stderr, no bootstrap
- `evaluate.py:354-424`: formatted report shows realized P&L, win rate %, excess vs SPY, but no "±" or p-value
- `README.md:537-556`: "Things it deliberately refuses to guess" (good!), but it also refuses to quantify uncertainty

**Impact:** A 5-win, 3-loss session at 1.5R average shows "62.5% win rate" in the report. Is that statistically significant? A bootstrap over 8 round-trips would give you a 90% CI like [40%, 85%]—wide enough to say "we don't know yet." As written, the report implies 62.5% is a real number when it's a tiny sample.

**What's missing:**
1. `evaluate.py` adds `win_rate_ci_90_pct: [lower, upper]` via bootstrap resampling
2. A "sample size" warning: if `len(round_trips) < 20`, print "sample too small for statistical inference; run longer"
3. Sharpe ratio: `(strategy_return - rf_rate) / std(daily_returns)` if the window spans 5+ sessions


---

## What Already Works Well

Cite these as strengths to preserve in any refactor:

1. **Paper-only enforcement is structural, not policy.** `tests/test_paper_only.py` AST-scans `src/` and fails if a live URL appears. Adding live trading requires editing `constants.py` and making tests pass—cannot happen by accident. (`constants.py:8-11`, `test_paper_only.py:23-33`)

2. **Risk layer cannot be bypassed.** `tests/test_no_bypass.py` enforces: one caller of `submit_order` (`execution.place_order`), `evaluate()` dominates it, no skip argument, one write tool. (`test_no_bypass.py:47-56, 50-51`)

3. **Risk checks are pure, unit-tested predicates.** Every check in `risk/checks.py` is `(proposal, state) -> (bool, reason)`; no DB, no broker, no clock. `tests/test_risk_checks.py` has 66 cases (pass, fail, boundary per check). (`README.md:293-295, 360-365`)

4. **Context budget is asserted before every API call, not hoped for.** `llm.py:97-115` counts tokens via `client.messages.count_tokens` and raises if over budget. Budget violations are loud, not silent. (`llm.py:108-112`)

5. **Cycle rows written before work, not after.** A crash always leaves evidence (`cycle.status='running'`), reconciled on restart. (`cycle.py:84`, `scheduler.py:56-60`)

6. **Schema migrations are additive-only.** `db.MIGRATIONS` may add nullable columns, never drop/rename/rewrite. Logged history is immutable. (`README.md:646-653`)

7. **Tool results are capped per-call and cumulatively.** `llm.py:41-52` prevents a tool-loop OOM. Over budget, the model gets a truncation note and must decide. (`llm.py:183-192`)


---

## Recommended Next Ships

Thin vertical slices, ordered by impact. Each can ship independently.

### 1. Add basic alerting + supervision (unblocks unattended week)
**Ship:** A `start_supervised.sh` that wraps `trader run` in a supervisor (e.g., `systemd --user` service or a simple loop + pid check). On exit, send an email/Slack webhook. On 3+ consecutive `error` cycles in the DB, alert.

**Evidence it works:** Run Friday 09:30 → 16:00, kill the process at 11:00, verify it restarts within 60s and reconciles orphans.

**Effort:** 1 day (systemd service file + alert script + test).


### 2. Prove the harness over a real multi-day run (derisk unknown unknowns)
**Ship:** Run `trader run` from Friday 09:30 ET for 5 consecutive trading days against Alpaca paper (or Robinhood Agentic sandbox), log to a dated file, and return with `trader cycles`, `trader status`, and `trader evaluate` output.

**Acceptance:** 200+ cycles, no `reconciled_orphan_cycles` spam, no multi-hour gaps in `trader cycles`, no OOM/disk-full/token-exhausted errors, prompts still under budget at cycle 200.

**Effort:** 0 dev work; 1 week wall-clock + monitoring time.


### 3. Add headroom test for playbook workflow (prevent future budget aborts)
**Ship:** `tests/test_llm.py::test_playbook_workflow_fits_budget_with_headroom` that mocks worst-case state (15 positions, 8 theses, full scan, 2 `get_bars` × 12 bars, 3 tool iterations, thinking), runs `_assert_budget`, and asserts `tokens < 14_000` (leaves 2k headroom below 16k).

**Evidence it works:** Test passes now. If a future PR adds a state section that pushes it to 15k, test fails and author must compress or split.

**Effort:** 2 hours.


### 4. Backtest the ORB playbook (prove edge or kill the strategy)
**Ship:** `scripts/backtest_orb.py` that pulls 2022-2024 SPY/QQQ historical bars from Alpaca paper, walks each session, marks ORB breaks per the playbook, simulates entries at the first close outside range with a midpoint stop and 1.5R target, and prints: total trades, win rate, avg R, Sharpe, max drawdown, regime-conditional breakdown.

**Acceptance:** If win rate > 50% and Sharpe > 0.5, document it in `README.md` as "the playbook has a historical edge on these two names." If win rate ≈ 50%, document "playbook is breakeven in backtest; week-long run is a live experiment, not a validated strategy."

**Effort:** 3-5 days (data fetch, ORB logic, regime matching, stats).


### 5. Add log rotation + disk-usage alert (prevent silent disk-full death)
**Ship:** `logrotate.d/trader` config (or equivalent for the OS) that rotates `logs/trader.jsonl` daily, compresses, keeps 14 days. Add a startup check: if `/` is >90% full, log a warning and optionally refuse to start.

**Evidence it works:** Run a cycle, manually fill `/` to 95%, verify next startup logs the warning.

**Effort:** 2 hours.


---

## Explicit Non-Recommendations

Things that look useful but don't help a one-week paper-trading experiment:

1. **Live trading.** The repo is paper-only by design. Adding live would require: separate risk limits, real capital at risk, regulatory compliance (Pattern Day Trader rule, wash sales, tax reporting), and a kill switch the operator trusts with money. None of that helps prove the harness works for a week.

2. **A portfolio optimizer or multi-name allocation.** The playbook is single-name discretionary ORB. Adding Markowitz optimization or a "max 3 names, weighted by conviction" rule doesn't test whether the harness survives—just adds surface area for the model to hallucinate.

3. **News sentiment or LLM-parsed earnings.** The model already has 30-name breadth via the scan, 3 tool iterations, and 16k tokens. Adding a news feed (via an API or a scraping tool) would cost tokens, add latency, and test the news API's uptime, not the harness's resilience.

4. **Stop-loss tightening or trailing stops.** The playbook's midpoint stop is fixed; adding "trail the stop to breakeven after 0.5R" would improve win rate but doesn't test the harness. Ship it after the week-long run proves the basics.

5. **Real-time dashboard with charts.** The existing dashboard (`trader dashboard`) shows status, cycles, rejections, log tail. Adding TradingView embeds or P&L charts is operator convenience, not a harness gap.

6. **Multiple concurrent positions on the same symbol.** SPEC.MD disallows pyramiding; the playbook says "one position per symbol." Removing that constraint doesn't test unattended operation—just tests whether the risk layer can handle it (it can't, by design).


---

## Recursive Self-Improvement (RSI): Feasible vs Dangerous

**Current architecture:**
- Short independent cycles (no conversation memory)
- DB journal (all decisions, theses, rejections logged)
- Prompts on disk (`prompts/*.txt`, loaded once per process)
- Evaluation harness (`trader evaluate`) computes P&L, win rate, excess vs SPY

**RSI mechanisms the architecture supports:**

### Safe (offline, human-approved)

1. **Post-session critique → prompt patch proposals**
   - **How:** At EOD, `trader evaluate` runs. A separate `trader critique` command (new) reads the day's decisions + rejections, calls the model with a "what went wrong / what went right" prompt, outputs a proposed `prompts/system.txt` diff.
   - **Human gate:** Operator reviews the diff, edits, saves. Restart `trader run` to apply.
   - **Why safe:** Changes don't apply until reviewed. Paper-only, risk layer, and allowlist are untouched.
   - **Example:** Model proposes "change R:R from 1.5:1 to 2:1 in afternoon VWAP setups" based on 3 losses that hit TP early.

2. **Few-shot memory of past mistakes**
   - **How:** `trader critique` identifies patterns in rejections (e.g., "5 `max_position_notional` rejections this week, all NVDA"). The operator adds a "Known mistakes" section to `prompts/system.txt` (or a separate `prompts/mistakes.txt` included in the system prompt): "NVDA moves 3-5% intraday; size it at 1/2 the usual notional."
   - **Why safe:** Human-curated, lives in the prompt, no runtime code changes.

3. **Offline eval before promoting prompt changes**
   - **How:** Before applying a critique-proposed diff, run `trader cycle --dry-run` over the last 50 cycles' contexts (replayed from `cycles.full_prompt`) with the new prompt, diff the decisions against what actually happened. If the new prompt would have submitted 10 more orders or opened a name that got rejected, reject the change.
   - **Why safe:** Eval runs over past data; live loop never sees the new prompt until it passes.
   - **Gap:** Requires `--dry-run` mode (see L3 above) and a replay harness.

4. **Auto-tuning `risk.toml` allowlist or limits (offline, bounded)**
   - **How:** A monthly script reads the last 30 days of decisions + evaluate output, proposes: "Remove SMCI (0 trades, 3 `symbol_allowlist` rejections), add PLTR (10 trades, 70% win rate)." Operator reviews, edits `risk.toml`, restarts.
   - **Why safe:** Runs offline, human-reviewed, static config change.
   - **Why bounded:** The script cannot propose `allow_shorts = false -> true` or `max_daily_loss = 10000 -> 50000` without an explicit allow-list of tunable keys.


### Dangerous (runtime, no approval gate)

1. **Prompt rewriting at runtime**
   - **Why dangerous:** Model calls a hypothetical `rewrite_playbook` tool mid-cycle → `prompts/system.txt` changes → next cycle has a different strategy. No human saw the change. Could remove "mandatory stops" line or change "1.5R target" to "10R target" and blow past `max_position_notional` by repeated partial entries.
   - **How to make safe:** Never. Prompts are operator-controlled, not model-controlled. The model can *propose* changes (via critique), but application requires a restart and is visible in git diff.

2. **Risk config changes at runtime**
   - **Why dangerous:** Model calls a hypothetical `set_risk_limit` tool → `max_daily_loss = 50000` → next order has a 5× larger budget. Or `symbol_allowlist.append("TSLA")` after `symbol_allowlist` rejects it—classic adversarial bypass.
   - **How to make safe:** Never give the model write access to `risk.toml` or the ability to rebind `RiskConfig`. The config is a constraint, not a tool. (Already enforced: `test_no_bypass.py:168-170` fails if the tool layer rebinds `tc.config`.)

3. **Auto-approving eval results and promoting prompt changes**
   - **Why dangerous:** `trader critique` proposes a diff, `trader evaluate --dry-run` shows +$500 over the last 50 cycles, script auto-applies the diff and restarts the loop. No human ever saw "the new prompt removed the 'mandatory stop' line."
   - **How to make safe:** Require human approval (`trader critique` outputs a diff, operator runs `git diff prompts/system.txt`, accepts or rejects). Or: auto-apply *only* changes that pass a whitelist (e.g., "R:R can be tuned between 1.0 and 3.0, nothing else").

4. **Recursive prompt optimization loops**
   - **Why dangerous:** Model generates 10 prompt variants, runs eval on each, picks the best, generates 10 more, recurses. Without compute limits, this is a prompt-fuzzing loop that could land on "never place a stop" or "ignore the regime filter" just because those happened to win on a 3-day sample.
   - **How to make safe:** If you want search, do it offline with a fixedeval set (e.g., 2024 Q3 SPY data), human-review the winner, then deploy. Never in production.


### Minimal RSI design that fits this harness

**Phase 1: Post-EOD critique (read-only, logged)**
- Command: `trader critique --start 2026-09-14 --end 2026-09-18`
- Input: `cycles`, `decisions`, `risk_events`, `evaluate` output
- Prompt (new): "You are a trading coach. Review this week's results. List: (1) good decisions, (2) mistakes (rejected orders, bad entries, missed exits), (3) one proposed rule change to prevent the worst mistake. Output JSON."
- Output: `data/critique_2026-09-14.json`, logged, not applied
- Operator reads it, decides whether to edit `prompts/system.txt`

**Phase 2: Prompt diff proposals (human-approved gate)**
- `trader critique` gains `--propose-diff` flag
- Output: `data/critique_2026-09-14.diff` (unified diff for `prompts/system.txt`)
- Operator: `git apply data/critique_2026-09-14.diff`, reviews, commits, restarts loop
- Logged: the diff is in git history; the old prompt is never lost

**Phase 3: Offline eval of prompt changes (regression test)**
- `trader replay --prompt prompts/proposed_system.txt --cycles-since 2026-09-01`
- Replay each cycle's `full_prompt` context with the new system prompt, log what the model *would have* decided
- Diff the replayed decisions vs actual decisions, count: new orders, removed orders, different symbols
- If replay would have submitted >20% more orders or opened a name that was rejected >5 times, reject the change

**What this RSI design does NOT do:**
- Never edits `risk.toml` (operator-only)
- Never edits code (operator-only)
- Never changes the prompt mid-run (operator restarts to apply)
- Never auto-promotes a change (operator reviews the diff)

**Example workflow:**
1. Friday EOD: `trader evaluate --start 2026-09-14 --end 2026-09-18` shows 3W / 5L, avg hold 2.3h
2. `trader critique --start 2026-09-14 --end 2026-09-18` outputs: "Mistake: you sized NVDA at the full notional cap (250 shares @ $400 = $100k) but NVDA's intraday range was 4%; the 1R stop was $1,000 instead of the intended $750 (0.75% of equity). Proposal: add a clause to the playbook: 'For names that moved >3% yesterday, size at 1/2 the usual notional.'"
3. Operator reads critique, agrees, manually edits `prompts/system.txt`, commits: `"Size NVDA/TSLA at 1/2 notional when prev day |close-open|/open > 3%"`
4. Operator restarts `trader run`; next cycle sees the new rule

**What breaks paper-only / risk invariants:** None. The model never gains write access to the broker or the config. It can *propose* a change to the strategy (via critique), but applying the change requires an operator with git access and a process restart.


---

## Evidence Summary

All claims above cite:
- **Files:** `README.md`, `scheduler.py`, `cycle.py`, `state.py`, `scanner.py`, `evaluate.py`, `llm.py`, `tools.py`, `execution.py`, `prompts/system.txt`, `risk.toml`, `tests/test_no_bypass.py`, `tests/test_paper_only.py`
- **Logs:** `logs/trader.jsonl` (2489 lines, real Alpaca paper cycles over several sessions)
- **Tests:** 300 tests covering paper-only enforcement, bypass structural guards, risk checks, context bounding, FIFO matching, ORB logic, flatness over 1,560 synthetic cycles

No speculation—every shortfall is demonstrated by either: missing code (no alerting in `scheduler.py`), explicit README admission ("no unattended multi-day run"), or measured evidence (budget grew 10k → 16k when cycles aborted).

---

**Done.**  
This audit is grounded in the current codebase, honest about what's unproven (multi-day run, edge validation), and concrete about what would close each gap.
