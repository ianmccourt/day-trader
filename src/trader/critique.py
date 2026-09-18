"""Post-session critique: a coaching LLM reads the week's results and proposes a playbook change.

Phase 1-2 of the RSI design from SHORTFALLS.md. This module never applies changes
automatically — it journals the critique and optionally produces a diff that the
operator reviews and applies manually.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic

from trader.broker import Broker
from trader.db import iso, utcnow
from trader.evaluate import evaluate
from trader.prompts import DEFAULT_PROMPT_DIR, PromptError, load


#: Where critique artifacts land. Append-only / auditable.
DEFAULT_CRITIQUE_DIR = Path("data/critiques")

#: Which prompts the diff generator is allowed to target. Never risk.toml, src/, tests.
ALLOWED_PROMPT_FILES = frozenset({"system.txt", "cycle_user.txt"})

#: Stub critique for `--stub` mode (no API key needed, like cycle --stub elsewhere)
STUB_CRITIQUE = {
    "good_decisions": ["(stub mode: no real critique)"],
    "mistakes": [],
    "proposed_rule_change": "No rule change needed this period.",
}


class CritiqueError(RuntimeError):
    """Critique generation failed (bad date range, model error, parse error)."""


@dataclass(frozen=True, slots=True)
class Critique:
    """One critique artifact: coach output plus metadata for the journal."""

    window_start: str
    window_end: str
    critique_at: str
    system_prompt_sha256: str
    good_decisions: list[str]
    mistakes: list[str]
    proposed_rule_change: str
    #: The full evaluation summary that was sent to the coach (for replay/audit)
    eval_summary: dict[str, Any]
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format_context(
    start: str, end: str, report: dict[str, Any], decisions: list[dict[str, Any]],
    rejections: list[dict[str, Any]]
) -> str:
    """Render the evaluation + decisions + rejections into prose for the coach."""
    lines = [
        f"## Trading Window: {start} to {end}",
        "",
        "## Summary",
        f"- Cycles: {report.get('cycles', 0)} ({report.get('cycles_by_status', {})})",
        f"- Orders submitted: {report.get('orders_submitted', 0)}",
        f"- Orders filled: {report.get('orders_filled', 0)}",
        f"- Round trips: {len(report.get('round_trips', []))}",
        f"- Win rate: {report.get('win_rate_pct', 'n/a')}%",
        f"- Realized P&L: ${report.get('realized_pl', 0):.2f}",
        f"- Strategy return: {report.get('strategy_return_pct', 'n/a')}%",
        f"- vs buy-and-hold SPY: {report.get('excess_vs_benchmark_pct', 'n/a')}%",
        "",
        "## Recent Decisions (last 30)",
    ]
    if not decisions:
        lines.append("(no decisions in this window)")
    else:
        for d in decisions[-30:]:
            line = (
                f"- cycle {d['cycle_id']} @ {d['timestamp']}: {d['action']} "
                f"{d.get('symbol', 'n/a')} qty={d.get('qty', 'n/a')} "
                f"-> {d.get('risk_result', 'n/a')} ({d.get('outcome', 'n/a')})"
            )
            lines.append(line)

    lines.extend(["", "## Risk Rejections"])
    if not rejections:
        lines.append("(no rejections in this window)")
    else:
        by_check: dict[str, int] = {}
        for r in rejections:
            by_check[r["check_name"]] = by_check.get(r["check_name"], 0) + 1
        for check, count in sorted(by_check.items(), key=lambda x: -x[1]):
            lines.append(f"- {count}x {check}")
        lines.append("")
        lines.append("Most recent rejections:")
        for r in rejections[-10:]:
            lines.append(
                f"- cycle {r['cycle_id']} @ {r['timestamp']}: {r['check_name']} — {r['reason']}"
            )

    return "\n".join(lines)


def _call_coach(
    context: str, *, api_key: str, model: str = "claude-sonnet-4.5-20241022"
) -> tuple[dict[str, Any], int, int]:
    """Call the critique prompt. Returns (parsed_json, prompt_tokens, completion_tokens)."""
    try:
        system = load("critique")
    except PromptError as exc:
        raise CritiqueError(f"critique prompt not found: {exc}") from exc

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system.replace("{context}", context),
            messages=[{"role": "user", "content": "Please provide your critique as JSON."}],
        )
    except anthropic.APIError as exc:
        raise CritiqueError(f"Anthropic API error: {exc}") from exc

    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    # The model often wraps JSON in markdown fences; strip them
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if len(lines) > 2 else lines)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CritiqueError(f"coach output is not valid JSON: {exc}\n{text}") from exc

    if not isinstance(parsed, dict):
        raise CritiqueError(f"coach output is not a JSON object: {type(parsed)}")

    required = {"good_decisions", "mistakes", "proposed_rule_change"}
    missing = required - set(parsed.keys())
    if missing:
        raise CritiqueError(f"coach output missing keys: {sorted(missing)}")

    return parsed, response.usage.input_tokens, response.usage.output_tokens


def generate_critique(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    broker: Broker | None = None,
    api_key: str | None = None,
    stub: bool = False,
    prompt_dir: Path = DEFAULT_PROMPT_DIR,
) -> Critique:
    """Generate a critique over a date range. Reads DB + calls evaluate + calls coach LLM."""
    if start > end:
        raise CritiqueError(f"start {start} is after end {end}")

    # Reuse evaluate to get the summary
    report_obj = evaluate(conn, start, end, broker=broker)
    report = report_obj.to_dict()

    # Fetch recent decisions for context
    decisions = [
        dict(row)
        for row in conn.execute(
            "SELECT d.cycle_id, d.timestamp, d.action, d.symbol, d.qty, "
            "d.risk_result, d.outcome FROM decisions d "
            "JOIN cycles c USING (cycle_id) "
            "WHERE c.trading_day BETWEEN ? AND ? ORDER BY d.id",
            (start, end),
        ).fetchall()
    ]

    # Fetch risk rejections
    rejections = [
        dict(row)
        for row in conn.execute(
            "SELECT r.cycle_id, r.timestamp, r.check_name, r.reason FROM risk_events r "
            "JOIN cycles c USING (cycle_id) "
            "WHERE c.trading_day BETWEEN ? AND ? ORDER BY r.id",
            (start, end),
        ).fetchall()
    ]

    context = _format_context(start, end, report, decisions, rejections)

    # Compute SHA of current system.txt
    try:
        system_text = load("system", prompt_dir)
        system_sha = _sha256(system_text)
    except PromptError:
        system_sha = "(system.txt not found)"

    if stub:
        return Critique(
            window_start=start,
            window_end=end,
            critique_at=iso(utcnow()),
            system_prompt_sha256=system_sha,
            good_decisions=STUB_CRITIQUE["good_decisions"],
            mistakes=STUB_CRITIQUE["mistakes"],
            proposed_rule_change=STUB_CRITIQUE["proposed_rule_change"],
            eval_summary=report,
            model="stub",
        )

    if api_key is None:
        raise CritiqueError("Anthropic API key required (not in stub mode)")

    parsed, prompt_tokens, completion_tokens = _call_coach(context, api_key=api_key)

    return Critique(
        window_start=start,
        window_end=end,
        critique_at=iso(utcnow()),
        system_prompt_sha256=system_sha,
        good_decisions=parsed.get("good_decisions") or [],
        mistakes=parsed.get("mistakes") or [],
        proposed_rule_change=parsed.get("proposed_rule_change") or "No rule change needed.",
        eval_summary=report,
        model="claude-sonnet-4.5-20241022",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def save_critique(critique: Critique, output_dir: Path = DEFAULT_CRITIQUE_DIR) -> Path:
    """Write the critique artifact to disk. Returns the path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"critique_{critique.window_start}_{critique.window_end}.json"
    path = output_dir / filename
    path.write_text(json.dumps(critique.to_dict(), indent=2, default=str), encoding="utf-8")
    return path


def generate_diff(
    critique: Critique,
    target: str = "system.txt",
    prompt_dir: Path = DEFAULT_PROMPT_DIR,
    output_dir: Path = DEFAULT_CRITIQUE_DIR,
) -> Path | None:
    """Generate a unified diff for the proposed rule change. Returns path or None if no change."""
    if target not in ALLOWED_PROMPT_FILES:
        raise CritiqueError(
            f"refusing to generate diff for {target!r}; only {sorted(ALLOWED_PROMPT_FILES)} allowed"
        )

    if "no rule change needed" in critique.proposed_rule_change.lower():
        return None

    # Load current prompt
    try:
        current = load(target.removesuffix(".txt"), prompt_dir)
    except PromptError as exc:
        raise CritiqueError(f"cannot load {target}: {exc}") from exc

    # The proposed change is prose, not a real diff. We'll append it as a comment
    # so the operator can manually integrate it. A real diff would require the
    # coach to output precise line numbers and before/after text.
    proposed_section = (
        f"\n\n## Proposed change from critique {critique.window_start} to {critique.window_end}\n"
        f"## Critique says: {critique.proposed_rule_change}\n"
        f"## Review this section and integrate the change manually, then delete this comment.\n"
    )
    modified = current + proposed_section

    # Generate unified diff
    diff_lines = list(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"prompts/{target}",
            tofile=f"prompts/{target}",
            lineterm="",
        )
    )

    if not diff_lines:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"critique_{critique.window_start}_{critique.window_end}.diff"
    diff_path = output_dir / filename
    diff_path.write_text("".join(diff_lines), encoding="utf-8")
    return diff_path


def format_critique(critique: Critique) -> str:
    """Human-readable critique output."""
    lines = [
        f"Critique: {critique.window_start} to {critique.window_end}",
        f"Generated: {critique.critique_at}",
        f"System prompt SHA256: {critique.system_prompt_sha256}",
        "",
        "## Good Decisions",
    ]
    if not critique.good_decisions:
        lines.append("(none noted)")
    else:
        for item in critique.good_decisions:
            lines.append(f"- {item}")

    lines.extend(["", "## Mistakes"])
    if not critique.mistakes:
        lines.append("(none noted)")
    else:
        for item in critique.mistakes:
            lines.append(f"- {item}")

    lines.extend(["", "## Proposed Rule Change", critique.proposed_rule_change])

    if critique.model:
        lines.extend(
            [
                "",
                f"Model: {critique.model}",
                f"Tokens: {critique.prompt_tokens} in / {critique.completion_tokens} out",
            ]
        )

    return "\n".join(lines)
