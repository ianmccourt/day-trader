"""Immutable facts about the deployment target.

The paper-trading endpoint lives here as a single module-level constant and is
never read from config, argv, or the environment. SPEC.md constraint #1: there
is deliberately no affordance in this repo for pointing the harness at live
trading. Enabling live would be a reviewed code change to this file, not a
runtime toggle.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

#: The only Alpaca endpoint this repo may ever talk to.
ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"

#: `TRADER_BROKER` values. `paper` is the only default. This is a product
#: switch (Alpaca paper vs Robinhood Agentic MCP), not an Alpaca URL flip.
BROKER_PAPER = "paper"
BROKER_ROBINHOOD_AGENTIC = "robinhood_agentic"
BROKER_MODES = frozenset({BROKER_PAPER, BROKER_ROBINHOOD_AGENTIC})

#: Guard value. `tests/test_paper_only.py` greps the source tree for this and
#: fails the build if it appears anywhere outside a test or a comment.
ALPACA_LIVE_BASE_URL_FORBIDDEN = "https://api.alpaca.markets"

#: Exchange-local timezone. All "trading day" and RTH reasoning happens here;
#: everything persisted is UTC ISO-8601.
MARKET_TZ = ZoneInfo("America/New_York")

#: Regular trading hours, exchange-local. The broker clock is authoritative for
#: holidays and early closes; these are the fallback bounds and the window the
#: scheduler wakes inside.
RTH_OPEN = (9, 30)
RTH_CLOSE = (16, 0)

#: Model used from Phase 3 onward. Current-generation Sonnet: adaptive thinking
#: is the only on-mode, `budget_tokens` is rejected, and effort adds `xhigh`.
ANTHROPIC_MODEL = "claude-sonnet-5"

#: Hard ceiling on assembled prompt size, enforced before every API call
#: (SPEC.md constraint #4). Raised from 10k when the precomputed market scan
#: and open-orders sections joined the state block, then from 12k after
#: 2026-09-11: five cycles aborted at 10.0–10.9k during the 09:50–10:15 ET
#: primary entry window, and the day's trade-placing cycle peaked at 10,972 —
#: the playbook's own mandated workflow (open-risk check, quote, bars, order)
#: plus adaptive-thinking blocks echoed back per the API contract does not fit
#: in 12k with headroom. 16k covers the measured worst case plus one more
#: thinking-heavy iteration; the loop stays bounded by MAX_TOOL_ITERATIONS and
#: the tool-result character caps, not by this assertion firing mid-cycle.
MAX_PROMPT_TOKENS = 16_000

#: Exchange-local (hour, minute) after which the playbook forbids new entries
#: (prompts/system.txt session clock: 14:30–15:30 manage/flatten, 15:30–16:00
#: flatten). The cycle runner skips the LLM entirely after this time when the
#: account is flat with no resting orders — the playbook guarantees the answer
#: would be no_action, so the tokens buy nothing.
NO_NEW_ENTRIES_AFTER_ET = (14, 30)

#: Thesis rationale cap, mirrored by a CHECK constraint in the schema.
MAX_THESIS_RATIONALE_CHARS = 800
