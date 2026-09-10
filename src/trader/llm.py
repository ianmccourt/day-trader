"""The Anthropic-backed agent: one short, independent invocation per cycle.

A manual tool loop rather than the SDK's tool runner. SPEC.md asks to own every
failure mode, and the loop needs to interpose the risk layer between the model's
proposal and the broker, then hand the rejection back as a tool result.

The context budget (SPEC.md constraint #4) is asserted before *every* request in
the loop, not just the first — tool results accumulate in the message array, so
the last iteration is the one that would blow the budget.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import anthropic

from trader.agent import AgentResult
from trader.broker import Broker
from trader.constants import ANTHROPIC_MODEL, MAX_PROMPT_TOKENS
from trader.execution import ExecutionResult
from trader.prompts import DEFAULT_PROMPT_DIR, load, render
from trader.risk.config import RiskConfig
from trader.state import CycleContext, render_state
from trader.tools import TOOL_SCHEMAS, ToolContext, dispatch

log = logging.getLogger("trader.llm")

#: Ceiling on loop iterations. The model can read a few times and write several
#: orders; anything beyond this is a loop, not deliberation.
MAX_TOOL_ITERATIONS = 8

#: Small on purpose. Output becomes input on the next iteration, so a generous
#: cap here spends the prompt budget from constraint #4.
MAX_OUTPUT_TOKENS = 4096

#: Truncation applied to one tool result before it goes back into the messages.
MAX_TOOL_RESULT_CHARS = 1_500

#: Cumulative ceiling on tool-result text across the whole cycle. Per-result
#: truncation alone does not bound the loop: six iterations of parallel calls
#: would still be tens of thousands of characters, and every one of them is
#: input tokens on the next request. Past this, results are replaced by a short
#: note — the loop stays bounded by construction rather than by the budget
#: assertion firing and failing the cycle.
#: Measured, not guessed: dense JSON (bar data, prices) tokenizes at roughly
#: 1.5 chars per token, so this is ~2,700 tokens — which is what fits beside a
#: worst-case state block inside MAX_PROMPT_TOKENS.
MAX_TOTAL_TOOL_RESULT_CHARS = 4_000


class ContextBudgetExceeded(RuntimeError):
    """The assembled prompt would exceed MAX_PROMPT_TOKENS.

    Raised, never worked around. If this fires, the fix is to render less state
    or cap a tool result — not to raise the ceiling.
    """


class AnthropicAgent:
    """Implements the Agent protocol. One API conversation, discarded per cycle."""

    def __init__(
        self,
        conn: Any,
        broker: Broker,
        config: RiskConfig,
        *,
        api_key: str | None = None,
        model: str = ANTHROPIC_MODEL,
        prompt_dir: Path = DEFAULT_PROMPT_DIR,
        max_tool_iterations: int = MAX_TOOL_ITERATIONS,
        max_prompt_tokens: int = MAX_PROMPT_TOKENS,
        thinking: bool = True,
        effort: str = "medium",
        client: Any | None = None,
    ) -> None:
        self.conn = conn
        self.broker = broker
        self.config = config
        self.model = model
        self.prompt_dir = prompt_dir
        self.max_tool_iterations = max_tool_iterations
        self.max_prompt_tokens = max_prompt_tokens
        self.thinking = thinking
        self.effort = effort
        self.client = client or anthropic.Anthropic(api_key=api_key)

    # --- budget ------------------------------------------------------------

    def _assert_budget(self, system: str, messages: list[dict[str, Any]], label: str) -> int:
        """Count the real assembled prompt and raise if it is over budget.

        Counted through the API's own tokenizer, not an estimate — a local
        approximation that drifts from the server's count would make the
        assertion meaningless.
        """
        counted = self.client.messages.count_tokens(
            model=self.model, system=system, messages=messages, tools=TOOL_SCHEMAS
        )
        tokens = int(counted.input_tokens)
        if tokens > self.max_prompt_tokens:
            raise ContextBudgetExceeded(
                f"assembled prompt is {tokens} tokens at {label}, over the "
                f"{self.max_prompt_tokens} budget. Cycle aborted before the API call."
            )
        log.debug("context_budget", extra={"tokens": tokens, "at": label})
        return tokens

    # --- request -----------------------------------------------------------

    def _create(self, system: str, messages: list[dict[str, Any]]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "output_config": {"effort": self.effort},
        }
        if self.thinking:
            # Adaptive is the current shape for 4.6+; budget_tokens is deprecated.
            kwargs["thinking"] = {"type": "adaptive"}
        return self.client.messages.create(**kwargs)

    # --- the loop ----------------------------------------------------------

    def run(self, ctx: CycleContext) -> AgentResult:
        system = load("system", self.prompt_dir)
        user_turn = render("cycle_user", self.prompt_dir, state=render_state(ctx))
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_turn}]

        tc = ToolContext(conn=self.conn, broker=self.broker, ctx=ctx, config=self.config)
        tool_calls: list[dict[str, Any]] = []
        tool_result_chars = 0
        prompt_tokens = completion_tokens = 0
        peak_prompt_tokens = 0
        final_text = ""
        stop_reason = None

        for iteration in range(self.max_tool_iterations):
            peak_prompt_tokens = max(
                peak_prompt_tokens,
                self._assert_budget(system, messages, f"iteration {iteration}"),
            )
            response = self._create(system, messages)
            prompt_tokens += response.usage.input_tokens
            completion_tokens += response.usage.output_tokens
            stop_reason = response.stop_reason

            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                final_text = (
                    f"model refused: {getattr(details, 'category', None)} "
                    f"{getattr(details, 'explanation', '')}".strip()
                )
                log.warning("model_refusal", extra={"cycle_id": ctx.cycle_id})
                break

            final_text = "\n".join(b.text for b in response.content if b.type == "text").strip()

            if stop_reason != "tool_use":
                break

            # Echo the assistant turn back verbatim, thinking blocks included.
            messages.append({"role": "assistant", "content": response.content})

            results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                # Tool inputs are parsed JSON from the SDK; never string-matched.
                args = dict(block.input) if isinstance(block.input, dict) else {}
                text, is_error = dispatch(tc, block.name, args)
                if len(text) > MAX_TOOL_RESULT_CHARS:
                    text = text[:MAX_TOOL_RESULT_CHARS] + "…(truncated)"
                if tool_result_chars + len(text) > MAX_TOTAL_TOOL_RESULT_CHARS:
                    log.warning(
                        "tool_result_budget_exhausted",
                        extra={"cycle_id": ctx.cycle_id, "tool": block.name},
                    )
                    text = (
                        "error: this cycle's tool-output budget is exhausted. "
                        "Decide with what you already have, or end your turn."
                    )
                    is_error = True
                tool_result_chars += len(text)
                tool_calls.append(
                    {
                        "iteration": iteration,
                        "name": block.name,
                        "input": args,
                        "is_error": is_error,
                        "result": text,
                    }
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                        "is_error": is_error,
                    }
                )
            # All results go back in one user message — splitting them trains
            # the model out of parallel tool calls.
            messages.append({"role": "user", "content": results})
        else:
            log.warning(
                "tool_loop_exhausted",
                extra={"cycle_id": ctx.cycle_id, "iterations": self.max_tool_iterations},
            )

        executions: list[ExecutionResult] = list(tc.executions)
        action, symbol, qty = _summarise(executions)

        return AgentResult(
            action=action,
            reasoning=final_text or "(model produced no text)",
            symbol=symbol,
            qty=qty,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            # The full prompt is the whole assembled conversation, not just the
            # first turn — that is what I need to be able to diff across cycles.
            full_prompt=json.dumps(
                {
                    "system": system,
                    "messages": _serialisable(messages),
                    "tools": [t["name"] for t in TOOL_SCHEMAS],
                    "peak_prompt_tokens": peak_prompt_tokens,
                    "budget": self.max_prompt_tokens,
                },
                indent=2,
                default=str,
            ),
            full_response=final_text,
            tool_calls=tool_calls,
            executions=executions,
            stop_reason=stop_reason,
        )


def _summarise(executions: list[ExecutionResult]) -> tuple[str, str | None, float | None]:
    """What the cycle records as its action, derived from what actually happened."""
    if not executions:
        return "no_action", None, None
    last = executions[-1]
    return last.proposal.action, last.proposal.symbol, last.proposal.qty


def _serialisable(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SDK content blocks are Pydantic models; make them JSON for the log."""
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue
        blocks = [
            block.model_dump() if hasattr(block, "model_dump") else block for block in content
        ]
        out.append({"role": message["role"], "content": blocks})
    return out
