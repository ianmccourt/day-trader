"""Robinhood Agentic adapter: capability mapping, fail-closed stops, clock fallback."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.broker import BrokerError
from trader.brokers import broker_endpoint, make_broker
from trader.config import MissingCredential, Settings, load_settings
from trader.constants import (
    ALPACA_PAPER_BASE_URL,
    BROKER_PAPER,
    BROKER_ROBINHOOD_AGENTIC,
    MARKET_TZ,
)
from trader.robinhood_mcp import (
    ROBINHOOD_MCP_URL,
    DiscoveredTool,
    FileTokenStorage,
    RobinhoodMcpBroker,
    bind_order_args,
    map_capabilities,
    weekday_rth_clock,
)


def _tool(
    name: str,
    *,
    description: str = "",
    properties: dict | None = None,
    required: tuple[str, ...] = (),
) -> DiscoveredTool:
    return DiscoveredTool(
        name=name,
        description=description,
        properties=properties or {},
        required=required,
    )


def _write_tool(**extra: dict) -> DiscoveredTool:
    props = {
        "symbol": {"type": "string"},
        "quantity": {"type": "number"},
        "side": {"type": "string"},
    }
    props.update(extra)
    return _tool("place_order", properties=props)


class FakeMcpSession:
    def __init__(self, tools: list, handlers: dict) -> None:
        self.tools = tools
        self.handlers = handlers
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name: str, arguments: dict | None = None):
        self.calls.append((name, arguments or {}))
        payload = self.handlers[name](arguments or {})
        return SimpleNamespace(isError=False, content=[], structuredContent=payload)


def _sdk_tool(name: str, properties: dict, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": properties},
    )


def test_map_capabilities_picks_place_order_not_get_order() -> None:
    tools = [
        _tool("get_account", properties={"unused": {}}),
        _tool("get_positions", properties={"symbol": {}, "qty": {}}),
        _tool("get_quote", properties={"symbol": {}}),
        _tool("get_order", properties={"order_id": {}}),
        _tool(
            "place_order",
            properties={"symbol": {}, "quantity": {}, "side": {}, "stop_price": {}},
        ),
        _tool("get_market_hours", properties={"is_open": {}}),
        _tool("get_bars", properties={"symbol": {}, "timeframe": {}, "limit": {}}),
    ]
    caps = map_capabilities(tools)
    assert caps["write"] is not None and caps["write"].name == "place_order"
    assert caps["order_status"] is not None and caps["order_status"].name == "get_order"
    assert caps["account"] is not None and caps["account"].name == "get_account"
    assert caps["quote"] is not None and caps["quote"].name == "get_quote"
    assert caps["bars"] is not None and caps["bars"].name == "get_bars"
    assert caps["clock"] is not None and caps["clock"].name == "get_market_hours"


def test_missing_write_tool_is_none() -> None:
    caps = map_capabilities([_tool("get_account"), _tool("get_positions")])
    assert caps["write"] is None


def test_bind_order_args_refuses_a_stop_the_schema_cannot_attach() -> None:
    tool = _write_tool()
    with pytest.raises(BrokerError, match="stop-loss"):
        bind_order_args(
            tool, symbol="AAPL", qty=1, side="buy", stop_price=99.0, take_profit_price=None
        )


def test_bind_order_args_refuses_a_take_profit_the_schema_cannot_attach() -> None:
    tool = _write_tool(stop_price={"type": "number"})
    with pytest.raises(BrokerError, match="take-profit"):
        bind_order_args(
            tool,
            symbol="AAPL",
            qty=1,
            side="buy",
            stop_price=99.0,
            take_profit_price=110.0,
        )


def test_bind_order_args_passes_protection_when_the_schema_has_it() -> None:
    tool = _write_tool(stop_price={"type": "number"}, take_profit_price={"type": "number"})
    args = bind_order_args(
        tool, symbol="AAPL", qty=2, side="buy", stop_price=99.0, take_profit_price=110.0
    )
    assert args == {
        "symbol": "AAPL",
        "quantity": 2,
        "side": "buy",
        "stop_price": 99.0,
        "take_profit_price": 110.0,
    }


def test_weekday_rth_clock_open_and_closed() -> None:
    wed = datetime(2026, 9, 9, 10, 0, tzinfo=MARKET_TZ)  # Wednesday
    sat = datetime(2026, 9, 12, 10, 0, tzinfo=MARKET_TZ)
    before = datetime(2026, 9, 9, 9, 29, tzinfo=MARKET_TZ)
    at_close = datetime(2026, 9, 9, 16, 0, tzinfo=MARKET_TZ)
    assert weekday_rth_clock(wed).is_open is True
    assert weekday_rth_clock(sat).is_open is False
    assert weekday_rth_clock(before).is_open is False
    assert weekday_rth_clock(at_close).is_open is False


def test_submit_order_uses_discovered_write_tool(tmp_path: Path) -> None:
    session = FakeMcpSession(
        tools=[
            _sdk_tool("get_account", {}),
            _sdk_tool(
                "place_order",
                {
                    "symbol": {"type": "string"},
                    "quantity": {"type": "number"},
                    "side": {"type": "string"},
                    "stop_price": {"type": "number"},
                },
            ),
        ],
        handlers={
            "place_order": lambda args: {
                "id": "rh-1",
                "status": "submitted",
                "filled_qty": 0,
            }
        },
    )

    @asynccontextmanager
    async def factory():
        yield session

    broker = RobinhoodMcpBroker(tmp_path / "tok.json", session_factory=factory)
    receipt = broker.submit_order(symbol="AAPL", qty=1, side="buy", stop_price=99.0)
    assert receipt.order_id == "rh-1"
    assert session.calls == [
        ("place_order", {"symbol": "AAPL", "quantity": 1, "side": "buy", "stop_price": 99.0})
    ]


def test_submit_order_fails_closed_without_a_write_tool(tmp_path: Path) -> None:
    session = FakeMcpSession(tools=[_sdk_tool("get_account", {})], handlers={})

    @asynccontextmanager
    async def factory():
        yield session

    broker = RobinhoodMcpBroker(tmp_path / "tok.json", session_factory=factory)
    with pytest.raises(BrokerError, match="no write tool"):
        broker.submit_order(symbol="AAPL", qty=1, side="buy")


def test_clock_falls_back_when_no_clock_tool(tmp_path: Path) -> None:
    session = FakeMcpSession(tools=[_sdk_tool("get_account", {})], handlers={})

    @asynccontextmanager
    async def factory():
        yield session

    broker = RobinhoodMcpBroker(tmp_path / "tok.json", session_factory=factory)
    clock = broker.get_clock()
    assert clock.next_open.tzinfo is not None
    assert clock.next_close.tzinfo is not None


def test_get_account_and_price_parse_nested_payloads(tmp_path: Path) -> None:
    session = FakeMcpSession(
        tools=[
            _sdk_tool("get_account", {}),
            _sdk_tool("get_stock_quote", {"symbol": {"type": "string"}}),
            _sdk_tool("list_positions", {}),
        ],
        handlers={
            "get_account": lambda _a: {
                "account": {"equity": "100000", "cash": "20000", "buyingPower": "40000"}
            },
            "get_stock_quote": lambda _a: {"quote": {"last_price": 123.45}},
            "list_positions": lambda _a: {
                "positions": [{"ticker": "AAPL", "quantity": "3", "avg_entry_price": "100"}]
            },
        },
    )

    @asynccontextmanager
    async def factory():
        yield session

    broker = RobinhoodMcpBroker(tmp_path / "tok.json", session_factory=factory)
    acct = broker.get_account()
    assert acct["equity"] == 100000.0
    assert acct["buying_power"] == 40000.0
    assert broker.get_latest_price("AAPL") == 123.45
    positions = broker.get_positions()
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["qty"] == 3.0


def test_file_token_storage_round_trips(tmp_path: Path) -> None:
    from mcp.shared.auth import OAuthToken

    path = tmp_path / "tokens.json"
    store = FileTokenStorage(path)
    token = OAuthToken(access_token="a", token_type="Bearer", refresh_token="r")

    async def _go() -> None:
        assert await store.get_tokens() is None
        await store.set_tokens(token)
        loaded = await store.get_tokens()
        assert loaded is not None
        assert loaded.access_token == "a"
        assert loaded.refresh_token == "r"

    import asyncio

    asyncio.run(_go())
    assert path.stat().st_mode & 0o777 == 0o600
    assert "a" in path.read_text()


def test_unknown_broker_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADER_BROKER", "live")
    with pytest.raises(ValueError, match="TRADER_BROKER"):
        load_settings(require_broker=False)


def test_paper_is_the_default_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADER_BROKER", raising=False)
    settings = load_settings(require_broker=False)
    assert settings.broker == BROKER_PAPER
    assert settings.is_paper is True


def test_make_broker_selects_the_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    paper = Settings(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        db_path=tmp_path / "t.sqlite3",
        cycle_minutes=5,
        log_level="INFO",
        broker=BROKER_PAPER,
    )
    from trader.broker import AlpacaBroker

    assert isinstance(make_broker(paper), AlpacaBroker)
    assert broker_endpoint(paper) == ALPACA_PAPER_BASE_URL

    token = tmp_path / "rh.json"
    token.write_text("{}", encoding="utf-8")
    rh = Settings(
        alpaca_api_key="",
        alpaca_secret_key="",
        db_path=tmp_path / "t.sqlite3",
        cycle_minutes=5,
        log_level="INFO",
        broker=BROKER_ROBINHOOD_AGENTIC,
        robinhood_token_path=token,
    )
    assert isinstance(make_broker(rh), RobinhoodMcpBroker)
    assert broker_endpoint(rh) == ROBINHOOD_MCP_URL


def test_make_broker_paper_without_keys_is_a_credential_error(tmp_path: Path) -> None:
    settings = Settings(
        alpaca_api_key="",
        alpaca_secret_key="",
        db_path=tmp_path / "t.sqlite3",
        cycle_minutes=5,
        log_level="INFO",
    )
    with pytest.raises(MissingCredential, match="Alpaca"):
        make_broker(settings)
