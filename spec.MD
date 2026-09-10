# SPEC.md — Agentic paper-trading harness

## What we're building

A long-horizon LLM trading agent that runs during US market hours against
**Alpaca's paper trading API**. The LLM proposes trades; deterministic code
decides whether they execute.

This is an experiment in long-horizon agent architecture. Treat profitability
as out of scope — the deliverable is a correct, observable, safe harness.

## Non-negotiable constraints

Read these before writing any code. They are not suggestions.

1. **Paper trading only.** The Alpaca base URL is hard-coded to the paper
   endpoint in a single constant. There is no code path, config flag, or env
   var in this repo that can point it at live trading. If I later want live,
   that will be a deliberate, reviewed change — do not build the affordance now.
2. **The LLM has exactly one write tool.** Everything else it can call is
   read-only. No shell access, no arbitrary HTTP, no database writes except
   through defined helpers.
3. **The risk layer cannot be bypassed.** `place_order` routes through it
   unconditionally. It is pure functions over (proposal, current_state), unit
   tested independently of the LLM. If a check fails, the order is rejected and
   the rejection is returned to the model as a tool result.
4. **Context budget is enforced, not hoped for.** Assert the assembled prompt
   is under 10k tokens before every API call. Raise on violation. The whole
   design goal is that cycle 200 looks like cycle 1.
5. **No secrets in code.** API keys from environment only. `.env` in
   `.gitignore` from the first commit.

## Architecture

Do **not** run one long conversation across the trading day. Run a scheduled
loop of short, independent agent invocations with state held externally.

```
APScheduler (every N min, RTH only)
  └─> build_cycle_context()      # deterministic, from DB + broker
       └─> single Anthropic API call w/ tools
            └─> tool loop (read tools, then up to N write proposals)
                 └─> risk layer → broker OR rejection
                      └─> persist decision + full transcript
                           └─> tear down context entirely
```

Each cycle's prompt is: system prompt + rendered state + delta since last cycle.
Nothing carries over in the message array. Everything that matters is in the DB.

## Stack

- Python 3.11+, `uv` for dependency management
- `alpaca-py` for broker and market data
- `anthropic` SDK, model `claude-sonnet-4-6`
- SQLite via plain `sqlite3` or SQLModel — no heavyweight ORM
- APScheduler for the cycle loop
- `pytest` for the risk layer tests
- Structured logging to both stdout and a `cycles` table

Prefer stdlib and thin wrappers. Do not introduce LangChain, LlamaIndex, or any
agent framework. The loop is ~200 lines and I want to own every failure mode.

## State schema (starting point — adjust if you have a good reason, and say why)

- `positions_snapshot` — symbol, qty, avg_price, unrealized_pl, captured_at.
  Always read fresh from the broker each cycle; never trusted from memory.
- `theses` — symbol, opened_at, rationale (TEXT, hard-capped ~800 chars),
  invalidation_condition, status. The agent's own reasoning per open position.
- `decisions` — cycle_id, timestamp, action, symbol, qty, reasoning,
  risk_result, broker_order_id, outcome.
- `cycles` — cycle_id, started_at, prompt_tokens, completion_tokens,
  full_prompt, full_response, tool_calls (JSON), duration_ms, error.
- `risk_events` — every rejection, with which check fired and the proposal.

Log the full prompt and response for every cycle. I will spend more time reading
these than reading the code, so make them easy to query and diff.

## Risk layer

Implement as independent predicates in `risk/checks.py`, each
`(proposal, state) -> (bool, reason)`. Configurable via a single `risk.yaml`:

- `max_position_notional` — per symbol
- `max_total_exposure` — portfolio-wide
- `max_daily_loss` — halt trading for the day when breached
- `symbol_allowlist` — explicit list; nothing else is tradeable
- `max_orders_per_hour` and `max_orders_per_day`
- `regular_trading_hours_only`
- `kill_switch` — reads a flag from the DB, checked at the top of every cycle
  and again before every order. When set, the process logs and idles.

Every check gets a unit test with a passing case, a failing case, and a boundary
case. These tests are the most important code in the repo.

## Build order

Do not attempt this in one pass. Each phase ends with something runnable that I
can inspect before you continue. Stop and check in at each boundary.

**Phase 1 — Loop without intelligence.**
Scheduler, DB schema, Alpaca connection, state rendering, full logging. The
"agent" is a stub that always returns "no action." Prove the loop runs a full
session, survives a restart mid-day, and reconstructs state correctly from the DB.

**Phase 2 — Risk layer.**
All checks plus tests, wired to a fake broker. Prove that a deliberately
malicious set of proposals (oversized, off-allowlist, after hours, rapid-fire)
is rejected every time. No LLM involved yet.

**Phase 3 — The agent.**
Anthropic call, tool definitions, tool loop, context budget assertion. Read
tools first, then the single write tool. Prompt structure lives in a separate
`prompts/` directory as plain text files, not inline strings.

**Phase 4 — Evaluation harness.**
Given a date range of logged cycles, report: return vs. buy-and-hold SPY over
the same window, number of trades, win rate, average holding period, count and
breakdown of risk rejections. I need to be able to tell whether the agent beat
doing nothing.

## Strategy

Deliberately unspecified. The system prompt in `prompts/` should be a
placeholder that states the agent's constraints and available tools but gives it
no edge-seeking instructions. I'll write that part myself once the harness works.
Do not invent a trading strategy or fill this in with indicator heuristics.

## Style

- Type hints throughout, `ruff` clean.
- Errors surface loudly. No bare `except:`. A broker timeout should fail the
  cycle and log, not silently skip.
- Comments explain *why*, not *what*.
- Small commits per phase with clear messages.

## Ask me before

- Adding any dependency not listed above
- Changing the schema in a way that loses logged history
- Anything that touches order submission logic outside the risk layer
- Any design tradeoff where you had to guess my intent