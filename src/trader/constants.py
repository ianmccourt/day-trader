"""Immutable facts about the deployment target.

The paper-trading endpoint lives here as a single module-level constant and is
never read from config, argv, or the environment. SPEC.md constraint #1: there
is deliberately no affordance in this repo for pointing the harness at live
trading. Enabling live would be a reviewed code change to this file, not a
runtime toggle.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

#: The only broker endpoint this repo may ever talk to.
ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"

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

#: Model used from Phase 3 onward.
ANTHROPIC_MODEL = "claude-sonnet-4-6"

#: Hard ceiling on assembled prompt size, enforced before every API call
#: (SPEC.md constraint #4).
MAX_PROMPT_TOKENS = 10_000

#: Thesis rationale cap, mirrored by a CHECK constraint in the schema.
MAX_THESIS_RATIONALE_CHARS = 800
