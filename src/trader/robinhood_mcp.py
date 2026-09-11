"""Robinhood Agentic MCP broker adapter.

Orders still go through `execution.place_order` → `evaluate()` first. This
module is the only place that talks to `https://agent.robinhood.com/mcp/trading`.
Tool names are discovered at runtime from `tools/list` — a missing write,
quote, or protective-exit parameter fails closed rather than dropping data.

The market clock falls back to weekday regular trading hours when the server
exposes no clock tool. That fallback does not know holidays or early closes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from trader.broker import MAX_BARS, BrokerError, Clock, OrderFill, OrderReceipt
from trader.constants import MARKET_TZ, RTH_CLOSE, RTH_OPEN
from trader.db import iso, utcnow

#: Official Robinhood Agentic MCP endpoint. Not configurable — same idea as
#: `ALPACA_PAPER_BASE_URL`. Discovery/registration happen against this URL.
ROBINHOOD_MCP_URL = "https://agent.robinhood.com/mcp/trading"

#: Fixed loopback callback so a stored client registration stays valid.
OAUTH_CALLBACK_PORT = 18765
OAUTH_CALLBACK_PATH = "/callback"

log = logging.getLogger("trader.robinhood")

_CAPABILITIES = (
    "account",
    "positions",
    "quote",
    "bars",
    "clock",
    "order_status",
    "write",
)

_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "account": ("account", "balance", "equity", "buying_power"),
    "positions": ("position", "holding", "positions"),
    "quote": ("quote", "price", "last_trade", "last_price", "ticker"),
    "bars": ("bar", "ohlc", "candle", "history", "historical"),
    "clock": ("clock", "hours", "market_status", "is_open", "session"),
    "order_status": ("get_order", "order_status", "order_detail", "fetch_order"),
    "write": ("place_order", "submit_order", "create_order", "place"),
}

_PROP_HINTS: dict[str, tuple[str, ...]] = {
    "account": ("equity", "cash", "buying_power", "buyingpower"),
    "positions": ("symbol", "qty", "quantity"),
    "quote": ("symbol", "ticker"),
    "bars": ("timeframe", "interval", "period", "granularity"),
    "clock": ("is_open",),
    "order_status": ("order_id", "id"),
    "write": ("symbol", "side", "qty", "quantity"),
}

_SYMBOL_KEYS = ("symbol", "ticker", "instrument")
_QTY_KEYS = ("qty", "quantity", "shares")
_SIDE_KEYS = ("side", "action")
_STOP_KEYS = ("stop_price", "stop", "stop_loss", "stoploss", "stop_loss_price")
_TP_KEYS = ("take_profit_price", "take_profit", "takeprofit", "profit_price", "target_price")
_TF_KEYS = ("timeframe", "interval", "granularity", "resolution", "period")
_LIMIT_KEYS = ("limit", "count", "n", "bars")
_ORDER_ID_KEYS = ("order_id", "id", "orderid")

_TIMEFRAME_ALIASES: dict[str, tuple[str, ...]] = {
    "1Min": ("1Min", "1min", "1m", "minute", "1"),
    "5Min": ("5Min", "5min", "5m", "5"),
    "15Min": ("15Min", "15min", "15m", "15"),
    "1Hour": ("1Hour", "1hour", "1h", "hour", "60"),
    "1Day": ("1Day", "1day", "1d", "day", "daily"),
}


def _norm(key: str) -> str:
    return key.lower().replace("-", "").replace("_", "")


def _ci_get(data: Mapping[str, Any], *names: str) -> Any:
    index = {_norm(str(k)): v for k, v in data.items()}
    for name in names:
        if _norm(name) in index:
            return index[_norm(name)]
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _unwrap(data: Any, *containers: str) -> Any:
    if not isinstance(data, dict):
        return data
    for key in containers:
        inner = _ci_get(data, key)
        if isinstance(inner, (dict, list)):
            return inner
    return data


def _as_list(data: Any, *containers: str) -> list[Any]:
    data = _unwrap(data, *containers)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("positions", "holdings", "data", "items", "results", "bars"):
            inner = _ci_get(data, key)
            if isinstance(inner, list):
                return inner
    return []


@dataclass(frozen=True, slots=True)
class DiscoveredTool:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]

    @classmethod
    def from_sdk(cls, tool: Any) -> DiscoveredTool:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
        if not isinstance(schema, dict):
            schema = {}
        props = schema.get("properties") or {}
        required = tuple(schema.get("required") or ())
        return cls(
            name=str(tool.name),
            description=str(getattr(tool, "description", None) or ""),
            properties=dict(props) if isinstance(props, dict) else {},
            required=required,
        )

    def prop(self, *names: str) -> str | None:
        index = {_norm(k): k for k in self.properties}
        for name in names:
            if _norm(name) in index:
                return index[_norm(name)]
        return None


def _score(tool: DiscoveredTool, capability: str) -> int:
    name = tool.name.lower().replace("-", "_")
    desc = tool.description.lower()
    props = {_norm(p) for p in tool.properties}
    score = 0
    for hint in _NAME_HINTS[capability]:
        key = hint.lower()
        if name == key or name.endswith("_" + key) or name.startswith(key + "_"):
            score += 5
        elif key in name:
            score += 3
        if key in desc:
            score += 1
    for hint in _PROP_HINTS[capability]:
        if _norm(hint) in props or any(_norm(hint) in p for p in props):
            score += 2
    if capability == "write" and "place" in name:
        score += 3
    if capability == "order_status" and any(w in name for w in ("place", "create", "submit")):
        score -= 6
    return score


def map_capabilities(tools: list[DiscoveredTool]) -> dict[str, DiscoveredTool | None]:
    """Pick at most one tool per capability. None means fail closed later."""
    mapped: dict[str, DiscoveredTool | None] = {}
    used: set[str] = set()
    # Write first so order_status cannot steal the placement tool.
    order = ("write", "order_status", "account", "positions", "quote", "bars", "clock")
    for cap in order:
        best: DiscoveredTool | None = None
        best_score = 0
        for tool in tools:
            if tool.name in used:
                continue
            score = _score(tool, cap)
            if score > best_score:
                best, best_score = tool, score
        if best is not None and best_score >= 3:
            mapped[cap] = best
            used.add(best.name)
        else:
            mapped[cap] = None
    return {cap: mapped.get(cap) for cap in _CAPABILITIES}


def _assign(args: dict[str, Any], tool: DiscoveredTool, names: tuple[str, ...], value: Any) -> bool:
    key = tool.prop(*names)
    if key is None:
        return False
    args[key] = value
    return True


def _qty_for_schema(tool: DiscoveredTool, qty: float) -> int | float | str:
    key = tool.prop(*_QTY_KEYS)
    schema = tool.properties.get(key or "", {})
    typ = schema.get("type") if isinstance(schema, dict) else None
    if typ == "integer":
        if abs(qty - round(qty)) > 1e-9:
            raise BrokerError(f"qty {qty} is not an integer; {tool.name} requires an integer")
        return round(qty)
    if typ == "string":
        return str(int(qty) if abs(qty - round(qty)) < 1e-9 else qty)
    return qty


def bind_order_args(
    tool: DiscoveredTool,
    *,
    symbol: str,
    qty: float,
    side: str,
    stop_price: float | None,
    take_profit_price: float | None,
) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if not _assign(args, tool, _SYMBOL_KEYS, symbol):
        raise BrokerError(f"Robinhood tool {tool.name!r} has no symbol parameter")
    if not _assign(args, tool, _QTY_KEYS, _qty_for_schema(tool, qty)):
        raise BrokerError(f"Robinhood tool {tool.name!r} has no quantity parameter")
    if not _assign(args, tool, _SIDE_KEYS, side):
        raise BrokerError(f"Robinhood tool {tool.name!r} has no side parameter")
    if stop_price is not None and not _assign(args, tool, _STOP_KEYS, stop_price):
        raise BrokerError(
            "Robinhood MCP write tool has no stop-loss parameter; "
            "refusing to submit an unprotected order"
        )
    if take_profit_price is not None:
        tp_keys = _TP_KEYS
        desc = tool.description.lower()
        if "take profit" in desc or "take_profit" in desc or "profit target" in desc:
            tp_keys = (*_TP_KEYS, "limit_price", "limitprice")
        if not _assign(args, tool, tp_keys, take_profit_price):
            raise BrokerError(
                "Robinhood MCP write tool has no take-profit parameter; "
                "refusing to submit without the requested protection"
            )
    return args


def weekday_rth_clock(now: datetime | None = None) -> Clock:
    """Weekday 09:30-16:00 America/New_York. Holidays and early closes are wrong."""
    now = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
    open_t = now.replace(hour=RTH_OPEN[0], minute=RTH_OPEN[1], second=0, microsecond=0)
    close_t = now.replace(hour=RTH_CLOSE[0], minute=RTH_CLOSE[1], second=0, microsecond=0)
    weekday = now.weekday() < 5
    is_open = weekday and open_t <= now < close_t

    def _next_open_after(day: datetime, *, skip_today: bool) -> datetime:
        candidate = day.replace(hour=RTH_OPEN[0], minute=RTH_OPEN[1], second=0, microsecond=0)
        if skip_today or candidate <= day or candidate.weekday() >= 5:
            candidate = candidate + timedelta(days=1)
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
            candidate = candidate.replace(
                hour=RTH_OPEN[0], minute=RTH_OPEN[1], second=0, microsecond=0
            )
        return candidate

    if is_open:
        next_open = _next_open_after(now, skip_today=True)
        next_close = close_t
    elif weekday and now < open_t:
        next_open = open_t
        next_close = close_t
    else:
        next_open = _next_open_after(now, skip_today=True)
        next_close = next_open.replace(hour=RTH_CLOSE[0], minute=RTH_CLOSE[1])
    return Clock(timestamp=now, is_open=is_open, next_open=next_open, next_close=next_close)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    path.chmod(0o600)


class FileTokenStorage(TokenStorage):
    """JSON token + client registration on disk. Never log the contents."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BrokerError(
                f"Robinhood token file {self.path} is not valid JSON. "
                "Delete it and run `uv run trader rh-login`."
            ) from exc
        if not isinstance(raw, dict):
            raise BrokerError(f"Robinhood token file {self.path} must be a JSON object")
        return raw

    def _save(self, payload: dict[str, Any]) -> None:
        _write_json(self.path, payload)

    async def get_tokens(self) -> OAuthToken | None:
        blob = self._load().get("tokens")
        if not blob:
            return None
        return OAuthToken.model_validate(blob)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        payload = self._load()
        payload["tokens"] = tokens.model_dump(mode="json")
        self._save(payload)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        blob = self._load().get("client_info")
        if not blob:
            return None
        return OAuthClientInformationFull.model_validate(blob)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        payload = self._load()
        payload["client_info"] = client_info.model_dump(mode="json")
        self._save(payload)


class _CallbackServer:
    """One-shot loopback listener for the OAuth redirect."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._event = threading.Event()
        self._code: str | None = None
        self._state: str | None = None
        self._error: str | None = None
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != OAUTH_CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                params = parse_qs(parsed.query)
                if params.get("error"):
                    server._error = params["error"][0]
                else:
                    codes = params.get("code") or []
                    if not codes:
                        server._error = "callback missing code"
                    else:
                        server._code = codes[0]
                        states = params.get("state") or [None]
                        server._state = states[0]
                server._event.set()
                body = (
                    b"<html><body>Robinhood login complete. You can close this tab.</body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    async def wait(self, timeout: float = 300.0) -> tuple[str, str | None]:
        deadline = asyncio.get_running_loop().time() + timeout
        while not self._event.is_set():
            if asyncio.get_running_loop().time() > deadline:
                raise BrokerError(
                    "Robinhood OAuth timed out waiting for the browser callback. "
                    "Re-run `uv run trader rh-login`."
                )
            await asyncio.sleep(0.05)
        if self._error:
            raise BrokerError(f"Robinhood OAuth failed: {self._error}")
        assert self._code is not None
        return self._code, self._state

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _oauth_provider(
    storage: FileTokenStorage,
    redirect_handler: Callable[[str], Awaitable[None]],
    callback_handler: Callable[[], Awaitable[tuple[str, str | None]]],
) -> OAuthClientProvider:
    return OAuthClientProvider(
        server_url=ROBINHOOD_MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="day-trader harness",
            redirect_uris=[AnyUrl(f"http://127.0.0.1:{OAUTH_CALLBACK_PORT}{OAUTH_CALLBACK_PATH}")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


async def _refuse_browser(_url: str) -> None:
    raise BrokerError(
        "Robinhood OAuth needs a browser. Run `uv run trader rh-login` on this machine."
    )


async def _refuse_callback() -> tuple[str, str | None]:
    raise BrokerError(
        "Robinhood OAuth needs a browser. Run `uv run trader rh-login` on this machine."
    )


def _parse_tool_result(result: Any) -> Any:
    if getattr(result, "isError", False):
        raise BrokerError(f"Robinhood MCP tool error: {_result_text(result)}")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    return _result_text(result)


def _result_text(result: Any) -> Any:
    texts: list[str] = []
    for part in getattr(result, "content", None) or []:
        text = getattr(part, "text", None)
        if text:
            texts.append(str(text))
    blob = "\n".join(texts).strip()
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return {"text": blob}


def _parse_account(raw: Any) -> dict[str, Any]:
    data = _unwrap(raw, "account", "data", "result")
    if not isinstance(data, dict):
        raise BrokerError(f"Robinhood account payload is not an object: {type(raw).__name__}")
    equity = _as_float(
        _ci_get(data, "equity", "portfolio_value", "account_value", "net_liquidation")
    )
    if equity is None:
        raise BrokerError("Robinhood account payload has no equity field")
    return {
        "equity": equity,
        "last_equity": _as_float(
            _ci_get(data, "last_equity", "previous_equity", "equity_previous_close")
        ),
        "cash": _as_float(_ci_get(data, "cash", "cash_available", "available_cash")) or 0.0,
        "buying_power": _as_float(_ci_get(data, "buying_power", "buyingpower", "bp")),
        "long_market_value": _as_float(
            _ci_get(data, "long_market_value", "long_value", "market_value")
        ),
        "short_market_value": _as_float(_ci_get(data, "short_market_value", "short_value")) or 0.0,
        "captured_at": iso(utcnow()),
    }


def _parse_positions(raw: Any) -> list[dict[str, Any]]:
    captured_at = iso(utcnow())
    out: list[dict[str, Any]] = []
    for row in _as_list(raw, "positions", "holdings", "data"):
        if not isinstance(row, dict):
            continue
        symbol = _ci_get(row, "symbol", "ticker")
        if not symbol:
            continue
        qty = _as_float(_ci_get(row, "qty", "quantity", "shares")) or 0.0
        out.append(
            {
                "symbol": str(symbol).upper(),
                "qty": qty,
                "avg_price": _as_float(
                    _ci_get(row, "avg_price", "average_price", "avg_entry_price")
                )
                or 0.0,
                "current_price": _as_float(_ci_get(row, "current_price", "last_price", "price")),
                "market_value": _as_float(_ci_get(row, "market_value", "value")),
                "unrealized_pl": _as_float(_ci_get(row, "unrealized_pl", "unrealized_pnl", "pnl")),
                "captured_at": captured_at,
            }
        )
    return out


def _parse_price(raw: Any, symbol: str) -> float:
    data = raw
    if isinstance(raw, dict):
        nested = _ci_get(raw, symbol, symbol.upper(), symbol.lower())
        if isinstance(nested, dict):
            data = nested
        else:
            data = _unwrap(raw, "quote", "trade", "last", "data", "result")
    if not isinstance(data, dict):
        price = _as_float(data)
    else:
        price = _as_float(
            _ci_get(data, "price", "last_price", "last", "close", "mark", "last_trade_price")
        )
    if price is None or price <= 0:
        raise BrokerError(f"Robinhood quote for {symbol} has no positive price")
    return price


def _parse_clock(raw: Any) -> Clock:
    data = _unwrap(raw, "clock", "hours", "data", "result")
    if not isinstance(data, dict):
        raise BrokerError("Robinhood clock payload is not an object")
    is_open_raw = _ci_get(data, "is_open", "isOpen", "market_open", "open")
    if is_open_raw is None:
        raise BrokerError("Robinhood clock payload has no is_open field")
    timestamp = _as_datetime(_ci_get(data, "timestamp", "now", "as_of")) or utcnow()
    next_open = _as_datetime(_ci_get(data, "next_open", "nextOpen"))
    next_close = _as_datetime(_ci_get(data, "next_close", "nextClose"))
    if next_open is None or next_close is None:
        fallback = weekday_rth_clock(timestamp)
        next_open = next_open or fallback.next_open
        next_close = next_close or fallback.next_close
    return Clock(
        timestamp=timestamp,
        is_open=bool(is_open_raw),
        next_open=next_open,
        next_close=next_close,
    )


def _parse_bars(raw: Any) -> list[dict[str, Any]]:
    bars = []
    for row in _as_list(raw, "bars", "candles", "data"):
        if not isinstance(row, dict):
            continue
        ts = _ci_get(row, "t", "timestamp", "time", "datetime")
        o = _as_float(_ci_get(row, "o", "open"))
        h = _as_float(_ci_get(row, "h", "high"))
        low = _as_float(_ci_get(row, "l", "low"))
        c = _as_float(_ci_get(row, "c", "close"))
        v = _as_float(_ci_get(row, "v", "volume")) or 0.0
        if ts is None or None in (o, h, low, c):
            continue
        bars.append({"t": str(ts), "o": o, "h": h, "l": low, "c": c, "v": v})
    return bars


def _parse_receipt(raw: Any) -> OrderReceipt:
    data = _unwrap(raw, "order", "data", "result")
    if not isinstance(data, dict):
        raise BrokerError("Robinhood order payload is not an object")
    order_id = _ci_get(data, "order_id", "id", "orderId")
    if not order_id:
        raise BrokerError("Robinhood order payload has no id")
    return OrderReceipt(
        order_id=str(order_id),
        status=str(_ci_get(data, "status") or "submitted"),
        submitted_at=_as_datetime(_ci_get(data, "submitted_at", "created_at", "timestamp")),
        filled_qty=_as_float(_ci_get(data, "filled_qty", "filled_quantity")),
    )


def _parse_fill(raw: Any, order_id: str) -> OrderFill:
    data = _unwrap(raw, "order", "data", "result")
    if not isinstance(data, dict):
        raise BrokerError("Robinhood order-status payload is not an object")
    return OrderFill(
        order_id=str(_ci_get(data, "order_id", "id", "orderId") or order_id),
        status=str(_ci_get(data, "status") or "unknown"),
        filled_qty=_as_float(_ci_get(data, "filled_qty", "filled_quantity")) or 0.0,
        filled_avg_price=_as_float(_ci_get(data, "filled_avg_price", "average_price", "avg_price")),
        filled_at=_as_datetime(_ci_get(data, "filled_at", "updated_at")),
    )


def _timeframe_value(tool: DiscoveredTool, timeframe: str) -> str:
    key = tool.prop(*_TF_KEYS)
    schema = tool.properties.get(key or "", {})
    enum = schema.get("enum") if isinstance(schema, dict) else None
    aliases = _TIMEFRAME_ALIASES.get(timeframe, (timeframe,))
    if isinstance(enum, list) and enum:
        lowered = {str(v).lower(): str(v) for v in enum}
        for alias in aliases:
            if alias.lower() in lowered:
                return lowered[alias.lower()]
        raise BrokerError(
            f"Robinhood bars tool does not accept timeframe {timeframe!r}; enum={enum!r}"
        )
    return aliases[0]


class RobinhoodMcpBroker:
    """Broker protocol over Robinhood Agentic MCP. Lazy: no network in `__init__`."""

    def __init__(
        self,
        token_path: Path,
        *,
        interactive: bool = False,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._token_path = token_path
        self._interactive = interactive
        self._session_factory = session_factory
        self._caps: dict[str, DiscoveredTool | None] | None = None
        self._logged_caps = False

    @property
    def base_url(self) -> str:
        return ROBINHOOD_MCP_URL

    def _run(self, coro: Awaitable[Any]) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        raise BrokerError("Robinhood MCP calls cannot nest inside a running event loop")

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        if self._session_factory is not None:
            async with self._session_factory() as session:
                yield session
            return
        storage = FileTokenStorage(self._token_path)
        if self._interactive:
            callback = _CallbackServer(OAUTH_CALLBACK_PORT)
            try:

                async def redirect(url: str) -> None:
                    print(f"Opening browser for Robinhood OAuth:\n{url}", flush=True)
                    webbrowser.open(url)

                oauth = _oauth_provider(storage, redirect, callback.wait)
                async with streamablehttp_client(
                    ROBINHOOD_MCP_URL, auth=oauth, timeout=60.0, sse_read_timeout=300.0
                ) as streams:
                    read, write, _sid = streams
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            finally:
                callback.close()
            return
        oauth = _oauth_provider(storage, _refuse_browser, _refuse_callback)
        try:
            async with streamablehttp_client(
                ROBINHOOD_MCP_URL, auth=oauth, timeout=60.0, sse_read_timeout=300.0
            ) as streams:
                read, write, _sid = streams
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"Robinhood MCP connection failed: {exc}") from exc

    async def _caps_for(self, session: Any) -> dict[str, DiscoveredTool | None]:
        if self._caps is None:
            listed = await session.list_tools()
            tools = [DiscoveredTool.from_sdk(t) for t in listed.tools]
            self._caps = map_capabilities(tools)
            if not self._logged_caps:
                log.info(
                    "robinhood_capabilities",
                    extra={"caps": {k: (v.name if v else None) for k, v in self._caps.items()}},
                )
                self._logged_caps = True
        return self._caps

    def _require(self, caps: dict[str, DiscoveredTool | None], name: str) -> DiscoveredTool:
        tool = caps.get(name)
        if tool is None:
            raise BrokerError(f"Robinhood MCP has no {name} tool (tools/list). Failing closed.")
        return tool

    async def _call(self, session: Any, tool: DiscoveredTool, arguments: dict[str, Any]) -> Any:
        result = await session.call_tool(tool.name, arguments)
        return _parse_tool_result(result)

    def get_clock(self) -> Clock:
        return self._run(self._get_clock())

    async def _get_clock(self) -> Clock:
        async with self._session() as session:
            caps = await self._caps_for(session)
            tool = caps.get("clock")
            if tool is None:
                return weekday_rth_clock()
            raw = await self._call(session, tool, {})
            return _parse_clock(raw)

    def get_account(self) -> dict[str, Any]:
        return self._run(self._get_account())

    async def _get_account(self) -> dict[str, Any]:
        async with self._session() as session:
            caps = await self._caps_for(session)
            raw = await self._call(session, self._require(caps, "account"), {})
            return _parse_account(raw)

    def get_positions(self) -> list[dict[str, Any]]:
        return self._run(self._get_positions())

    async def _get_positions(self) -> list[dict[str, Any]]:
        async with self._session() as session:
            caps = await self._caps_for(session)
            raw = await self._call(session, self._require(caps, "positions"), {})
            return _parse_positions(raw)

    def get_latest_price(self, symbol: str) -> float:
        return self._run(self._get_latest_price(symbol))

    async def _get_latest_price(self, symbol: str) -> float:
        async with self._session() as session:
            caps = await self._caps_for(session)
            tool = self._require(caps, "quote")
            args: dict[str, Any] = {}
            if not _assign(args, tool, _SYMBOL_KEYS, symbol):
                args["symbol"] = symbol
            raw = await self._call(session, tool, args)
            return _parse_price(raw, symbol)

    def get_bars(self, symbol: str, *, timeframe: str, limit: int) -> list[dict[str, Any]]:
        return self._run(self._get_bars(symbol, timeframe=timeframe, limit=limit))

    async def _get_bars(self, symbol: str, *, timeframe: str, limit: int) -> list[dict[str, Any]]:
        async with self._session() as session:
            caps = await self._caps_for(session)
            tool = self._require(caps, "bars")
            args: dict[str, Any] = {}
            _assign(args, tool, _SYMBOL_KEYS, symbol)
            _assign(args, tool, _TF_KEYS, _timeframe_value(tool, timeframe))
            _assign(args, tool, _LIMIT_KEYS, max(1, min(int(limit), MAX_BARS)))
            raw = await self._call(session, tool, args)
            return _parse_bars(raw)[: max(1, min(int(limit), MAX_BARS))]

    def get_scan_data(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Not supported over the Agentic MCP; the scan degrades to a note.

        Batched snapshot/bars endpoints have no capability mapping here, and
        sweeping the allowlist one `get_bars` call at a time would be dozens of
        MCP round-trips per cycle. Callers (trader.state) catch this and render
        "scan unavailable" instead of failing the cycle.
        """
        raise BrokerError("market scan is not supported by the Robinhood MCP adapter")

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """No open-orders capability is mapped; fail closed with a clear reason.

        trader.state degrades this to a rendered note rather than a dead cycle.
        """
        raise BrokerError("open-order listing is not supported by the Robinhood MCP adapter")

    def cancel_open_orders(self, symbol: str) -> int:
        """No cancel capability is mapped. execution.place_order logs and proceeds."""
        raise BrokerError("order cancellation is not supported by the Robinhood MCP adapter")

    def submit_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: str,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> OrderReceipt:
        """Do not call this directly. `execution.place_order` is the sole caller."""
        if side not in ("buy", "sell"):
            raise BrokerError(f"side must be 'buy' or 'sell', got {side!r}")
        return self._run(
            self._submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
            )
        )

    async def _submit_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: str,
        stop_price: float | None,
        take_profit_price: float | None,
    ) -> OrderReceipt:
        async with self._session() as session:
            caps = await self._caps_for(session)
            tool = self._require(caps, "write")
            args = bind_order_args(
                tool,
                symbol=symbol,
                qty=qty,
                side=side,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
            )
            raw = await self._call(session, tool, args)
            return _parse_receipt(raw)

    def get_order(self, order_id: str) -> OrderFill:
        return self._run(self._get_order(order_id))

    async def _get_order(self, order_id: str) -> OrderFill:
        async with self._session() as session:
            caps = await self._caps_for(session)
            tool = self._require(caps, "order_status")
            args: dict[str, Any] = {}
            if not _assign(args, tool, _ORDER_ID_KEYS, order_id):
                raise BrokerError(f"Robinhood tool {tool.name!r} has no order id parameter")
            raw = await self._call(session, tool, args)
            return _parse_fill(raw, order_id)


def interactive_login(token_path: Path) -> dict[str, Any]:
    """Browser OAuth on the desktop. `trader run` cannot complete this."""

    async def _login() -> dict[str, Any]:
        broker = RobinhoodMcpBroker(token_path, interactive=True)
        async with broker._session() as session:
            listed = await session.list_tools()
            tools = [DiscoveredTool.from_sdk(t) for t in listed.tools]
            caps = map_capabilities(tools)
            broker._caps = caps
        return {
            "tools": [t.name for t in tools],
            "capabilities": {k: (v.name if v else None) for k, v in caps.items()},
        }

    return asyncio.run(_login())
