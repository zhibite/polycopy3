"""On-chain decoding and RPC log streaming for Polymarket CTF Exchange V2.

The exchange emits one ``OrderFilled`` event per order fill.  For the wallet
that owns the order the event has ``maker == wallet``; a taker (market) order
is emitted once with ``taker == exchange`` and aggregates all maker fills, so
this single event matches the data-api activity row one-for-one.

Event (V2):
    OrderFilled(
        bytes32 indexed orderHash,
        address indexed maker,
        address indexed taker,
        uint8 side,                 # 0 = BUY, 1 = SELL (maker perspective)
        uint256 tokenId,
        uint256 makerAmountFilled,
        uint256 takerAmountFilled,
        uint256 fee,
        bytes32 builder,
        bytes32 metadata,
    )

Amount mapping for the maker (the wallet whose activity we copy):
    BUY  -> shares = takerAmount, notional = makerAmount, cash_out = notional + fee
    SELL -> shares = makerAmount, proceeds = takerAmount, cash_in   = proceeds - fee

``price`` is the gross order price (notional / shares), which is what
data-api reports; ``usdc_size`` is the actual cash flow (net of fee).
"""
from __future__ import annotations

import collections
import json
import logging
import os
import queue
import threading
import time
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from . import rules
from .http import UA

V2_CTF_EXCHANGE = "0xe111180000d2663c0091e4f400237545b87b996b"
V2_NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310f59"
V2_EXCHANGES = (V2_CTF_EXCHANGE, V2_NEG_RISK_EXCHANGE)

ORDER_FILLED_V2_TOPIC = (
    "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
)

USDC_DECIMALS = 6
TOKEN_DECIMALS = 6

# drpc was the most reliable in the PoC (30/30 eth_getLogs); publicnode is
# kept as a fallback even though it intermittently rejects log ranges.
DEFAULT_RPC_URLS = (
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
)
DEFAULT_WS_URLS = (
    "wss://polygon.drpc.org",
    "wss://polygon-bor-rpc.publicnode.com",
)

# Polygon produces ~2s blocks; used only to turn a lookback in seconds into a
# starting block for catch-up.  A few extra blocks are harmless (dedupe).
SECONDS_PER_BLOCK = 2


class RpcError(RuntimeError):
    pass


def pad_address(address: str) -> str:
    clean = address.lower().removeprefix("0x")
    return "0x" + clean.rjust(64, "0")


def lookback_blocks(seconds: int) -> int:
    if seconds <= 0:
        return 0
    return max(1, int(seconds / SECONDS_PER_BLOCK) + 5)


def _hex_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 16 if str(value).startswith("0x") else 10)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class OrderFill:
    """A decoded V2 OrderFilled event, from the maker's perspective."""

    maker: str
    taker: str
    exchange: str
    tx_hash: str
    log_index: int
    block_number: int
    block_ts: int | None
    order_hash: str
    side: str                      # "BUY" | "SELL"
    token_id: str
    shares_units: int              # CTF tokens, 6 decimals
    gross_units: int               # USDC notional, 6 decimals
    fee_units: int                 # USDC, 6 decimals
    received_at: float = 0.0       # local time when the feed decoded it

    @property
    def shares(self) -> float:
        return self.shares_units / 10**TOKEN_DECIMALS

    @property
    def gross_usd(self) -> float:
        return self.gross_units / 10**USDC_DECIMALS

    @property
    def fee_usd(self) -> float:
        return self.fee_units / 10**USDC_DECIMALS

    @property
    def price(self) -> float:
        if self.shares_units <= 0:
            return 0.0
        return self.gross_units / self.shares_units

    @property
    def net_usd(self) -> float:
        """Cash actually paid (BUY) or received (SELL), net of fee."""
        if self.side == "BUY":
            return (self.gross_units + self.fee_units) / 10**USDC_DECIMALS
        return (self.gross_units - self.fee_units) / 10**USDC_DECIMALS

    def is_mine(self, wallet: str) -> bool:
        return self.maker.lower() == wallet.lower()

    def to_row(self, meta: "TokenMeta", *, source: str = "onchain") -> dict[str, Any]:
        """Render a data-api-shaped activity row for the existing engine."""
        return {
            "proxy_wallet": self.maker,
            "timestamp": int(self.block_ts or 0),
            "condition_id": meta.condition_id,
            "type": "TRADE",
            "size": round(self.shares, 6),
            "usdc_size": round(self.net_usd, 6),
            "transaction_hash": self.tx_hash,
            "price": round(self.price, 10),
            "token_id": self.token_id,
            "side": self.side,
            "outcome": meta.outcome,
            "outcome_index": meta.outcome_index,
            "title": meta.title,
            "slug": meta.slug,
            "event_slug": meta.event_slug,
            "end_ts": meta.end_ts,
            "log_index": self.log_index,
            "source": source,
        }


def decode_order_filled(log: dict[str, Any]) -> OrderFill | None:
    """Decode one V2 OrderFilled log.  Returns None for other logs."""
    address = str(log.get("address") or "").lower()
    if address not in V2_EXCHANGES:
        return None
    topics = log.get("topics") or []
    if len(topics) < 4 or str(topics[0]).lower() != ORDER_FILLED_V2_TOPIC:
        return None
    data = str(log.get("data") or "")
    if data.startswith("0x"):
        data = data[2:]
    if len(data) < 7 * 64:
        return None
    words = [data[i:i + 64] for i in range(0, 7 * 64, 64)]
    try:
        side = int(words[0], 16)
        token_id = str(int(words[1], 16))
        maker_units = int(words[2], 16)
        taker_units = int(words[3], 16)
        fee_units = int(words[4], 16)
    except ValueError:
        return None
    if side not in (0, 1):
        return None

    if side == 0:
        shares_units, gross_units, side_name = taker_units, maker_units, "BUY"
    else:
        shares_units, gross_units, side_name = maker_units, taker_units, "SELL"
    if shares_units <= 0:
        return None

    return OrderFill(
        maker="0x" + str(topics[2])[-40:].lower(),
        taker="0x" + str(topics[3])[-40:].lower(),
        exchange=address,
        tx_hash=str(log.get("transactionHash") or "").lower(),
        log_index=int(_hex_int(log.get("logIndex"), 0) or 0),
        block_number=int(_hex_int(log.get("blockNumber"), 0) or 0),
        block_ts=_hex_int(log.get("blockTimestamp")),
        order_hash=str(topics[1]).lower(),
        side=side_name,
        token_id=token_id,
        shares_units=shares_units,
        gross_units=gross_units,
        fee_units=fee_units,
    )


class RpcClient:
    """Minimal JSON-RPC client with endpoint rotation and retries."""

    def __init__(self, urls: Iterable[str] | None = None, *, timeout: int = 20,
                 retries: int = 4,
                 logger: logging.Logger | None = None):
        urls = [u.strip() for u in (urls or DEFAULT_RPC_URLS) if u and u.strip()]
        self.urls = urls or list(DEFAULT_RPC_URLS)
        self.timeout = timeout
        self.retries = max(1, retries)
        self.log = logger or logging.getLogger("polycopy.onchain.rpc")
        self._idx = 0

    def _post(self, payload: Any) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            url = self.urls[self._idx % len(self.urls)]
            try:
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": UA},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                if isinstance(body, dict) and body.get("error") is not None:
                    raise RpcError(str(body["error"])[:300])
                return body
            except Exception as exc:  # network, JSON, or JSON-RPC error
                last = exc
                self.log.warning("RPC %s failed: %s", url, exc)
                self._idx += 1
                if attempt + 1 < self.retries:
                    time.sleep(min(5.0, 0.5 * (attempt + 1)))
        raise RpcError(f"all RPC endpoints failed: {last}")

    def call(self, method: str, params: list[Any]) -> Any:
        body = self._post({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params})
        if isinstance(body, list):
            body = body[0] if body else {}
        if not isinstance(body, dict):
            raise RpcError(f"unexpected RPC response for {method}")
        if body.get("error") is not None:
            raise RpcError(str(body["error"])[:300])
        return body.get("result")

    def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """Run several calls in one HTTP round trip; failures come back None."""
        if not calls:
            return []
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
            for i, (method, params) in enumerate(calls)
        ]
        body = self._post(payload)
        if isinstance(body, dict):
            raise RpcError(str(body.get("error") or body)[:300])
        by_id = {item.get("id"): item for item in body if isinstance(item, dict)}
        out: list[Any] = []
        for i in range(len(calls)):
            item = by_id.get(i) or {}
            out.append(None if item.get("error") is not None else item.get("result"))
        return out

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_timestamp(self, block_number: int) -> int | None:
        block = self.call("eth_getBlockByNumber", [hex(block_number), False])
        if not isinstance(block, dict):
            return None
        return _hex_int(block.get("timestamp"))

    def transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        body = self.call("eth_getTransactionReceipt", [tx_hash])
        return body if isinstance(body, dict) else None

    def logs(self, addresses: Iterable[str], wallets: Iterable[str],
             from_block: int, to_block: int) -> list[dict[str, Any]]:
        wallets = [w for w in wallets if w]
        flt: dict[str, Any] = {
            "address": list(addresses),
            "fromBlock": hex(max(0, from_block)),
            "toBlock": hex(max(0, to_block)),
        }
        if wallets:
            flt["topics"] = [ORDER_FILLED_V2_TOPIC, None,
                             [pad_address(w) for w in wallets]]
        else:
            flt["topics"] = [ORDER_FILLED_V2_TOPIC]
        body = self.call("eth_getLogs", [flt])
        return body if isinstance(body, list) else []


@dataclass(frozen=True)
class TokenMeta:
    condition_id: str
    token_id: str
    outcome: str = ""
    outcome_index: int | None = None
    title: str = ""
    slug: str = ""
    event_slug: str = ""
    end_ts: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "outcome": self.outcome,
            "outcome_index": self.outcome_index,
            "title": self.title,
            "slug": self.slug,
            "event_slug": self.event_slug,
            "end_ts": self.end_ts,
        }


class TokenResolver:
    """Resolve a CTF token id to condition id + display metadata, with cache."""

    def __init__(self, clob: Any, store: Any | None = None, gamma: Any | None = None,
                 *, include_event_slug: bool = False,
                 logger: logging.Logger | None = None):
        self.clob = clob
        self.store = store
        self.gamma = gamma
        self.include_event_slug = include_event_slug
        self.log = logger or logging.getLogger("polycopy.onchain.meta")
        self._memory: dict[str, TokenMeta | None] = {}

    def resolve(self, token_id: str) -> TokenMeta | None:
        token_id = str(token_id)
        if token_id in self._memory:
            return self._memory[token_id]
        cached = self._from_store(token_id)
        if cached is not None:
            self._memory[token_id] = cached
            return cached

        meta = self._fetch(token_id)
        if meta is not None:
            self._memory[token_id] = meta
            self._to_store(meta)
        return meta

    def _fetch(self, token_id: str) -> TokenMeta | None:
        try:
            body = self.clob.market_by_token(token_id)
        except Exception:
            self.log.exception("markets-by-token failed for %s", token_id)
            return None
        if not isinstance(body, dict):
            return None
        condition_id = str(body.get("condition_id") or "")
        if not condition_id:
            return None
        try:
            market = self.clob.market(condition_id)
        except Exception:
            self.log.exception("market lookup failed for %s", condition_id)
            return None
        if not isinstance(market, dict):
            return None
        tokens = market.get("tokens") or []
        outcome = ""
        outcome_index: int | None = None
        for idx, token in enumerate(tokens):
            if str(token.get("token_id") or "") == token_id:
                outcome = str(token.get("outcome") or "")
                outcome_index = idx
                break
        if not outcome:
            # Never create a position without an outcome label: settlement
            # compares it to the winning outcome and a blank would always lose.
            self.log.warning("token %s missing from CLOB market %s",
                             token_id, condition_id)
            return None
        event_slug = ""
        end_ts = rules.market_end_ts(market)
        if self.gamma is not None and (self.include_event_slug or end_ts is None):
            try:
                gm = self.gamma.market_by_condition(condition_id) or {}
                if end_ts is None:
                    end_ts = rules.market_end_ts(gm)
                if self.include_event_slug:
                    events = gm.get("events") or []
                    if events:
                        event_slug = str(events[0].get("slug") or "")
            except Exception:
                self.log.debug("gamma metadata lookup failed for %s",
                               condition_id, exc_info=True)
        return TokenMeta(
            condition_id=condition_id,
            token_id=token_id,
            outcome=outcome,
            outcome_index=outcome_index,
            title=str(market.get("question") or ""),
            slug=str(market.get("market_slug") or ""),
            event_slug=event_slug,
            end_ts=end_ts,
        )

    def _from_store(self, token_id: str) -> TokenMeta | None:
        if self.store is None:
            return None
        raw = self.store.get_kv("tokenmeta:v2:" + token_id)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return TokenMeta(**data)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _to_store(self, meta: TokenMeta) -> None:
        if self.store is None:
            return
        try:
            self.store.set_kv("tokenmeta:v2:" + meta.token_id,
                              json.dumps(meta.as_json(), separators=(",", ":")))
        except Exception:
            self.log.debug("failed to cache token metadata", exc_info=True)


class OrderFilledFeed:
    """Background WebSocket log subscription with HTTP catch-up.

    One subscription filters both V2 exchanges for ``maker`` in the configured
    wallet set.  On reconnect the feed backfills missed blocks with
    ``eth_getLogs`` before resuming live messages.  If no WebSocket URL is
    configured (or the library is unavailable) it falls back to HTTP polling.
    """

    def __init__(self, rpc: RpcClient, wallets: Iterable[str], *,
                 ws_urls: Iterable[str] | None = None,
                 addresses: Iterable[str] = V2_EXCHANGES,
                 start_block: int | None = None,
                 catchup_chunk: int = 100,
                 poll_fallback_seconds: float = 1.0,
                 catchup_interval: float = 15.0,
                 max_reconnect_seconds: float = 30.0,
                 seen_limit: int = 20_000,
                 logger: logging.Logger | None = None):
        self.rpc = rpc
        self.wallets = [w.lower() for w in wallets if w]
        if not self.wallets:
            raise ValueError("OrderFilledFeed needs at least one wallet")
        self.addresses = tuple(a.lower() for a in addresses)
        self.ws_urls = [u for u in (ws_urls if ws_urls is not None
                                    else DEFAULT_WS_URLS) if u]
        self.start_block = start_block
        self.catchup_chunk = max(1, catchup_chunk)
        self.poll_fallback_seconds = max(0.2, poll_fallback_seconds)
        # Even while the subscription is healthy, poll eth_getLogs now and
        # then; a silent subscription must not lose fills.
        self.catchup_interval = max(5.0, catchup_interval)
        self._last_catchup_at = 0.0
        self.max_reconnect_seconds = max(1.0, max_reconnect_seconds)
        self.seen_limit = max(100, seen_limit)
        self.log = logger or logging.getLogger("polycopy.onchain.feed")

        self.queue: queue.Queue[OrderFill] = queue.Queue()
        self.ready = threading.Event()
        self.last_block = start_block
        self.connected = False
        self.reconnects = 0
        self.errors = 0
        self.last_error = ""
        self.last_message_at = 0.0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: dict[tuple[str, int], None] = {}
        self._seen_order: collections.deque[tuple[str, int]] = collections.deque()
        self._block_ts: dict[int, int] = {}
        self._block_ts_order: collections.deque[int] = collections.deque()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="onchain-feed",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def get(self, timeout: float | None = None) -> OrderFill | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "last_block": self.last_block,
            "queue": self.queue.qsize(),
            "reconnects": self.reconnects,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_message_at": self.last_message_at,
            "wallets": len(self.wallets),
        }

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if self.ws_urls:
                    self._run_ws()
                else:
                    self._run_poll()
                backoff = 1.0
                # A clean close should not turn into a reconnect hot loop.
                if self._stop.wait(1.0):
                    break
            except Exception as exc:
                self.connected = False
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log.warning("on-chain feed error: %s", self.last_error)
                if self._stop.wait(backoff):
                    break
                backoff = min(self.max_reconnect_seconds, backoff * 2)

    def _run_ws(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            self.log.warning("websockets is not installed; using HTTP polling")
            self.ws_urls = []
            self._run_poll()
            return

        url = self.ws_urls[self.reconnects % len(self.ws_urls)]
        self.reconnects += 1
        self.log.info("connecting %s wallets=%d", url, len(self.wallets))
        with connect(url, open_timeout=10, ping_interval=20, ping_timeout=20,
                     max_size=16 * 1024 * 1024) as ws:
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                "params": ["logs", self._filter()],
            }))
            deadline = time.time() + 10
            subscription = None
            while time.time() < deadline:
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
                if msg.get("id") != 1:
                    continue
                if msg.get("error") is not None:
                    raise RpcError(str(msg["error"])[:300])
                subscription = msg.get("result")
                break
            if not subscription:
                raise RpcError("log subscription not acknowledged")

            self.connected = True
            self.log.info("subscribed %s", subscription)
            self._catch_up()
            self.ready.set()

            while not self._stop.is_set():
                try:
                    msg = json.loads(ws.recv(timeout=5.0))
                except TimeoutError:
                    self._maybe_catch_up()
                    continue
                self.last_message_at = time.time()
                if msg.get("method") != "eth_subscription":
                    self._maybe_catch_up()
                    continue
                log = (msg.get("params") or {}).get("result") or {}
                # Live logs do not advance the contiguous catch-up frontier:
                # a subscription gap must be filled by eth_getLogs, not skipped
                # because a later block arrived over the socket.
                self._handle_log(log, catchup=False)
                self._maybe_catch_up()
        self.connected = False

    def _run_poll(self) -> None:
        self.connected = True
        self.log.info("RPC polling every %.1fs", self.poll_fallback_seconds)
        while not self._stop.is_set():
            self._catch_up()
            self.ready.set()
            self._stop.wait(self.poll_fallback_seconds)
        self.connected = False

    def _filter(self) -> dict[str, Any]:
        return {
            "address": list(self.addresses),
            "topics": [ORDER_FILLED_V2_TOPIC, None,
                       [pad_address(w) for w in self.wallets]],
        }

    def _maybe_catch_up(self) -> None:
        if time.time() - self._last_catchup_at < self.catchup_interval:
            return
        try:
            self._catch_up()
        except Exception:
            self.log.warning("periodic catch-up failed", exc_info=True)

    def _catch_up(self) -> None:
        self._last_catchup_at = time.time()
        head = self.rpc.block_number()
        if self.last_block is None:
            self.last_block = head if self.start_block is None else self.start_block
        start = self.last_block + 1
        if start > head:
            self.last_block = max(self.last_block, head)
            return
        block = start
        while block <= head and not self._stop.is_set():
            end = min(head, block + self.catchup_chunk - 1)
            logs = self.rpc.logs(self.addresses, self.wallets, block, end)
            for log in logs:
                self._handle_log(log, catchup=True)
            self.last_block = max(self.last_block, end)
            block = end + 1

    def _handle_log(self, log: dict[str, Any], *,
                    catchup: bool = True) -> None:
        if not isinstance(log, dict) or log.get("removed"):
            return
        fill = decode_order_filled(log)
        if fill is None or fill.maker not in self.wallets:
            return
        key = (fill.tx_hash, fill.log_index)
        if key in self._seen:
            return
        self._remember(key)
        if fill.block_ts is None:
            fill = replace(fill, block_ts=self._timestamp(fill.block_number))
        fill = replace(fill, received_at=time.time())
        if catchup:
            self.last_block = max(self.last_block or 0, fill.block_number)
        self.last_message_at = time.time()
        self.queue.put(fill)

    def _timestamp(self, block_number: int) -> int | None:
        cached = self._block_ts.get(block_number)
        if cached is not None:
            return cached
        ts = self.rpc.block_timestamp(block_number)
        if ts is not None:
            self._block_ts[block_number] = ts
            self._block_ts_order.append(block_number)
            while len(self._block_ts_order) > 5000:
                self._block_ts.pop(self._block_ts_order.popleft(), None)
        return ts

    def _remember(self, key: tuple[str, int]) -> None:
        self._seen[key] = None
        self._seen_order.append(key)
        while len(self._seen_order) > self.seen_limit:
            self._seen.pop(self._seen_order.popleft(), None)


@dataclass
class OnchainSettings:
    """Runtime settings for the on-chain source (env + CLI overrides)."""

    rpc_urls: list[str] = field(default_factory=lambda: list(DEFAULT_RPC_URLS))
    ws_urls: list[str] = field(default_factory=lambda: list(DEFAULT_WS_URLS))
    exchanges: list[str] = field(default_factory=lambda: list(V2_EXCHANGES))
    lookback_seconds: int = 0
    # drpc's free plan rejects eth_getLogs ranges over 100 blocks; 100 also
    # works on publicnode, so this default keeps catch-up on the primary URL.
    catchup_chunk: int = 100
    poll_fallback_seconds: float = 1.0
    trace_path: str | None = None

    @classmethod
    def from_env(cls) -> "OnchainSettings":
        settings = cls()
        http = os.environ.get("POLY_RPC_HTTP", "")
        ws = os.environ.get("POLY_RPC_WS", "")
        if http:
            settings.rpc_urls = [u.strip() for u in http.split(",") if u.strip()]
        if ws:
            settings.ws_urls = [u.strip() for u in ws.split(",") if u.strip()]
        if os.environ.get("POLY_ONCHAIN_LOOKBACK"):
            try:
                settings.lookback_seconds = int(os.environ["POLY_ONCHAIN_LOOKBACK"])
            except ValueError:
                pass
        settings.trace_path = os.environ.get("POLY_ONCHAIN_TRACE") or None
        return settings
