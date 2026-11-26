"""Execution abstraction: paper fills and live CLOB orders.

The engine only ever sees an :class:`Executor`.  The paper path keeps the
existing order-book simulation; the live path persists an order intent before
submitting, so a crash cannot silently duplicate a real order.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from . import broker
from .api import ClobApi
from .config import LiveConfig, PaperConfig, RunConfig
from .store import Store

LIVE_MODES = ("paper", "live")
LIVE_OPEN_STATUSES = ("INTENT", "PREPARED", "SUBMITTED", "PARTIAL")


class LiveTradingError(RuntimeError):
    """Raised when live mode is requested but cannot be configured safely."""


@dataclass(frozen=True)
class TradeIntent:
    dedupe_key: str
    target: str
    condition_id: str
    token_id: str
    outcome: str
    title: str
    event_slug: str
    side: str                 # "BUY" | "SELL"
    shares: float
    limit_price: float
    signal_price: float
    fee_rate: float
    max_spend: float | None = None       # available cash, including fee
    max_market: float | None = None      # remaining per-condition cost
    min_notional: float = 0.0
    strict: bool = False                 # exits accept any bid
    source: str = "onchain"


@dataclass(frozen=True)
class Fill:
    shares: float
    price: float
    fee: float
    order_id: int | None = None
    raw: dict[str, Any] | None = None


@dataclass
class ExecutionResult:
    fills: list[Fill] = field(default_factory=list)
    status: str = "no_fill"
    reason: str | None = None
    order_id: str | None = None
    raw: dict[str, Any] | None = None
    ledger_id: int | None = None

    @property
    def filled_shares(self) -> float:
        return sum(f.shares for f in self.fills)

    @property
    def notional(self) -> float:
        return sum(f.shares * f.price for f in self.fills)

    @property
    def fee(self) -> float:
        return sum(f.fee for f in self.fills)


@dataclass
class EntryStatus:
    """Cumulative state of a resting/terminal order in the local ledger."""

    order_id: int | None = None
    status: str = "unknown"       # open|partial|filled|canceled|rejected|unknown
    remote_status: str = ""
    filled_shares: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    applied_shares: float = 0.0
    applied_notional: float = 0.0
    applied_fee: float = 0.0
    canceled: bool = False
    terminal: bool = False
    found: bool = False
    reason: str | None = None


class Executor(Protocol):
    is_live: bool
    min_shares: float

    def available_cash(self, fallback: float, store: Store | None = None) -> float: ...
    def buy(self, intent: TradeIntent,
            store: Store | None = None) -> ExecutionResult: ...
    def sell(self, intent: TradeIntent,
             store: Store | None = None) -> ExecutionResult: ...
    def start_maker_buy(self, intent: TradeIntent,
                        store: Store | None = None,
                        pending_id: int | None = None) -> ExecutionResult: ...
    def refresh_order(self, ledger_id: int,
                      store: Store | None = None) -> EntryStatus: ...
    def cancel_entry(self, ledger_id: int,
                     store: Store | None = None) -> bool: ...
    def entry_cancel_reason(self, store: Store | None = None) -> str | None: ...
    def reconcile(self, store: Store | None = None) -> None: ...
    def redeem(self, condition_id: str) -> bool: ...


class PaperExecutor:
    """Order-book walk, identical to the pre-live engine behaviour."""

    is_live = False

    def __init__(self, clob: ClobApi, cfg: PaperConfig,
                 logger: logging.Logger | None = None):
        self.clob = clob
        self.cfg = cfg
        self.min_shares = float(cfg.min_shares)
        self.log = logger or logging.getLogger("polycopy.execution.paper")

    def available_cash(self, fallback: float,
                       store: Store | None = None) -> float:
        return float(fallback)

    def buy(self, intent: TradeIntent,
            store: Store | None = None) -> ExecutionResult:
        payload = self.clob.book(intent.token_id)
        if not payload:
            return ExecutionResult(status="no_fill", reason="book_unavailable")
        book = broker.parse_book(payload)
        ex, reason = broker.simulate_buy(
            book, intent.shares, intent.signal_price, self.cfg)
        if ex is None:
            return ExecutionResult(status="no_fill", reason=reason or "no_fill")
        fee = broker.taker_fee(ex.shares, ex.price, intent.fee_rate)
        notional = ex.shares * ex.price
        if (intent.max_market is not None
                and notional > intent.max_market + 1e-9) or (
                intent.max_spend is not None
                and notional + fee > intent.max_spend + 1e-9):
            scale = 1.0
            if notional > 0 and intent.max_market is not None:
                scale = min(scale, intent.max_market / notional)
            if notional + fee > 0 and intent.max_spend is not None:
                scale = min(scale, intent.max_spend / (notional + fee))
            scaled = ex.shares * max(0.0, scale)
            if scaled * ex.price < intent.min_notional:
                reason = ("insufficient_cash"
                          if intent.max_spend is not None
                          and intent.max_spend < (intent.max_market or 0)
                          else "max_market_exposure")
                return ExecutionResult(status="no_fill", reason=reason)
            ex2, reason = broker.simulate_buy(
                book, scaled, intent.signal_price, self.cfg)
            if ex2 is None:
                return ExecutionResult(status="no_fill",
                                       reason=reason or "no_fill")
            ex = ex2
            fee = broker.taker_fee(ex.shares, ex.price, intent.fee_rate)
        return ExecutionResult(
            fills=[Fill(ex.shares, ex.price, fee)], status="filled")

    def sell(self, intent: TradeIntent,
             store: Store | None = None) -> ExecutionResult:
        payload = self.clob.book(intent.token_id)
        if not payload:
            return ExecutionResult(status="no_fill", reason="book_unavailable")
        book = broker.parse_book(payload)
        ex, reason = broker.simulate_sell(
            book, intent.shares, intent.signal_price, self.cfg,
            strict=intent.strict,
        )
        if ex is None:
            return ExecutionResult(status="no_fill", reason=reason or "no_fill")
        fee = broker.taker_fee(ex.shares, ex.price, intent.fee_rate)
        return ExecutionResult(
            fills=[Fill(ex.shares, ex.price, fee)], status="filled")

    def start_maker_buy(self, intent: TradeIntent,
                        store: Store | None = None,
                        pending_id: int | None = None) -> ExecutionResult:
        return ExecutionResult(status="rejected", reason="maker_unsupported")

    def refresh_order(self, ledger_id: int,
                      store: Store | None = None) -> EntryStatus:
        return EntryStatus(order_id=int(ledger_id), status="unknown")

    def cancel_entry(self, ledger_id: int,
                     store: Store | None = None) -> bool:
        return False

    def entry_cancel_reason(self, store: Store | None = None) -> str | None:
        return None

    def reconcile(self, store: Store | None = None) -> None:
        return None

    def redeem(self, condition_id: str) -> bool:
        return False


class LiveExecutor:
    """Submit FAK orders through a gateway, with safety gates and a ledger."""

    is_live = True

    def __init__(self, store: Store, cfg: LiveConfig, clob: ClobApi,
                 gateway: Any = None, *, dry_run: bool | None = None,
                 logger: logging.Logger | None = None):
        self.store = store
        self.cfg = cfg
        self.clob = clob
        self.gateway = gateway
        self.dry_run = bool(cfg.dry_run if dry_run is None else dry_run)
        self.min_shares = float(cfg.min_order_shares)
        self.log = logger or logging.getLogger("polycopy.execution.live")
        self._lock = threading.RLock()
        self._balance_cache: tuple[float, float] = (0.0, 0.0)

    # ------------------------------------------------------------------ buys
    def _store_for(self, store: Store | None) -> Store:
        return store if store is not None else self.store

    def available_cash(self, fallback: float,
                       store: Store | None = None) -> float:
        """Use real collateral in live mode; never size off the paper ledger."""
        if self.dry_run:
            return float(fallback)
        now = time.time()
        cached_at, cached = self._balance_cache
        if cached_at and now - cached_at < 5.0:
            return cached
        balance = self.balance()
        if balance is None:
            self.log.error(
                "live collateral balance unavailable; blocking new buys")
            return 0.0
        value = max(0.0, float(balance))
        self._balance_cache = (now, value)
        return value

    def buy(self, intent: TradeIntent,
            store: Store | None = None) -> ExecutionResult:
        store = self._store_for(store)
        with self._lock:
            blocked = self._buy_block_reason(intent, store)
            if blocked:
                return ExecutionResult(status="rejected", reason=blocked)
            tick = self._tick_size(intent.token_id)
            limit = _floor_tick(intent.limit_price, tick)
            shares = self._cap_shares(intent, limit, store)
            if shares <= 0:
                return ExecutionResult(status="rejected",
                                       reason="below_min_size")
            order = store.create_order(
                dedupe_key=intent.dedupe_key, target=intent.target,
                mode="live", side="BUY", condition_id=intent.condition_id,
                token_id=intent.token_id, outcome=intent.outcome,
                title=intent.title, event_slug=intent.event_slug,
                limit_price=limit, size=shares, fee_rate=intent.fee_rate,
            )
            store.commit()
            order_id = int(order["id"])
            if order.get("status") not in (None, "INTENT"):
                # The unique key says this signal already has an order.
                return ExecutionResult(status="rejected", reason="duplicate_order",
                                       order_id=str(order_id),
                                       ledger_id=order_id)
            if self.dry_run:
                self._update(order_id, store=store, status="DRY_RUN",
                             reason="dry_run")
                return ExecutionResult(status="dry_run", reason="dry_run",
                                       order_id=str(order_id),
                                       ledger_id=order_id)
            if self.gateway is None:
                self._update(order_id, store=store, status="REJECTED",
                             reason="live_gateway_unavailable")
                return ExecutionResult(
                    status="rejected", reason="live_gateway_unavailable",
                    order_id=str(order_id), ledger_id=order_id)
            try:
                result = self.gateway.place_fak_buy(
                    token_id=intent.token_id, price=limit, size=shares,
                    fee_rate=intent.fee_rate,
                    on_prepared=lambda cid, raw: self._prepared(
                        order_id, cid, raw, store=store),
                )
            except Exception as exc:  # noqa: BLE001
                self.log.exception("live buy failed token=%s", intent.token_id)
                self._update(order_id, store=store, status="UNKNOWN",
                             reason=f"submit_error:{type(exc).__name__}")
                return ExecutionResult(status="rejected",
                                       reason="submit_error", order_id=str(order_id),
                                       ledger_id=order_id)
            return self._finish_order(order_id, "BUY", result, store=store)

    # ----------------------------------------------------------------- sells
    def sell(self, intent: TradeIntent,
             store: Store | None = None) -> ExecutionResult:
        store = self._store_for(store)
        with self._lock:
            if self.gateway is None and not self.dry_run:
                return ExecutionResult(status="rejected",
                                       reason="live_gateway_unavailable")
            tick = self._tick_size(intent.token_id)
            limit = _ceil_tick(intent.limit_price, tick)
            shares = _round_shares(intent.shares)
            if shares < self.cfg.min_order_shares:
                return ExecutionResult(status="rejected",
                                       reason="below_min_size")
            order = store.create_order(
                dedupe_key=intent.dedupe_key, target=intent.target,
                mode="live", side="SELL", condition_id=intent.condition_id,
                token_id=intent.token_id, outcome=intent.outcome,
                title=intent.title, event_slug=intent.event_slug,
                limit_price=limit, size=shares, fee_rate=intent.fee_rate,
            )
            store.commit()
            order_id = int(order["id"])
            if order.get("status") not in (None, "INTENT"):
                return ExecutionResult(status="rejected",
                                       reason="duplicate_order",
                                       order_id=str(order_id),
                                       ledger_id=order_id)
            if self.dry_run:
                self._update(order_id, store=store, status="DRY_RUN",
                             reason="dry_run")
                return ExecutionResult(status="dry_run", reason="dry_run",
                                       order_id=str(order_id),
                                       ledger_id=order_id)
            try:
                result = self.gateway.place_fak_sell(
                    token_id=intent.token_id, price=limit, size=shares,
                    fee_rate=intent.fee_rate,
                    on_prepared=lambda cid, raw: self._prepared(
                        order_id, cid, raw, store=store),
                )
            except Exception as exc:  # noqa: BLE001
                self.log.exception("live sell failed token=%s", intent.token_id)
                self._update(order_id, store=store, status="UNKNOWN",
                             reason=f"submit_error:{type(exc).__name__}")
                return ExecutionResult(status="rejected",
                                       reason="submit_error", order_id=str(order_id),
                                       ledger_id=order_id)
            return self._finish_order(order_id, "SELL", result, store=store)

    # ----------------------------------------------------------- maker entry
    def start_maker_buy(self, intent: TradeIntent,
                        store: Store | None = None,
                        pending_id: int | None = None) -> ExecutionResult:
        """Place a resting GTC buy at the target's price.

        A GTC order that crosses the spread fills immediately as a taker; the
        fee estimate follows the book at placement.  A resting order pays the
        maker fee (zero for the markets this bot trades).
        """
        store = self._store_for(store)
        with self._lock:
            blocked = self._buy_block_reason(
                intent, store, exclude_pending_id=pending_id)
            if blocked:
                return ExecutionResult(status="rejected", reason=blocked)
            tick = self._tick_size(intent.token_id)
            limit = _floor_tick(intent.limit_price, tick)
            shares = self._cap_shares(
                intent, limit, store, use_depth=False,
                exclude_pending_id=pending_id)
            if shares <= 0:
                return ExecutionResult(status="rejected",
                                       reason="below_min_size")
            ask = self._best_ask(intent.token_id)
            crossed = ask is not None and ask <= limit + 1e-12
            fee_rate = float(intent.fee_rate) if crossed else 0.0
            order = store.create_order(
                dedupe_key=intent.dedupe_key, target=intent.target,
                mode="live", side="BUY", condition_id=intent.condition_id,
                token_id=intent.token_id, outcome=intent.outcome,
                title=intent.title, event_slug=intent.event_slug,
                limit_price=limit, size=shares, fee_rate=fee_rate,
            )
            store.commit()
            order_id = int(order["id"])
            if order.get("status") not in (None, "INTENT"):
                return ExecutionResult(status="rejected",
                                       reason="duplicate_order",
                                       order_id=str(order_id),
                                       ledger_id=order_id)
            if self.dry_run:
                self._update(order_id, store=store, status="DRY_RUN",
                             reason="dry_run")
                return ExecutionResult(status="dry_run", reason="dry_run",
                                       order_id=str(order_id),
                                       ledger_id=order_id)
            if self.gateway is None:
                self._update(order_id, store=store, status="REJECTED",
                             reason="live_gateway_unavailable")
                return ExecutionResult(status="rejected",
                                       reason="live_gateway_unavailable",
                                       order_id=str(order_id),
                                       ledger_id=order_id)
            try:
                result = self.gateway.place_limit_buy(
                    token_id=intent.token_id, price=limit, size=shares,
                    fee_rate=fee_rate,
                    on_prepared=lambda cid, raw: self._prepared(
                        order_id, cid, raw, store=store),
                )
            except Exception as exc:  # noqa: BLE001
                self.log.exception("live maker buy failed token=%s",
                                   intent.token_id)
                self._update(order_id, store=store, status="UNKNOWN",
                             reason=f"submit_error:{type(exc).__name__}")
                return ExecutionResult(status="rejected",
                                       reason="submit_error",
                                       order_id=str(order_id),
                                       ledger_id=order_id)
            return self._finish_order(order_id, "BUY", result, store=store)

    def entry_cancel_reason(self, store: Store | None = None) -> str | None:
        """A resting maker order is a new buy; kill switch/day loss cancels it."""
        store = self._store_for(store)
        if Path(self.cfg.kill_switch_path).exists():
            return "kill_switch"
        if self.cfg.max_daily_loss_usd > 0:
            day_start = int(datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp())
            pnl = store.realized_pnl_since(day_start)
            if pnl <= -abs(self.cfg.max_daily_loss_usd):
                return "daily_loss_limit"
        return None

    def refresh_order(self, ledger_id: int,
                      store: Store | None = None) -> EntryStatus:
        """Refresh one local order and apply cumulative fill deltas exactly once.

        Used both by the pending-buy processor and startup reconciliation.
        The fills table is the ledger of applied shares, so replaying a poll or
        recovering from a crash cannot double-apply a fill.
        """
        store = self._store_for(store)
        order = store.order(int(ledger_id))
        if not order:
            return EntryStatus(order_id=int(ledger_id), status="unknown",
                               reason="order_missing")
        local = str(order.get("status") or "")
        applied_s, applied_n, applied_f = store.fill_totals_for_order(ledger_id)
        filled = float(order.get("filled_size") or 0.0)
        if local == "FILLED" and filled > 0 and applied_s + 1e-9 >= filled:
            return EntryStatus(
                order_id=int(ledger_id), status="filled",
                filled_shares=filled,
                avg_price=float(order.get("avg_price") or 0.0),
                fee=float(order.get("fee") or 0.0),
                applied_shares=applied_s, applied_notional=applied_n,
                applied_fee=applied_f, terminal=True, found=True)
        terminal_local = local in ("CANCELED", "REJECTED")
        client_id = order.get("client_order_id") or order.get("exchange_order_id")
        if not client_id:
            if local in ("INTENT", "PREPARED", "SUBMITTED", "PARTIAL"):
                self.log.critical(
                    "live order %s has no client_order_id; manual check",
                    ledger_id)
            return EntryStatus(
                order_id=int(ledger_id), status=_entry_status(local),
                filled_shares=filled,
                avg_price=float(order.get("avg_price") or 0.0),
                fee=float(order.get("fee") or 0.0),
                applied_shares=applied_s, applied_notional=applied_n,
                applied_fee=applied_f, canceled=local == "CANCELED",
                terminal=terminal_local, found=False,
                reason="no_client_order_id")
        if self.gateway is None:
            return EntryStatus(
                order_id=int(ledger_id), status=_entry_status(local),
                filled_shares=filled,
                applied_shares=applied_s, applied_notional=applied_n,
                applied_fee=applied_f, canceled=local == "CANCELED",
                terminal=terminal_local, found=False,
                reason="live_gateway_unavailable")
        try:
            remote = self.gateway.get_order(str(client_id))
        except Exception:  # noqa: BLE001
            self.log.exception("order lookup failed id=%s", client_id)
            return EntryStatus(
                order_id=int(ledger_id), status=_entry_status(local),
                filled_shares=filled,
                applied_shares=applied_s, applied_notional=applied_n,
                applied_fee=applied_f, canceled=local == "CANCELED",
                terminal=terminal_local, found=False, reason="lookup_failed")
        if remote is None:
            return EntryStatus(
                order_id=int(ledger_id), status=_entry_status(local),
                filled_shares=filled,
                applied_shares=applied_s, applied_notional=applied_n,
                applied_fee=applied_f, canceled=local == "CANCELED",
                terminal=terminal_local, found=False, reason="not_found")
        parsed = self.gateway.parse_order(
            remote, side=str(order["side"]),
            fee_rate=float(order.get("fee_rate") or 0.0),
        )
        new_filled = max(filled, float(parsed.filled_size or 0.0))
        new_price = (float(parsed.avg_price or 0.0)
                     or float(order.get("avg_price") or 0.0)
                     or float(order.get("limit_price") or 0.0))
        new_fee = max(float(order.get("fee") or 0.0), float(parsed.fee or 0.0))
        parsed_canceled = bool(getattr(parsed, "canceled", False))
        if terminal_local:
            new_status = local
        elif parsed_canceled:
            new_status = "CANCELED"
        else:
            new_status = _ledger_status(parsed.status)
        store.update_order(
            ledger_id, status=new_status, filled_size=new_filled,
            avg_price=new_price, fee=new_fee,
            exchange_order_id=parsed.order_id or order.get("exchange_order_id"),
            raw=_json(parsed.raw), reason=parsed.reason,
        )
        store.commit()
        applied_s, applied_n, applied_f = store.fill_totals_for_order(ledger_id)
        total_notional = new_filled * new_price
        delta_s = new_filled - applied_s
        if delta_s > 1e-9:
            delta_n = max(0.0, total_notional - applied_n)
            delta_f = max(0.0, new_fee - applied_f)
            price = delta_n / delta_s if delta_n > 0 else new_price
            self._apply_reconciled(
                order, Fill(delta_s, price, delta_f, order_id=ledger_id),
                store=store)
            applied_s += delta_s
            applied_n += delta_n
            applied_f += delta_f
        status = _entry_status(new_status)
        return EntryStatus(
            order_id=int(ledger_id), status=status,
            remote_status=str(getattr(parsed, "remote_status", "") or ""),
            filled_shares=new_filled, avg_price=new_price, fee=new_fee,
            applied_shares=applied_s, applied_notional=applied_n,
            applied_fee=applied_f, canceled=new_status == "CANCELED",
            terminal=status in ("filled", "canceled", "rejected"),
            found=True, reason=parsed.reason,
        )

    def cancel_entry(self, ledger_id: int,
                     store: Store | None = None) -> bool:
        """Request cancellation and confirm it; a request is not confirmation."""
        store = self._store_for(store)
        with self._lock:
            order = store.order(int(ledger_id))
            if not order:
                return False
            local = str(order.get("status") or "")
            if local in ("CANCELED", "REJECTED", "FILLED"):
                return True
            client_id = order.get("client_order_id") or order.get("exchange_order_id")
            if not client_id:
                self._update(ledger_id, store=store, status="CANCELED",
                             reason="never_submitted")
                return True
            if self.gateway is None:
                return False
            try:
                self.gateway.cancel_order(str(client_id))
            except Exception:  # noqa: BLE001
                self.log.exception("cancel failed order=%s", ledger_id)
                return False
            status = self.refresh_order(ledger_id, store=store)
            return bool(status.canceled or status.status == "filled")

    # ------------------------------------------------------------- lifecycle
    def reconcile(self, store: Store | None = None) -> None:
        with self._lock:
            self._reconcile_locked(self._store_for(store))

    def _reconcile_locked(self, store: Store) -> None:
        """Never resubmit an intent; query the exchange instead.

        A crash between PREPARED and POST leaves an order with a client id but
        no exchange id.  Marking it UNKNOWN and requiring a human check is the
        only safe default for real money.
        """
        if self.gateway is None:
            return
        statuses = tuple(dict.fromkeys(
            LIVE_OPEN_STATUSES + ("FILLED", "CANCELED")))
        for order in store.orders(mode="live", statuses=statuses):
            order_id = int(order["id"])
            applied, _, _ = store.fill_totals_for_order(order_id)
            filled = float(order.get("filled_size") or 0.0)
            if (order["status"] in ("FILLED", "CANCELED")
                    and applied + 1e-9 >= filled):
                continue
            status = self.refresh_order(order_id, store=store)
            if not status.found and order["status"] in ("INTENT", "PREPARED"):
                self._update(order_id, store=store, status="UNKNOWN",
                             reason="not_found_on_exchange")
                self.log.critical(
                    "live order %s not found on exchange; manual check",
                    order["id"])

    def _apply_reconciled(self, order: dict[str, Any], fill: Fill, *,
                          store: Store) -> None:
        ts = int(time.time())
        side = str(order["side"]).upper()
        if side == "BUY":
            store.apply_buy(
                ts=ts, target=str(order["target"]), signal_id=None,
                condition_id=str(order["condition_id"]),
                token_id=str(order["token_id"]),
                outcome=str(order.get("outcome") or ""),
                title=str(order.get("title") or ""),
                event_slug=str(order.get("event_slug") or ""),
                shares=fill.shares, price=fill.price, fee=fill.fee,
                order_id=fill.order_id,
            )
            store.commit()
        else:
            dedupe = str(order.get("dedupe_key") or "")
            is_manual = dedupe.startswith("manual:")
            is_risk = dedupe.startswith("risk:")
            applied = store.apply_sell(
                ts=ts, target=str(order["target"]), signal_id=None,
                condition_id=str(order["condition_id"]),
                token_id=str(order["token_id"]),
                outcome=str(order.get("outcome") or ""),
                shares=fill.shares, price=fill.price, fee=fill.fee,
                note=("manual_sell" if is_manual
                      else "risk_exit" if is_risk else "reconciled"),
                order_id=fill.order_id, manual=is_manual,
                kind=("MANUAL" if is_manual else "RISK" if is_risk else "COPY"),
            )
            store.commit()
            if applied is None:
                self.log.critical(
                    "reconciled sell has no local position order=%s token=%s",
                    order["id"], order["token_id"])
                self._update(int(order["id"]), store=store,
                             reason="reconciled_fill_without_position")
                return
            pending = store.pending_exit(
                str(order["target"]), str(order["condition_id"]),
                str(order["token_id"]))
            if pending:
                remaining = max(0.0, float(pending["shares"]) - fill.shares)
                if remaining <= 1e-9:
                    store.delete_pending_exit(pending["id"])
                else:
                    store.update_pending_exit(
                        pending["id"], shares=remaining,
                        attempts=int(pending["attempts"]) + 1,
                        reason="reconciled", ts=ts)
                store.commit()
        self.log.warning(
            "ORDER FILL order=%s side=%s sh=%.4f px=%.4f fee=%.4f",
            order["id"], side, fill.shares, fill.price, fill.fee)

    def redeem(self, condition_id: str) -> bool:
        if self.dry_run or self.gateway is None:
            return False
        with self._lock:
            try:
                return bool(self.gateway.redeem(condition_id))
            except Exception:  # noqa: BLE001
                self.log.exception("redeem failed condition=%s", condition_id)
                return False

    def balance(self) -> float | None:
        if self.gateway is None:
            return None
        try:
            return self.gateway.balance()
        except Exception:  # noqa: BLE001
            self.log.exception("balance lookup failed")
            return None

    # --------------------------------------------------------------- helpers
    def _buy_block_reason(self, intent: TradeIntent, store: Store, *,
                          exclude_pending_id: int | None = None) -> str | None:
        kill = Path(self.cfg.kill_switch_path)
        if kill.exists():
            return "kill_switch"
        if self.cfg.max_daily_loss_usd > 0:
            day_start = int(datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp())
            pnl = store.realized_pnl_since(day_start)
            if pnl <= -abs(self.cfg.max_daily_loss_usd):
                return "daily_loss_limit"
        exposure = (store.total_open_cost()
                    + store.pending_buy_commitment(
                        exclude_id=exclude_pending_id))
        est = intent.shares * max(0.001, intent.limit_price)
        if (self.cfg.max_total_exposure_usd > 0
                and exposure + est > self.cfg.max_total_exposure_usd):
            return "max_total_exposure"
        return None

    def _cap_shares(self, intent: TradeIntent, limit: float, store: Store, *,
                    use_depth: bool = True,
                    exclude_pending_id: int | None = None) -> float:
        price = max(1e-6, limit)
        shares = max(0.0, intent.shares)
        if self.cfg.max_order_usd > 0:
            shares = min(shares, self.cfg.max_order_usd / price)
        if intent.max_spend is not None:
            fee_pct = max(0.0, intent.fee_rate) * (1.0 - price)
            shares = min(shares, intent.max_spend / (price * (1.0 + fee_pct)))
        if intent.max_market is not None:
            shares = min(shares, max(0.0, intent.max_market) / price)
        if self.cfg.max_total_exposure_usd > 0:
            committed = (store.total_open_cost()
                         + store.pending_buy_commitment(
                             exclude_id=exclude_pending_id))
            exposure_left = self.cfg.max_total_exposure_usd - committed
            if exposure_left <= 0:
                return 0.0
            shares = min(shares, exposure_left / price)
        if use_depth:
            depth = self._book_depth(intent.token_id, limit, side="BUY")
            if depth is not None:
                shares = min(shares, depth * self.cfg.max_book_depth_pct)
        shares = _round_shares(shares)
        if shares < self.cfg.min_order_shares:
            return 0.0
        return shares

    def _best_ask(self, token_id: str) -> float | None:
        payload = self.clob.book(token_id)
        if not payload:
            return None
        _, ask = broker.best_bid_ask(broker.parse_book(payload))
        return ask

    def _book_depth(self, token_id: str, limit: float,
                    side: str) -> float | None:
        payload = self.clob.book(token_id)
        if not payload:
            return None
        book = broker.parse_book(payload)
        if side == "BUY":
            levels = [lv for lv in book.asks if lv.price <= limit + 1e-12]
        else:
            levels = [lv for lv in book.bids if lv.price >= limit - 1e-12]
        if not levels:
            return 0.0
        return sum(lv.size for lv in levels)

    def _tick_size(self, token_id: str) -> float:
        if self.gateway is not None:
            try:
                tick = self.gateway.tick_size(token_id)
                if tick and tick > 0:
                    return float(tick)
            except Exception:  # noqa: BLE001
                self.log.exception("tick size lookup failed token=%s", token_id)
        payload = self.clob.book(token_id)
        if payload:
            try:
                tick = float(payload.get("tick_size") or 0.001)
                if tick > 0:
                    return tick
            except (TypeError, ValueError):
                pass
        return 0.001

    def _prepared(self, order_id: int, client_id: str | None,
                  raw: Any, store: Store | None = None) -> None:
        self._update(order_id, store=store, status="PREPARED",
                     client_order_id=str(client_id) if client_id else None,
                     raw=_json(raw))

    def _finish_order(self, order_id: int, side: str, result: Any,
                      store: Store | None = None) -> ExecutionResult:
        store = self._store_for(store)
        filled = float(getattr(result, "filled_size", 0.0) or 0.0)
        price = float(getattr(result, "avg_price", 0.0) or 0.0)
        fee = float(getattr(result, "fee", 0.0) or 0.0)
        status = str(getattr(result, "status", "rejected") or "rejected").lower()
        reason = getattr(result, "reason", None)
        remote_id = getattr(result, "order_id", None)
        raw = getattr(result, "raw", None)
        canceled = bool(getattr(result, "canceled", False))
        if status in ("submitted", "live", "delayed", "open"):
            self._update(order_id, store=store, status="SUBMITTED",
                         exchange_order_id=str(remote_id) if remote_id else None,
                         raw=_json(raw))
            return ExecutionResult(status="submitted", reason=reason,
                                   order_id=str(remote_id or ""), raw=raw,
                                   ledger_id=order_id)
        if filled > 0 and price > 0:
            if status in ("filled", "matched"):
                ledger_status = "FILLED"
            elif canceled:
                ledger_status = "CANCELED"
            else:
                ledger_status = "PARTIAL"
            self._update(order_id, store=store, status=ledger_status,
                         filled_size=filled, avg_price=price, fee=fee,
                         reason=reason,
                         exchange_order_id=str(remote_id) if remote_id else None,
                         raw=_json(raw))
            fills = [Fill(filled, price, fee, order_id=order_id, raw=raw)]
            return ExecutionResult(
                fills=fills,
                status=("filled" if ledger_status == "FILLED"
                        else "canceled" if ledger_status == "CANCELED"
                        else "partial"),
                reason=reason, order_id=str(remote_id or ""), raw=raw,
                ledger_id=order_id)
        ledger_status = "CANCELED" if status in (
            "canceled", "cancelled", "expired", "unmatched", "unfilled"
        ) else "REJECTED"
        self._update(order_id, store=store, status=ledger_status,
                     reason=reason,
                     exchange_order_id=str(remote_id) if remote_id else None,
                     raw=_json(raw))
        return ExecutionResult(status="no_fill", reason=reason or status,
                               order_id=str(remote_id or ""), raw=raw,
                               ledger_id=order_id)

    def _update(self, order_id: int, *, store: Store | None = None,
                **kwargs: Any) -> None:
        target_store = self._store_for(store)
        target_store.update_order(order_id, **kwargs)
        target_store.commit()


def _ledger_status(status: str) -> str:
    return {
        "filled": "FILLED", "matched": "FILLED",
        "partial": "PARTIAL",
        "submitted": "SUBMITTED", "live": "SUBMITTED", "open": "SUBMITTED",
        "canceled": "CANCELED", "cancelled": "CANCELED",
        "expired": "CANCELED", "unmatched": "CANCELED", "unfilled": "CANCELED",
        "rejected": "REJECTED",
    }.get(str(status or "").lower(), "SUBMITTED")


def _entry_status(ledger_status: str) -> str:
    return {
        "SUBMITTED": "open", "PREPARED": "open", "INTENT": "open",
        "PARTIAL": "partial", "FILLED": "filled",
        "CANCELED": "canceled", "REJECTED": "rejected",
        "DRY_RUN": "dry_run",
    }.get(str(ledger_status or "").upper(), "unknown")


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:20_000]
    except (TypeError, ValueError):
        return ""


def _round_shares(value: float) -> float:
    """Round down to the CLOB's usual 2-decimal share precision."""
    if value <= 0:
        return 0.0
    return math.floor(value * 100.0 + 1e-9) / 100.0


def _floor_tick(price: float, tick: float) -> float:
    tick = tick if tick > 0 else 0.001
    return round(math.floor(price / tick + 1e-9) * tick, 6)


def _ceil_tick(price: float, tick: float) -> float:
    tick = tick if tick > 0 else 0.001
    return round(math.ceil(price / tick - 1e-9) * tick, 6)


def build_executor(cfg: Any, store: Store, clob: ClobApi, *,
                   mode: str | None = None,
                   logger: logging.Logger | None = None) -> Executor:
    """Build the executor selected by ``run.mode``.

    ``mode`` lets the CLI override the config for a single canary run.
    """
    selected = str(mode or cfg.run.mode or RunConfig().mode).lower()
    if selected not in LIVE_MODES:
        raise ValueError(f"unknown execution mode {selected!r}")
    log = logger or logging.getLogger("polycopy.execution")
    if selected == "paper":
        return PaperExecutor(clob, cfg.paper, logger=log)
    if cfg.live.dry_run:
        return LiveExecutor(store, cfg.live, clob, gateway=None, dry_run=True,
                            logger=log)
    from .live_client import SdkClobGateway
    gateway = SdkClobGateway.from_config(cfg.live, logger=log)
    return LiveExecutor(store, cfg.live, clob, gateway=gateway, dry_run=False,
                        logger=log)
