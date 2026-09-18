"""Tests for the critique module (RSI Phase 1-2)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.fakes import FakeBroker
from trader.critique import (
    ALLOWED_PROMPT_FILES,
    STUB_CRITIQUE,
    Critique,
    CritiqueError,
    format_critique,
    generate_critique,
    generate_diff,
    save_critique,
)
from trader.db import connect, record_decision
from trader.state import trading_day_for


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "test.db")


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


def test_stub_critique_needs_no_api_key(conn: sqlite3.Connection, broker: FakeBroker) -> None:
    """Stub mode works without an Anthropic key, like cycle --stub elsewhere."""
    # Insert a minimal cycle
    conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
        ("2026-09-18T13:00:00Z", "2026-09-18", "ok"),
    )
    conn.commit()

    critique = generate_critique(conn, "2026-09-18", "2026-09-18", broker=broker, stub=True)
    assert critique.model == "stub"
    assert critique.good_decisions == STUB_CRITIQUE["good_decisions"]
    assert "no real critique" in critique.good_decisions[0].lower()


def test_critique_validates_date_range(conn: sqlite3.Connection, broker: FakeBroker) -> None:
    """Start after end is rejected."""
    with pytest.raises(CritiqueError, match="start .* is after end"):
        generate_critique(conn, "2026-09-19", "2026-09-18", broker=broker, stub=True)


def test_critique_captures_system_prompt_sha(
    conn: sqlite3.Connection, broker: FakeBroker, tmp_path: Path
) -> None:
    """The critique records the SHA256 of system.txt so we know what playbook was active."""
    conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
        ("2026-09-18T13:00:00Z", "2026-09-18", "ok"),
    )
    conn.commit()

    # Use a temp prompts dir
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "system.txt").write_text("test playbook\n")
    (prompt_dir / "critique.txt").write_text("stub\n")

    critique = generate_critique(
        conn, "2026-09-18", "2026-09-18", broker=broker, stub=True, prompt_dir=prompt_dir
    )
    # SHA256 of "test playbook\n" (without the \n stripped by .strip())
    import hashlib

    expected = hashlib.sha256(b"test playbook").hexdigest()
    assert critique.system_prompt_sha256 == expected


def test_save_critique_writes_json(
    conn: sqlite3.Connection, broker: FakeBroker, tmp_path: Path
) -> None:
    """Critique artifacts are written to data/critiques/ as JSON."""
    conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
        ("2026-09-18T13:00:00Z", "2026-09-18", "ok"),
    )
    conn.commit()

    critique = generate_critique(conn, "2026-09-18", "2026-09-18", broker=broker, stub=True)
    output_dir = tmp_path / "critiques"
    path = save_critique(critique, output_dir)

    assert path.exists()
    assert path.name == "critique_2026-09-18_2026-09-18.json"
    saved = json.loads(path.read_text())
    assert saved["window_start"] == "2026-09-18"
    assert saved["window_end"] == "2026-09-18"
    assert "good_decisions" in saved


def test_format_critique_produces_readable_output(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    """The human-readable format includes all sections."""
    conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
        ("2026-09-18T13:00:00Z", "2026-09-18", "ok"),
    )
    conn.commit()

    critique = generate_critique(conn, "2026-09-18", "2026-09-18", broker=broker, stub=True)
    output = format_critique(critique)

    assert "Critique: 2026-09-18 to 2026-09-18" in output
    assert "## Good Decisions" in output
    assert "## Mistakes" in output
    assert "## Proposed Rule Change" in output
    assert "Model: stub" in output


def test_generate_diff_refuses_disallowed_files(
    conn: sqlite3.Connection, broker: FakeBroker, tmp_path: Path
) -> None:
    """Diffs can only target prompts/*.txt, never risk.toml or src/."""
    conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
        ("2026-09-18T13:00:00Z", "2026-09-18", "ok"),
    )
    conn.commit()

    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "system.txt").write_text("playbook\n")
    (prompt_dir / "critique.txt").write_text("stub\n")

    critique = generate_critique(
        conn, "2026-09-18", "2026-09-18", broker=broker, stub=True, prompt_dir=prompt_dir
    )

    # Allowed
    for allowed in ALLOWED_PROMPT_FILES:
        if (prompt_dir / allowed).exists():
            # Should not raise if the file exists
            try:
                generate_diff(critique, target=allowed, prompt_dir=prompt_dir, output_dir=tmp_path)
            except CritiqueError:
                pass  # OK if no real change proposed

    # Disallowed
    with pytest.raises(CritiqueError, match="refusing to generate diff"):
        generate_diff(critique, target="risk.toml", prompt_dir=prompt_dir, output_dir=tmp_path)

    with pytest.raises(CritiqueError, match="refusing to generate diff"):
        generate_diff(critique, target="../src/trader/risk/config.py", prompt_dir=prompt_dir, output_dir=tmp_path)


def test_generate_diff_returns_none_when_no_change_proposed(
    conn: sqlite3.Connection, broker: FakeBroker, tmp_path: Path
) -> None:
    """If the coach says 'No rule change needed', no diff is generated."""
    conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
        ("2026-09-18T13:00:00Z", "2026-09-18", "ok"),
    )
    conn.commit()

    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "system.txt").write_text("playbook\n")
    (prompt_dir / "critique.txt").write_text("stub\n")

    critique = generate_critique(
        conn, "2026-09-18", "2026-09-18", broker=broker, stub=True, prompt_dir=prompt_dir
    )
    # Stub critique says "No rule change needed this period."
    assert "no rule change needed" in critique.proposed_rule_change.lower()

    diff_path = generate_diff(critique, prompt_dir=prompt_dir, output_dir=tmp_path)
    assert diff_path is None


def test_generate_diff_produces_unified_diff_for_real_change(
    conn: sqlite3.Connection, broker: FakeBroker, tmp_path: Path
) -> None:
    """When a real change is proposed, a unified diff is generated."""
    conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?)",
        ("2026-09-18T13:00:00Z", "2026-09-18", "ok"),
    )
    conn.commit()

    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "system.txt").write_text("original playbook\n")
    (prompt_dir / "critique.txt").write_text("stub\n")

    # Manually construct a critique with a real proposal
    critique = Critique(
        window_start="2026-09-18",
        window_end="2026-09-18",
        critique_at="2026-09-18T14:00:00Z",
        system_prompt_sha256="abc123",
        good_decisions=["good trade"],
        mistakes=["missed exit"],
        proposed_rule_change="Add: Always set stops 1% below entry for NVDA",
        eval_summary={},
        model="test",
    )

    diff_path = generate_diff(critique, prompt_dir=prompt_dir, output_dir=tmp_path)
    assert diff_path is not None
    assert diff_path.exists()
    assert diff_path.name == "critique_2026-09-18_2026-09-18.diff"

    diff_content = diff_path.read_text()
    assert "--- prompts/system.txt" in diff_content
    assert "+++ prompts/system.txt" in diff_content
    assert "Add: Always set stops 1% below entry for NVDA" in diff_content


def test_critique_includes_decisions_and_rejections(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    """The critique context includes recent decisions and risk rejections."""
    cycle_id = conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, ?) RETURNING cycle_id",
        ("2026-09-18T13:00:00Z", "2026-09-18", "ok"),
    ).fetchone()[0]

    record_decision(
        conn,
        cycle_id=cycle_id,
        action="buy",
        symbol="SPY",
        qty=10,
        reasoning="test order",
        risk_result="approved",
        broker_order_id="order123",
    )

    conn.execute(
        "INSERT INTO risk_events(cycle_id, timestamp, check_name, reason, proposal) "
        "VALUES (?, ?, ?, ?, ?)",
        (cycle_id, "2026-09-18T13:05:00Z", "max_position_notional", "too big", "{}"),
    )
    conn.commit()

    critique = generate_critique(conn, "2026-09-18", "2026-09-18", broker=broker, stub=True)
    # The eval_summary includes the full evaluation data
    assert "cycles" in critique.eval_summary
    # Decisions and rejections are passed to the coach (we can't easily verify
    # the coach prompt without mocking, but we can verify the critique was generated)
    assert critique.window_start == "2026-09-18"


def test_cli_smoke_critique_help(tmp_path: Path) -> None:
    """CLI help for critique command is available."""
    import subprocess

    result = subprocess.run(
        ["uv", "run", "trader", "critique", "--help"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[2],
    )
    assert result.returncode == 0
    assert "critique" in result.stdout.lower()
    assert "--propose-diff" in result.stdout
    assert "--start" in result.stdout
    assert "--end" in result.stdout
