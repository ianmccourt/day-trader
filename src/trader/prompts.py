"""Prompt loading. Prompt text lives in prompts/*.txt, never inline in code.

Templates use `{placeholder}` and are filled by explicit keyword, not by
str.format on arbitrary input — rendered state can contain braces, and a stray
one silently blowing up a cycle is not a failure mode worth having.
"""

from __future__ import annotations

import string
from functools import lru_cache
from pathlib import Path

DEFAULT_PROMPT_DIR = Path("prompts")


class PromptError(RuntimeError):
    """A prompt file is missing or does not have the placeholders we expect."""


@lru_cache(maxsize=32)
def _read(path: Path) -> str:
    if not path.is_file():
        raise PromptError(f"prompt file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise PromptError(f"prompt file is empty: {path}")
    return text


def load(name: str, prompt_dir: Path = DEFAULT_PROMPT_DIR) -> str:
    """Read `prompts/<name>.txt` verbatim."""
    return _read(prompt_dir / f"{name}.txt")


def render(name: str, prompt_dir: Path = DEFAULT_PROMPT_DIR, **values: str) -> str:
    """Fill a template's `{placeholders}`, failing loudly on any mismatch."""
    template = load(name, prompt_dir)
    expected = {
        field
        for _, field, _, _ in string.Formatter().parse(template)
        if field is not None
    }
    missing = expected - set(values)
    extra = set(values) - expected
    if missing or extra:
        raise PromptError(
            f"{name}.txt placeholder mismatch: missing={sorted(missing)} "
            f"unexpected={sorted(extra)}"
        )
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", value)
    return result
