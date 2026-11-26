import copy
import os
import tempfile
import time
import unittest
from pathlib import Path

from polycopy.config import AccountConfig, AppConfig, PaperConfig, TargetConfig
from polycopy.engine import Engine
from polycopy.execution import ExecutionResult, Fill, PaperExecutor
from polycopy.store import Store
from polycopy.web import config_io


class FakeData:
    def __init__(self, rows):
        self.rows = rows

    def activity(self, user, start_ts, limit=500, max_pages=3):
        return list(self.rows)

    def open_positions(self, user, limit=500, max_pages=10):
        # Disable ledger reconciliation unless a test opts in.
        return None


class FakeClob:
    def __init__(self, bid=0.59, ask=0.50):
        self.bid = bid
        self.ask = ask

    def book(self, token):
        return {
            "bids": [{"price": str(self.bid), "size": "1000"}],
            "asks": [{"price": str(self.ask), "size": "1000"}],
        }


class MutableClob(FakeClob):
    fail = False

    def book(self, token):
        if self.fail:
            return None
        return super().book(token)


class FakeGamma:
    def __init__(self, closed=False, yes_wins=True):
        self.closed = closed
        self.yes_wins = yes_wins

    def market_by_condition(self, cid):
        if self.closed:
            prices = '["1", "0"]' if self.yes_wins else '["0", "1"]'
            return {
                "closed": True,
                "outcomes": '["Yes", "No"]',
                "outcomePrices": prices,
                "clobTokenIds": '["t1", "t2"]',
            }
        return {
            "closed": False,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["t1", "t2"]',
        }


def trade_row(side, size, price, ts, tx="0x1"):
    return {
        "type": "TRADE",
        "transaction_hash": tx,
        "condition_id": "c1",
        "token_id": "t1",
        "side": side,
        "price": price,
        "size": size,
        "usdc_size": size * price,
        "timestamp": ts,
        "outcome": "Yes",
        "title": "Test market",
        "slug": "test-market",
        "event_slug": "test-event",
    }


class FakeExecutor:
    is_live = False
    min_shares = 1.0

    def __init__(self):
        self.buys = []
        self.sells = []

    def available_cash(self, fallback, store=None):
        return fallback

    def buy(self, intent, store=None):
        self.buys.append(intent)
        return ExecutionResult(
            fills=[Fill(intent.shares, intent.limit_price - 0.03, 0.0)],
            status="filled",
        )

    def sell(self, intent, store=None):
        self.sells.append(intent)
        return ExecutionResult(status="no_fill", reason="no_liquidity")

    def reconcile(self, store=None):
        return None

    def redeem(self, condition_id):
        return False


class EngineTest(unittest.TestCase):
    def make_engine(self, rows, gamma=None, clob=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "paper.db")
        cfg = AppConfig(
            account=AccountConfig(starting_cash=1000.0),
            paper=PaperConfig(
                db_path=db,
                log_path=os.path.join(tmp.name, "paper.log"),
                settle_every_seconds=10 ** 12,
                mark_every_seconds=10 ** 12,
                min_shares=1.0,
                fee_rate=0.01,
            ),
            targets={
                "t": TargetConfig(
                    address="0xabc",
                    scale_factor=0.02,
                    min_notional_usd=2.0,
                    max_notional_usd=100.0,
                    min_target_usd=20.0,
                    buy_price_min=0.30,
                    buy_price_max=0.70,
                    max_market_usd=1000.0,
                    max_open_positions=10,
                )
            },
        )
        store = Store(db)
        self.addCleanup(store.close)
        store.ensure_account(1000.0)
        engine = Engine(cfg, store, FakeData(rows), clob or FakeClob(),
                        gamma or FakeGamma())
        return engine, store

    def test_copy_buy_and_proportional_sell(self):
        now = int(time.time())
        rows = [
            trade_row("BUY", 100, 0.50, now - 5, tx="0x1"),
            trade_row("SELL", 40, 0.60, now - 4, tx="0x2"),
        ]
        engine, store = self.make_engine(rows)
        engine.cycle()

        pos = store.position("0xabc", "c1", "t1")
        self.assertEqual(pos["status"], "OPEN")
        # bought $2 of a $50 target fill -> 4 shares, then target sold 40% -> 1.6
        self.assertAlmostEqual(pos["shares"], 2.4)

        fee_sell = 1.6 * 0.59 * (1 - 0.59) * 0.01
        expected_cash = 1000.0 - 2.0 - 0.01 + 1.6 * 0.59 - fee_sell
        self.assertAlmostEqual(store.account()["cash"], expected_cash, places=6)

        total, copied = store.signal_counts()
        self.assertEqual(total, 2)
        self.assertEqual(copied, 2)

    def test_price_filter_records_skip(self):
        now = int(time.time())
        rows = [trade_row("BUY", 100, 0.80, now - 5)]
        engine, store = self.make_engine(rows)
        engine.cycle()
        self.assertEqual(store.count_open_positions(), 0)
        summary = store.signal_summary()
        self.assertEqual(summary[0]["reason"], "price_out_of_range")
        self.assertEqual(summary[0]["copied"], 0)

    def test_paper_ignores_maker_entry(self):
        now = int(time.time())
        engine, store = self.make_engine(
            [trade_row("BUY", 100, 0.50, now - 5, tx="0x1")])
        engine.cfg.targets["t"].maker_entry_enabled = True
        engine.cycle()
        self.assertEqual(store.pending_buys(), [])
        self.assertEqual(store.count_open_positions(), 1)
        self.assertEqual(store.signal_counts()[1], 1)

    def test_expiry_gate_skips_too_close_and_unknown(self):
        now = int(time.time())
        engine, store = self.make_engine([])
        target = engine.cfg.targets["t"]
        target.min_hours_to_end = 2.0
        engine.ingest_rows(
            "t", target,
            [dict(trade_row("BUY", 100, 0.50, now - 5, tx="0x1"),
                  end_ts=now + 3600)],
            source="test",
        )
        engine.ingest_rows(
            "t", target,
            [dict(trade_row("BUY", 100, 0.50, now - 4, tx="0x2"),
                  end_ts=now + 4 * 3600)],
            source="test",
        )
        engine.ingest_rows(
            "t", target,
            [trade_row("BUY", 100, 0.50, now - 3, tx="0x3")],
            source="test",
        )
        summary = {row["reason"]: row for row in store.signal_summary()}
        self.assertEqual(summary["expiry_too_close"]["n"], 1)
        self.assertEqual(summary["expiry_unknown"]["n"], 1)
        self.assertEqual(store.count_open_positions(), 1)

    def test_settlement_pays_winner(self):
        now = int(time.time())
        rows = [trade_row("BUY", 100, 0.50, now - 5)]
        engine, store = self.make_engine(rows, gamma=FakeGamma(closed=True))
        engine.cycle()
        settled = engine.settle()
        self.assertEqual(settled, 1)
        self.assertEqual(store.count_open_positions(), 0)
        self.assertAlmostEqual(store.account()["cash"], 1000.0 - 2.01 + 4.0)

    def test_settlement_loser_pays_zero(self):
        now = int(time.time())
        rows = [trade_row("BUY", 100, 0.50, now - 5)]
        engine, store = self.make_engine(rows, gamma=FakeGamma(closed=True, yes_wins=False))
        engine.cycle()
        engine.settle()
        self.assertEqual(store.count_open_positions(), 0)
        self.assertAlmostEqual(store.account()["cash"], 1000.0 - 2.01)

    def test_strict_sell_fills_below_floor(self):
        now = int(time.time())
        engine, store = self.make_engine(
            [trade_row("BUY", 100, 0.50, now - 5, tx="0x1")],
            clob=FakeClob(bid=0.40, ask=0.50),
        )
        engine.cycle()
        engine.data.rows = [trade_row("SELL", 40, 0.60, now - 4, tx="0x2")]
        engine.cycle()
        pos = store.position("0xabc", "c1", "t1")
        self.assertAlmostEqual(pos["shares"], 2.4)
        sell = store.recent_fills(1)[0]
        self.assertEqual(sell["side"], "SELL")
        self.assertAlmostEqual(sell["price"], 0.40)
        self.assertLess(sell["realized_pnl"], 0)

    def test_pending_exit_is_retried(self):
        now = int(time.time())
        clob = MutableClob(bid=0.40, ask=0.50)
        engine, store = self.make_engine(
            [trade_row("BUY", 100, 0.50, now - 5, tx="0x1")], clob=clob,
        )
        engine.cycle()
        engine.data.rows = [trade_row("SELL", 40, 0.60, now - 4, tx="0x2")]
        clob.fail = True
        engine.cycle()
        pending = store.pending_exit("0xabc", "c1", "t1")
        self.assertIsNotNone(pending)
        self.assertAlmostEqual(pending["shares"], 1.6)
        self.assertEqual(store.all_signals()[-1]["reason"], "exit_pending")

        clob.fail = False
        engine._last_exit_retry = 0
        engine.trading_tick()
        self.assertIsNone(store.pending_exit("0xabc", "c1", "t1"))
        self.assertAlmostEqual(
            store.position("0xabc", "c1", "t1")["shares"], 2.4)
        self.assertEqual(store.recent_fills(1)[0]["note"], "strict_exit")

    def test_orphan_dust_below_live_minimum_is_not_queued(self):
        engine, store = self.make_engine([])
        store.apply_buy(
            ts=int(time.time()), target="0xabc", signal_id=None,
            condition_id="c1", token_id="t1", outcome="Yes", title="Dust",
            event_slug="", shares=4.0, price=0.5, fee=0.0,
        )
        store.commit()
        engine.executor.is_live = True
        engine.executor.min_shares = 5.0
        queued = engine.reconcile_orphan_positions()
        self.assertEqual(queued, 0)
        self.assertIsNone(store.pending_exit("0xabc", "c1", "t1"))

    def test_target_reconciliation_closes_orphan(self):
        now = int(time.time())
        engine, store = self.make_engine(
            [trade_row("BUY", 100, 0.50, now - 5, tx="0x1")],
        )
        engine.cycle()
        self.assertAlmostEqual(
            store.position("0xabc", "c1", "t1")["shares"], 4.0)
        self.assertAlmostEqual(store.target_shares("0xabc", "t1"), 100.0)

        engine.data.open_positions = (
            lambda user, limit=500, max_pages=10: [])
        engine.reconcile_target_positions()
        self.assertEqual(store.target_shares("0xabc", "t1"), 0.0)

        engine.reconcile_orphan_positions()
        pending = store.pending_exit("0xabc", "c1", "t1")
        self.assertIsNotNone(pending)
        self.assertAlmostEqual(pending["shares"], 4.0)

        engine.retry_pending_exits()
        self.assertEqual(
            store.position("0xabc", "c1", "t1")["status"], "CLOSED")
        self.assertIsNone(store.pending_exit("0xabc", "c1", "t1"))

    def test_manual_sell_offsets_future_target_sells_and_fully_exits(self):
        now = int(time.time())
        engine, store = self.make_engine(
            [trade_row("BUY", 100, 0.50, now - 5, tx="0x1")])
        engine.cycle()
        pos = store.position("0xabc", "c1", "t1")
        self.assertAlmostEqual(pos["shares"], 4.0)
        self.assertAlmostEqual(pos["basis_shares"], 4.0)

        store.queue_manual_sell(
            target="0xabc", condition_id="c1", token_id="t1",
            pct=0.25, shares_requested=1.0,
        )
        store.commit()
        engine.process_manual_sells()
        pos = store.position("0xabc", "c1", "t1")
        self.assertAlmostEqual(pos["shares"], 3.0)
        self.assertAlmostEqual(pos["basis_shares"], 4.0)
        self.assertAlmostEqual(pos["manual_credit"], 1.0)

        # Target sells 50%: the manual 1-share credit covers part of it.
        engine.data.rows = [trade_row("SELL", 50, 0.60, now - 4, tx="0x2")]
        engine.cycle()
        pos = store.position("0xabc", "c1", "t1")
        self.assertAlmostEqual(pos["shares"], 2.0)
        self.assertAlmostEqual(pos["basis_shares"], 2.0)
        self.assertAlmostEqual(pos["manual_credit"], 0.0)

        # Target exits: all remaining shares must go.
        engine.data.rows = [trade_row("SELL", 50, 0.60, now - 3, tx="0x3")]
        engine.cycle()
        pos = store.position("0xabc", "c1", "t1")
        self.assertEqual(pos["status"], "CLOSED")
        self.assertAlmostEqual(pos["shares"], 0.0)

    def test_manual_credit_absorbed_target_sell_then_full_exit(self):
        now = int(time.time())
        engine, store = self.make_engine(
            [trade_row("BUY", 100, 0.50, now - 5, tx="0x1")])
        engine.cycle()
        store.queue_manual_sell(
            target="0xabc", condition_id="c1", token_id="t1",
            pct=0.5, shares_requested=2.0,
        )
        store.commit()
        engine.process_manual_sells()
        pos = store.position("0xabc", "c1", "t1")
        self.assertAlmostEqual(pos["shares"], 2.0)
        self.assertAlmostEqual(pos["basis_shares"], 4.0)
        self.assertAlmostEqual(pos["manual_credit"], 2.0)

        # Target sells 25%: equivalent 1 share, fully covered by credit.
        engine.data.rows = [trade_row("SELL", 25, 0.60, now - 4, tx="0x2")]
        engine.cycle()
        pos = store.position("0xabc", "c1", "t1")
        self.assertAlmostEqual(pos["shares"], 2.0)
        self.assertAlmostEqual(pos["basis_shares"], 3.0)
        self.assertAlmostEqual(pos["manual_credit"], 1.0)
        self.assertEqual(store.all_signals()[-1]["reason"],
                         "manual_credit_absorbed")

        # Target exits: all remaining shares go.
        engine.data.rows = [trade_row("SELL", 75, 0.60, now - 3, tx="0x3")]
        engine.cycle()
        pos = store.position("0xabc", "c1", "t1")
        self.assertEqual(pos["status"], "CLOSED")
        self.assertAlmostEqual(pos["shares"], 0.0)

    def test_hot_reload_updates_paper_executor_config(self):
        engine, store = self.make_engine([])
        self.assertIsInstance(engine.executor, PaperExecutor)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.toml"
        engine.cfg.path = path
        config_io.save_config(engine.cfg, path)
        engine._cfg_mtime = os.stat(path).st_mtime
        updated = copy.deepcopy(engine.cfg)
        updated.paper.min_fill_ratio = 0.75
        config_io.save_config(updated, path)
        stamp = os.stat(path).st_mtime + 2
        os.utime(path, (stamp, stamp))
        engine.maybe_reload_config()
        self.assertAlmostEqual(engine.executor.cfg.min_fill_ratio, 0.75)

    def test_injected_executor_is_used_for_buys(self):
        now = int(time.time())
        engine, store = self.make_engine(
            [trade_row("BUY", 100, 0.50, now - 5)])
        executor = FakeExecutor()
        engine.executor = executor
        engine.cycle()
        self.assertEqual(len(executor.buys), 1)
        self.assertEqual(executor.buys[0].side, "BUY")
        self.assertAlmostEqual(
            store.position("0xabc", "c1", "t1")["shares"], 4.0)

    def test_second_cycle_does_not_duplicate(self):
        now = int(time.time())
        rows = [trade_row("BUY", 100, 0.50, now - 5)]
        engine, store = self.make_engine(rows)
        engine.cycle()
        engine.cycle()
        total, copied = store.signal_counts()
        self.assertEqual(total, 1)
        self.assertEqual(copied, 1)
        self.assertAlmostEqual(store.position("0xabc", "c1", "t1")["shares"], 4.0)


if __name__ == "__main__":
    unittest.main()
