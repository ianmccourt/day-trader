"""Prompt text lives in files, not in code, and the templates must line up."""

from __future__ import annotations

from pathlib import Path

import pytest

from trader import prompts

REPO = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO / "prompts"


def test_the_shipped_prompts_load() -> None:
    assert prompts.load("system", PROMPT_DIR).startswith("You are the decision-making component")
    assert "{state}" in prompts.load("cycle_user", PROMPT_DIR)


def test_rendering_substitutes_the_state() -> None:
    out = prompts.render("cycle_user", PROMPT_DIR, state="## Cycle\ncycle_id: 7")
    assert "cycle_id: 7" in out
    assert "{state}" not in out


def test_a_missing_value_is_fatal() -> None:
    with pytest.raises(prompts.PromptError, match="missing="):
        prompts.render("cycle_user", PROMPT_DIR)


def test_an_unexpected_value_is_fatal() -> None:
    """Catches a renamed placeholder instead of silently dropping the state."""
    with pytest.raises(prompts.PromptError, match="unexpected="):
        prompts.render("cycle_user", PROMPT_DIR, state="s", extra="x")


def test_braces_in_the_state_survive_rendering() -> None:
    """Rendered state can contain JSON; str.format would choke on it."""
    out = prompts.render("cycle_user", PROMPT_DIR, state='{"a": 1}')
    assert '{"a": 1}' in out


def test_a_missing_file_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(prompts.PromptError, match="not found"):
        prompts.load("nope", tmp_path)


def test_an_empty_file_is_fatal(tmp_path: Path) -> None:
    (tmp_path / "blank.txt").write_text("   \n")
    with pytest.raises(prompts.PromptError, match="empty"):
        prompts.load("blank", tmp_path)


def test_no_prompt_text_is_inlined_in_the_source() -> None:
    """SPEC.md Phase 3: prompt structure lives in prompts/, not inline strings."""
    for path in (REPO / "src" / "trader").rglob("*.py"):
        text = path.read_text()
        assert "You are " not in text, f"prompt text inlined in {path.name}"


def test_the_system_prompt_states_the_constraints_but_no_strategy() -> None:
    """SPEC.md: a placeholder, with no edge-seeking instructions."""
    system = prompts.load("system", PROMPT_DIR).lower()
    assert "placeholder" in system
    assert "place_order" in system
    assert "risk layer" in system
    assert "kill switch" in system
    # Nothing that would amount to a strategy.
    for banned in ("moving average", "rsi", "momentum", "buy low", "oversold", "breakout"):
        assert banned not in system, f"strategy hint {banned!r} leaked into the system prompt"
