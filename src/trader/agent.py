"""Agent interface plus the Phase 1 stub.

The stub exists so the loop can be proven end to end — scheduling, state
assembly, persistence, restart recovery — with zero LLM involvement. Phase 3
adds an Anthropic-backed implementation of the same Protocol; nothing in
cycle.py should need to change when it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from trader.state import CycleContext, render_state

if TYPE_CHECKING:
    from trader.execution import ExecutionResult


@dataclass(frozen=True, slots=True)
class AgentResult:
    """What one agent invocation produced.

    `executions` is non-empty when the agent already routed an order through
    `execution.place_order` from inside its tool loop — which the tool-using
    agent does, because the model has to see the risk verdict as a tool result.
    A stub or scripted agent leaves it empty and lets run_cycle do the routing.
    Either way the order passed the same single gated path.
    """

    action: str  # "no_action", or the "buy"/"sell" the agent settled on
    reasoning: str
    symbol: str | None = None
    qty: float | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    full_prompt: str | None = None
    full_response: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    executions: list[ExecutionResult] = field(default_factory=list)
    stop_reason: str | None = None


class Agent(Protocol):
    def run(self, ctx: CycleContext) -> AgentResult: ...


class StubAgent:
    """Always declines to trade. Phase 1's 'intelligence'."""

    name = "stub"

    def run(self, ctx: CycleContext) -> AgentResult:
        prompt = render_state(ctx)
        return AgentResult(
            action="no_action",
            reasoning="stub agent: no trading logic wired up yet (Phase 1)",
            model=self.name,
            # Logged even for the stub so the cycles table has the same shape
            # in Phase 1 as it will in Phase 3 and I can diff across phases.
            full_prompt=prompt,
            full_response="no_action",
            prompt_tokens=None,
            completion_tokens=None,
            tool_calls=[],
        )
