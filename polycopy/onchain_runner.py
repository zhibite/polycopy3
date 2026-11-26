"""Paper-trading loop driven by RPC OrderFilled logs instead of data-api."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import rules
from .engine import Engine
from .onchain import (
    OnchainSettings,
    OrderFill,
    OrderFilledFeed,
    RpcClient,
    TokenMeta,
    TokenResolver,
    lookback_blocks,
)
from .store import Store

CURSOR_KEY = "onchain:last_block"
RESTART_KEY = "bot:restart_request"


class OnchainRunner:
    """Drain decoded fills from the feed and feed them to the copy engine."""

    def __init__(self, engine: Engine, feed: OrderFilledFeed,
                 resolver: TokenResolver, settings: OnchainSettings,
                 *, trace_path: str | None = None,
                 logger: logging.Logger | None = None):
        self.engine = engine
        self.feed = feed
        self.resolver = resolver
        self.settings = settings
        self.log = logger or logging.getLogger("polycopy.onchain")
        self.store = engine.store
        self.trace_path = trace_path or settings.trace_path
        self._trace = None
        if self.trace_path:
            Path(self.trace_path).parent.mkdir(parents=True, exist_ok=True)
            self._trace = open(self.trace_path, "a", encoding="utf-8")
        self._last_maint = 0.0
        self._last_health = 0.0
        self._last_trade_tick = 0.0
        self._last_ledger_try = 0.0
        self._startup_ready = False
        self._processed = 0
        self._failed = 0
        self._cursor_pending: int | None = None
        self._stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._restart_requested = False

    def run(self) -> int:
        try:
            self.engine.executor.reconcile(store=self.engine.store)
        except Exception:
            self.log.exception("startup executor reconciliation failed")
        # Do not tail stale maker entries until the feed has caught up and the
        # target position ledger has been reconciled.
        self.engine.allow_tail_entries = False
        # A restart request left over from a previous process should not make
        # a fresh start immediately bounce.  Requests are only honoured while
        # this process is running.
        try:
            self.store.set_kv(RESTART_KEY, "")
            self.store.commit()
        except Exception:
            self.log.exception("could not clear stale restart request")
        self.feed.start()
        self._start_maintenance()
        self.log.info(
            "on-chain loop started mode=%s dry_run=%s: %s",
            "live" if self.engine.executor.is_live else "paper",
            self.engine.cfg.live.dry_run, self.feed.stats())
        try:
            while True:
                fill = self.feed.get(timeout=0.5)
                if fill is not None:
                    try:
                        self._handle(fill)
                    except Exception:
                        self.log.exception("fill handling failed tx=%s log=%d",
                                           fill.tx_hash, fill.log_index)

                if self._restart_requested:
                    self.log.warning("restart request observed; stopping feed")
                    break

                now = time.time()
                if (not self._startup_ready and self.feed.ready.is_set()
                        and self.feed.queue.empty()
                        and now - self._last_ledger_try >= 30.0):
                    self._last_ledger_try = now
                    try:
                        self.engine.reconcile_target_positions()
                    except Exception:
                        self.log.exception(
                            "startup target ledger reconciliation failed")
                    if (self.engine._target_ledger_ok
                            and self.feed.queue.empty()):
                        self.engine.allow_tail_entries = True
                        self._startup_ready = True
                        self.log.info(
                            "target ledger ready; maker tails enabled")

                if now - self._last_trade_tick >= 1.0:
                    try:
                        self.engine.trading_tick()
                    except Exception:
                        self.log.exception("trading tick failed")
                    self._last_trade_tick = now

                if now - self._last_maint >= 2.0:
                    try:
                        self.engine.maybe_reload_config()
                        self._persist_cursor()
                        self.store.commit()
                    except Exception:
                        self.log.exception("on-chain bookkeeping failed")
                    self._last_maint = now

                if now - self._last_health >= 60.0:
                    self._health()
                    self._last_health = now
        except KeyboardInterrupt:
            self.log.info("on-chain paper loop stopped by user")
        finally:
            self._stop.set()
            thread = self._maintenance_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=10.0)
            self.feed.stop()
            self.store.commit()
            if self._trace is not None:
                self._trace.close()
                self._trace = None
        if self._restart_requested:
            return self._reexec()
        return 0

    def _reexec(self) -> int:
        """Replace the process image with the same command, minus --mode.

        ``--mode`` is intentionally dropped so a dashboard restart honours
        ``[run].mode`` from the freshly saved config instead of the mode the
        process happened to start with.
        """
        args = _strip_mode_args(list(sys.argv[1:]))
        argv = [sys.executable, "-m", "polycopy"] + args
        self.log.warning("re-executing: %s", " ".join(argv))
        try:
            os.execv(sys.executable, argv)
        except Exception:
            self.log.exception("re-exec failed")
            return 1
        return 0

    # ----------------------------------------------------------- maintenance
    def _start_maintenance(self) -> None:
        """Settle/mark on a separate SQLite connection so fills never wait.

        ``mark()`` walks up to ``mark_limit`` books over HTTP; running that in
        the signal loop would add seconds of queueing delay once the paper
        account holds positions.
        """
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop, name="onchain-maintenance",
            daemon=True,
        )
        self._maintenance_thread.start()

    def _maintenance_loop(self) -> None:
        try:
            store = Store(self.store.path)
        except Exception:
            self.log.exception("maintenance thread could not open %s",
                               self.store.path)
            return
        try:
            store.ensure_account(self.engine.cfg.account.starting_cash)
            engine = Engine(
                self.engine.cfg, store, self.engine.data, self.engine.clob,
                self.engine.gamma, logger=self.log,
                executor=self.engine.executor,
            )
            while not self._stop.wait(5.0):
                try:
                    engine.maybe_reload_config()
                    if self._consume_restart(store):
                        self._restart_requested = True
                        self.log.warning("restart requested via dashboard")
                        self._stop.set()
                        return
                    engine.maintenance()
                except Exception:
                    self.log.exception("background maintenance failed")
        finally:
            store.commit()
            store.close()

    def _consume_restart(self, store: Store) -> bool:
        try:
            if not store.get_kv(RESTART_KEY):
                return False
            store.set_kv(RESTART_KEY, "")
            store.commit()
            return True
        except Exception:
            self.log.exception("restart request check failed")
            return False

    # ------------------------------------------------------------------ fills
    def _handle(self, fill: OrderFill) -> None:
        started = time.time()
        targets = [
            (name, target) for name, target in self.engine.cfg.targets.items()
            if target.enabled and target.address.lower() == fill.maker.lower()
        ]
        if not targets:
            return

        meta = None
        for attempt in range(3):
            meta = self.resolver.resolve(fill.token_id)
            if meta is not None:
                break
            time.sleep(0.5 * (attempt + 1))
        if meta is None:
            self._failed += 1
            self.log.error("metadata resolution failed tx=%s token=%s",
                           fill.tx_hash, fill.token_id)
            self._write_trace(fill, None, None, None, started)
            return

        row = fill.to_row(meta, source="onchain")
        for name, target in targets:
            target_started = time.time()
            self.engine.ingest_rows(name, target, [row], source="onchain")
            self.store.commit()
            signal = self.store.signal_for(target.address, rules.dedupe_key(row))
            self._write_trace(fill, meta, signal, name, target_started)
            if signal is not None:
                self.log.info(
                    "CHAIN %-8s %s px=%.4f sh=%.2f $=%.2f copied=%s%s "
                    "latency=%.2fs %s",
                    name, fill.side, fill.price, fill.shares, fill.net_usd,
                    bool(signal.get("copied")),
                    "" if signal.get("copied") else f" ({signal.get('reason')})",
                    signal.get("latency_seconds") or 0.0,
                    (meta.title or fill.token_id)[:50],
                )
        self._processed += 1
        self._last_maint = 0.0  # run maintenance soon after a fill

    def _write_trace(self, fill: OrderFill, meta: TokenMeta | None,
                     signal: dict[str, Any] | None, target: str | None,
                     started: float) -> None:
        if self._trace is None:
            return
        record = {
            "target": target,
            "block": fill.block_number,
            "block_ts": fill.block_ts,
            "recv_at": round(fill.received_at, 6),
            "feed_delay_s": (round(fill.received_at - fill.block_ts, 6)
                             if fill.block_ts else None),
            "queue_delay_s": round(started - fill.received_at, 6),
            "handle_s": round(time.time() - started, 6),
            "tx": fill.tx_hash,
            "log_index": fill.log_index,
            "maker": fill.maker,
            "side": fill.side,
            "shares": fill.shares,
            "price": fill.price,
            "gross_usd": fill.gross_usd,
            "fee_usd": fill.fee_usd,
            "net_usd": fill.net_usd,
            "token_id": fill.token_id,
            "meta": meta.as_json() if meta else None,
            "signal": None if signal is None else {
                key: signal.get(key) for key in (
                    "copied", "reason", "latency_seconds", "our_shares",
                    "our_price", "our_notional", "our_fee",
                )
            },
        }
        self._trace.write(json.dumps(record, ensure_ascii=False,
                                     separators=(",", ":")) + "\n")
        self._trace.flush()

    # -------------------------------------------------------------- housekeeping
    def _persist_cursor(self) -> None:
        """Persist the feed block only once it has been stable and drained.

        A log can be decoded and queued between ``queue.empty()`` and the kv
        write; waiting for one more maintenance pass avoids advancing the
        cursor past a fill that was never processed.  On a crash the cursor is
        therefore a couple of blocks behind, which replay + dedupe handles.
        """
        block = self.feed.last_block
        if block is None or not self.feed.queue.empty():
            self._cursor_pending = None
            return
        if self._cursor_pending != block:
            self._cursor_pending = block
            return
        self.store.set_kv(CURSOR_KEY, str(block))
        self._cursor_pending = None

    def _health(self) -> None:
        stats = self.feed.stats()
        self.log.info(
            "onchain heartbeat connected=%s block=%s queue=%s processed=%d "
            "failed=%d errors=%d reconnects=%d",
            stats["connected"], stats["last_block"], stats["queue"],
            self._processed, self._failed, stats["errors"],
            stats["reconnects"],
        )


def _strip_mode_args(args: list[str]) -> list[str]:
    """Remove --mode/--mode=... so a re-exec follows [run].mode in config."""
    cleaned: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--mode":
            index += 2
            continue
        if arg.startswith("--mode="):
            index += 1
            continue
        cleaned.append(arg)
        index += 1
    return cleaned


def build_and_run(engine: Engine, settings: OnchainSettings, *,
                  logger: logging.Logger | None = None) -> int:
    """Create the RPC client, feed and resolver, then run the paper loop."""
    log = logger or logging.getLogger("polycopy.onchain")
    cfg = engine.cfg
    targets = [(name, target) for name, target in cfg.targets.items()
               if target.enabled]
    if not targets:
        raise ValueError("no enabled targets")
    wallets = [target.address for _, target in targets]

    rpc = RpcClient(settings.rpc_urls, logger=logging.getLogger(
        "polycopy.onchain.rpc"))
    store = engine.store
    cursor = int(store.get_kv(CURSOR_KEY, "0") or 0)
    if cursor > 0:
        start_block = cursor
    else:
        head = rpc.block_number()
        if settings.lookback_seconds > 0:
            back = lookback_blocks(settings.lookback_seconds)
        else:
            # Subscribe first, then catch up; include the current block so a
            # fill mined while the subscription was being set up is not lost.
            back = 1
        start_block = max(0, head - back - 1)
        store.set_kv(CURSOR_KEY, str(start_block))
        store.commit()

    include_event_slug = any(target.exclude_event_substrings
                             for _, target in targets)
    resolver = TokenResolver(
        engine.clob, store=store, gamma=engine.gamma,
        include_event_slug=include_event_slug,
        logger=logging.getLogger("polycopy.onchain.meta"),
    )
    # Gamma is passed unconditionally so the resolver can resolve the market
    # end date even when no event-slug exclusion is configured.
    feed = OrderFilledFeed(
        rpc, wallets,
        ws_urls=settings.ws_urls,
        addresses=settings.exchanges,
        start_block=start_block,
        catchup_chunk=settings.catchup_chunk,
        poll_fallback_seconds=settings.poll_fallback_seconds,
        logger=logging.getLogger("polycopy.onchain.feed"),
    )
    log.info(
        "on-chain source: wallets=%s rpc=%s ws=%s start_block=%s lookback=%ss"
        " mode=%s dry_run=%s",
        wallets, settings.rpc_urls, settings.ws_urls or "polling",
        start_block, settings.lookback_seconds,
        "live" if engine.executor.is_live else "paper", cfg.live.dry_run,
    )
    return OnchainRunner(engine, feed, resolver, settings, logger=log).run()
