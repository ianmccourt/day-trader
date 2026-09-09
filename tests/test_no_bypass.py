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


# --- Phase 3: exactly one write tool ---------------------------------------


def test_only_one_tool_calls_place_order() -> None:
    """SPEC.md constraint #2: the model has exactly one tool that writes."""
    callers = _calls_named(SRC / "tools.py", "place_order")
    assert callers == ["tools.py::_place_order"], callers


def test_no_read_tool_contains_a_write_statement() -> None:
    """Read tools must not reach the DB with INSERT/UPDATE/DELETE."""
    import inspect

    from trader import tools

    offenders = []
    for name, impl in tools.TOOL_IMPLS.items():
        if name == tools.WRITE_TOOL:
            continue
        source = inspect.getsource(impl).upper()
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "REPLACE "):
            if verb in source:
                offenders.append(f"{name}: {verb.strip()}")
    assert not offenders, offenders


def test_the_tool_surface_is_exactly_what_is_declared() -> None:
    """No tool may exist without a schema, or a schema without an implementation."""
    from trader import tools

    assert {t["name"] for t in tools.TOOL_SCHEMAS} == set(tools.TOOL_IMPLS)
    write_tools = [t for t in tools.TOOL_SCHEMAS if t["name"] == tools.WRITE_TOOL]
    assert len(write_tools) == 1


def test_no_tool_can_run_a_shell_or_arbitrary_http() -> None:
    """SPEC.md constraint #2: no shell access, no arbitrary HTTP."""
    import ast

    forbidden = {"subprocess", "os.system", "requests", "httpx", "urllib", "socket", "pty"}
    tree = ast.parse((SRC / "tools.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not (imported & forbidden), imported & forbidden
    assert "eval(" not in (SRC / "tools.py").read_text()


def test_the_agent_cannot_change_its_own_risk_config() -> None:
    """RiskConfig is frozen, and nothing in the tool layer rebinds it."""
    import re

    source = (SRC / "tools.py").read_text()
    assert not re.search(r"\btc\.config\s*=", source)
    assert not re.search(r"\bconfig\.\w+\s*=", source)


def test_the_test_double_implements_the_whole_broker_protocol() -> None:
    """A fake missing a method makes tests pass that would fail in production."""
    from tests.fakes import FakeBroker
    from trader.broker import AlpacaBroker, Broker

    required = {
        name
        for name in dir(Broker)
        if not name.startswith("_") and callable(getattr(Broker, name, None))
    }
    for implementation in (FakeBroker, AlpacaBroker):
        missing = {m for m in required if not hasattr(implementation, m)}
        assert not missing, f"{implementation.__name__} is missing {sorted(missing)}"
