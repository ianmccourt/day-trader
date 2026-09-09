"""SPEC.md constraint #3: the risk layer cannot be bypassed.

Enforced structurally rather than by review. If anyone adds a second caller of
Broker.submit_order, or an argument that skips evaluation, the build fails.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from trader import execution
from trader.risk import engine

SRC = Path(__file__).resolve().parents[1] / "src" / "trader"


def _calls_named(path: Path, name: str) -> list[str]:
    """Every function in `path` that calls something named `name`."""
    tree = ast.parse(path.read_text())
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                found.append(f"{path.name}::{node.name}")
                break
    return found


def test_submit_order_has_exactly_one_caller() -> None:
    callers = [
        c
        for path in SRC.rglob("*.py")
        for c in _calls_named(path, "submit_order")
        # broker.py's own method body calls the vendored client's submit_order.
        if not c.startswith("broker.py")
    ]
    assert callers == ["execution.py::place_order"], callers


def test_place_order_evaluates_before_submitting() -> None:
    """The call to evaluate() must dominate the call to submit_order()."""
    source = inspect.getsource(execution.place_order)
    assert source.index("evaluate(") < source.index("submit_order("), source


def test_place_order_has_no_skip_argument() -> None:
    params = set(inspect.signature(execution.place_order).parameters)
    assert params == {"conn", "broker", "ctx", "proposal", "config"}
    forbidden = {"skip", "force", "bypass", "dry_run", "override", "no_risk"}
    assert not any(any(f in p for f in forbidden) for p in params)


def test_evaluate_runs_every_registered_check() -> None:
    """No argument may reduce the check set below CHECKS by default."""
    from trader.risk.checks import CHECKS

    assert inspect.signature(engine.evaluate).parameters["checks"].default is CHECKS


def test_verdict_cannot_be_approved_with_failures_ignored() -> None:
    """A rejection must never be summarised as an approval."""
    from trader.risk.engine import Failure, Verdict

    v = Verdict(approved=False, failures=(Failure("kill_switch", "engaged"),))
    assert v.summary == "rejected:kill_switch"
    assert "REJECTED" in v.as_model_message()


def test_only_broker_imports_the_alpaca_sdk() -> None:
    """Order submission must not be reachable through a second client instance."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "broker.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            module = None
            if isinstance(node, ast.Import):
                module = node.names[0].name
            elif isinstance(node, ast.ImportFrom):
                module = node.module
            if module and module.split(".")[0] == "alpaca":
                offenders.append(f"{path.relative_to(SRC)}: {module}")
    assert not offenders, f"alpaca SDK imported outside broker.py: {offenders}"
