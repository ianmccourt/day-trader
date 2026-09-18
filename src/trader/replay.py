"""Strategy RSI: offline replay of candidate playbooks against historical contexts.

Given a date window of paper history, this module:
1. Loads frozen cycle contexts from the DB journal
2. Runs decisions with a candidate system prompt (not the active one)
3. Compares replayed decisions vs actual decisions
4. Scores: order counts, symbol changes, budget usage (no fake fills)

Replay never calls the real broker submit. Promotion requires human review.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from trader.broker import Broker
from trader.db import iso, utcnow
from trader.llm import MAX_PROMPT_TOKENS
from trader.prompts import DEFAULT_PROMPT_DIR, PromptError
from trader.tools import TOOL_SCHEMAS


#: Where replay reports land
DEFAULT_REPLAY_DIR = Path("data/replays")

#: Which prompt paths can be promoted to active
ALLOWED_PROMOTE_TARGETS = frozenset({
    Path("prompts/system.txt"),
    Path("prompts/cycle_user.txt"),
})

#: Stub replay for testing
STUB_REPLAY_DECISION = {
    "action": "no_action",
    "symbol": None,
    "qty": None,
    "reasoning": "(stub mode: no real replay)",
}


class ReplayError(RuntimeError):
    """Replay generation failed (bad date range, missing contexts, model error)."""


@dataclass(frozen=True, slots=True)
class ReplayedCycle:
    """One cycle's replay: what the candidate would have decided."""

    cycle_id: int
    actual_action: str
    actual_symbol: str | None
    actual_qty: float | None
    replayed_action: str
    replayed_symbol: str | None
    replayed_qty: float | None
    changed: bool  # True if replay differs from actual
    prompt_tokens: int | None = None
    budget_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Full replay report: all cycles, scoring, metadata."""

    candidate_path: str
    candidate_sha256: str
    window_start: str
    window_end: str
    replayed_at: str
    cycles_total: int
    cycles_changed: int
    orders_added: int  # Replay placed order, actual did not
    orders_removed: int  # Actual placed order, replay did not
    symbols_novel: set[str] = field(default_factory=set)  # Symbols replay would trade but actual didn't
    symbols_avoided: set[str] = field(default_factory=set)  # Symbols actual traded but replay wouldn't
    budget_exceeded_count: int = 0
    model: str | None = None
    replayed_cycles: list[ReplayedCycle] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["symbols_novel"] = sorted(self.symbols_novel)
        d["symbols_avoided"] = sorted(self.symbols_avoided)
        return d


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_candidate(path: Path) -> str:
    """Load a candidate playbook. Raises ReplayError if not found."""
    if not path.is_file():
        raise ReplayError(f"candidate playbook not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ReplayError(f"candidate playbook is empty: {path}")
    return text


def _load_frozen_contexts(
    conn: sqlite3.Connection, start: str, end: str
) -> list[dict[str, Any]]:
    """Load cycle contexts from the DB journal for replay."""
    rows = conn.execute(
        "SELECT cycle_id, full_prompt FROM cycles "
        "WHERE trading_day BETWEEN ? AND ? AND full_prompt IS NOT NULL "
        "ORDER BY cycle_id",
        (start, end),
    ).fetchall()
    
    contexts = []
    for row in rows:
        try:
            prompt_data = json.loads(row["full_prompt"])
            contexts.append({
                "cycle_id": row["cycle_id"],
                "system": prompt_data.get("system", ""),
                "messages": prompt_data.get("messages", []),
            })
        except (json.JSONDecodeError, KeyError):
            # Skip cycles with malformed prompts
            continue
    
    return contexts


def _load_actual_decisions(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[int, dict[str, Any]]:
    """Load actual decisions keyed by cycle_id."""
    rows = conn.execute(
        "SELECT d.cycle_id, d.action, d.symbol, d.qty FROM decisions d "
        "JOIN cycles c USING (cycle_id) "
        "WHERE c.trading_day BETWEEN ? AND ? AND d.action IN ('buy', 'sell', 'no_action') "
        "ORDER BY d.id",
        (start, end),
    ).fetchall()
    
    # Take the first decision per cycle (the primary action)
    decisions: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["cycle_id"] not in decisions:
            decisions[row["cycle_id"]] = {
                "action": row["action"],
                "symbol": row["symbol"],
                "qty": float(row["qty"]) if row["qty"] is not None else None,
            }
    
    return decisions


def _replay_cycle_stub(cycle_id: int) -> dict[str, Any]:
    """Stub replay decision (no API key needed)."""
    return {
        "cycle_id": cycle_id,
        "action": STUB_REPLAY_DECISION["action"],
        "symbol": STUB_REPLAY_DECISION["symbol"],
        "qty": STUB_REPLAY_DECISION["qty"],
        "reasoning": STUB_REPLAY_DECISION["reasoning"],
        "prompt_tokens": 0,
        "budget_exceeded": False,
    }


def _replay_cycle_with_model(
    context: dict[str, Any],
    candidate_system: str,
    *,
    api_key: str,
    model: str = "claude-sonnet-4.5-20241022",
) -> dict[str, Any]:
    """Replay one cycle with the candidate system prompt."""
    cycle_id = context["cycle_id"]
    original_messages = context["messages"]
    
    # Extract the user message (should be the first/only one for a single cycle)
    if not original_messages or original_messages[0].get("role") != "user":
        return {
            "cycle_id": cycle_id,
            "action": "error",
            "symbol": None,
            "qty": None,
            "reasoning": "malformed context: no user message",
            "prompt_tokens": 0,
            "budget_exceeded": False,
        }
    
    user_content = original_messages[0].get("content", "")
    if not isinstance(user_content, str):
        user_content = str(user_content)
    
    # Build replay messages
    messages = [{"role": "user", "content": user_content}]
    
    # Check budget before calling
    client = anthropic.Anthropic(api_key=api_key)
    try:
        counted = client.messages.count_tokens(
            model=model,
            system=candidate_system,
            messages=messages,
            tools=TOOL_SCHEMAS,
        )
        prompt_tokens = int(counted.input_tokens)
        budget_exceeded = prompt_tokens > MAX_PROMPT_TOKENS
    except Exception:
        prompt_tokens = 0
        budget_exceeded = False
    
    if budget_exceeded:
        return {
            "cycle_id": cycle_id,
            "action": "budget_exceeded",
            "symbol": None,
            "qty": None,
            "reasoning": f"candidate prompt would exceed budget: {prompt_tokens} > {MAX_PROMPT_TOKENS}",
            "prompt_tokens": prompt_tokens,
            "budget_exceeded": True,
        }
    
    # Call the model (single turn, no tool loop for replay)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=candidate_system,
            messages=messages,
            tools=TOOL_SCHEMAS,
        )
    except anthropic.APIError as exc:
        return {
            "cycle_id": cycle_id,
            "action": "error",
            "symbol": None,
            "qty": None,
            "reasoning": f"API error: {exc}",
            "prompt_tokens": prompt_tokens,
            "budget_exceeded": False,
        }
    
    # Parse response (look for tool calls)
    text_blocks = [b.text for b in response.content if b.type == "text"]
    tool_calls = [b for b in response.content if b.type == "tool_use"]
    
    # Extract decision from tool calls or text
    action = "no_action"
    symbol = None
    qty = None
    reasoning = " ".join(text_blocks).strip() or "(no text response)"
    
    for tool in tool_calls:
        if tool.name == "place_order":
            input_data = tool.input if isinstance(tool.input, dict) else {}
            action = input_data.get("action", "unknown")
            symbol = input_data.get("symbol")
            qty = input_data.get("qty")
            break
    
    return {
        "cycle_id": cycle_id,
        "action": action,
        "symbol": symbol,
        "qty": qty,
        "reasoning": reasoning[:200],  # Truncate for report
        "prompt_tokens": response.usage.input_tokens,
        "budget_exceeded": False,
    }


def replay_playbook(
    conn: sqlite3.Connection,
    candidate_path: Path,
    start: str,
    end: str,
    *,
    api_key: str | None = None,
    stub: bool = False,
) -> ReplayReport:
    """Replay a candidate playbook against historical contexts."""
    if start > end:
        raise ReplayError(f"start {start} is after end {end}")
    
    # Load candidate
    candidate_text = _load_candidate(candidate_path)
    candidate_sha = _sha256(candidate_text)
    
    # Load frozen contexts and actual decisions
    contexts = _load_frozen_contexts(conn, start, end)
    if not contexts:
        raise ReplayError(f"no cycle contexts found between {start} and {end}")
    
    actual_decisions = _load_actual_decisions(conn, start, end)
    
    # Replay each cycle
    replayed_cycles: list[ReplayedCycle] = []
    orders_added = 0
    orders_removed = 0
    symbols_novel: set[str] = set()
    symbols_avoided: set[str] = set()
    budget_exceeded_count = 0
    
    for context in contexts:
        cycle_id = context["cycle_id"]
        actual = actual_decisions.get(cycle_id, {"action": "no_action", "symbol": None, "qty": None})
        
        # Replay
        if stub:
            replayed = _replay_cycle_stub(cycle_id)
        else:
            if api_key is None:
                raise ReplayError("Anthropic API key required (not in stub mode)")
            replayed = _replay_cycle_with_model(
                context, candidate_text, api_key=api_key
            )
        
        # Compare
        changed = (
            replayed["action"] != actual["action"]
            or replayed["symbol"] != actual["symbol"]
        )
        
        # Track order deltas
        actual_ordered = actual["action"] in ("buy", "sell")
        replayed_ordered = replayed["action"] in ("buy", "sell")
        
        if replayed_ordered and not actual_ordered:
            orders_added += 1
            if replayed["symbol"]:
                symbols_novel.add(replayed["symbol"])
        
        if actual_ordered and not replayed_ordered:
            orders_removed += 1
            if actual["symbol"]:
                symbols_avoided.add(actual["symbol"])
        
        if replayed.get("budget_exceeded"):
            budget_exceeded_count += 1
        
        replayed_cycles.append(ReplayedCycle(
            cycle_id=cycle_id,
            actual_action=actual["action"],
            actual_symbol=actual["symbol"],
            actual_qty=actual["qty"],
            replayed_action=replayed["action"],
            replayed_symbol=replayed["symbol"],
            replayed_qty=replayed["qty"],
            changed=changed,
            prompt_tokens=replayed.get("prompt_tokens"),
            budget_exceeded=replayed.get("budget_exceeded", False),
        ))
    
    cycles_changed = sum(1 for c in replayed_cycles if c.changed)
    
    return ReplayReport(
        candidate_path=str(candidate_path),
        candidate_sha256=candidate_sha,
        window_start=start,
        window_end=end,
        replayed_at=iso(utcnow()),
        cycles_total=len(replayed_cycles),
        cycles_changed=cycles_changed,
        orders_added=orders_added,
        orders_removed=orders_removed,
        symbols_novel=symbols_novel,
        symbols_avoided=symbols_avoided,
        budget_exceeded_count=budget_exceeded_count,
        model="stub" if stub else "claude-sonnet-4.5-20241022",
        replayed_cycles=replayed_cycles,
    )


def save_replay(report: ReplayReport, output_dir: Path = DEFAULT_REPLAY_DIR) -> Path:
    """Write replay report to disk."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"replay_{report.window_start}_{report.window_end}_{report.candidate_sha256[:8]}.json"
    path = output_dir / filename
    path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    return path


def format_replay(report: ReplayReport) -> str:
    """Human-readable replay report."""
    lines = [
        f"Replay Report: {report.candidate_path}",
        f"Window: {report.window_start} to {report.window_end}",
        f"Candidate SHA256: {report.candidate_sha256}",
        f"Replayed at: {report.replayed_at}",
        "",
        "## Summary",
        f"Cycles total: {report.cycles_total}",
        f"Cycles changed: {report.cycles_changed} ({100 * report.cycles_changed / report.cycles_total if report.cycles_total else 0:.1f}%)",
        f"Orders added (replay would place, actual did not): {report.orders_added}",
        f"Orders removed (actual placed, replay would not): {report.orders_removed}",
        "",
        "## Symbols",
        f"Novel (replay would trade, actual didn't): {', '.join(sorted(report.symbols_novel)) or '(none)'}",
        f"Avoided (actual traded, replay wouldn't): {', '.join(sorted(report.symbols_avoided)) or '(none)'}",
    ]
    
    if report.budget_exceeded_count > 0:
        lines.extend([
            "",
            f"⚠️  Budget exceeded in {report.budget_exceeded_count} cycle(s) — candidate prompt too large",
        ])
    
    lines.extend(["", f"Model: {report.model}"])
    
    return "\n".join(lines)


def promote_candidate(
    candidate_path: Path,
    target: Path = Path("prompts/system.txt"),
    *,
    dry_run: bool = True,
) -> str:
    """Promote a candidate to active playbook. Returns diff or error message."""
    # Safety: only allow promoting to specific prompt files
    if target not in ALLOWED_PROMOTE_TARGETS:
        raise ReplayError(
            f"refusing to promote to {target}; only {sorted(str(p) for p in ALLOWED_PROMOTE_TARGETS)} allowed"
        )
    
    # Load candidate
    if not candidate_path.is_file():
        raise ReplayError(f"candidate not found: {candidate_path}")
    
    candidate_text = candidate_path.read_text(encoding="utf-8")
    
    # Load current active
    if not target.is_file():
        raise ReplayError(f"target not found: {target}")
    
    current_text = target.read_text(encoding="utf-8")
    
    # Generate diff
    import difflib
    diff_lines = list(
        difflib.unified_diff(
            current_text.splitlines(keepends=True),
            candidate_text.splitlines(keepends=True),
            fromfile=str(target),
            tofile=str(target),
            lineterm="",
        )
    )
    
    if not diff_lines:
        return "No changes (candidate identical to current active)"
    
    diff_text = "".join(diff_lines)
    
    if dry_run:
        return f"[DRY RUN] Would promote {candidate_path} -> {target}\n\n{diff_text}"
    
    # Actually promote (copy candidate to target)
    target.write_text(candidate_text, encoding="utf-8")
    return f"Promoted {candidate_path} -> {target}\n\n{diff_text}"
