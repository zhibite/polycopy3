import os
import tempfile
import time
import unittest

from polycopy.config import (
    AccountConfig, AppConfig, LiveConfig, PaperConfig, TargetConfig,
)
from polycopy.engine import Engine
from polycopy.execution import LiveExecutor
from polycopy.live_client import LiveOrderResult
from polycopy.store import Store


class FakeClob:
    def __init__(self, bid=0.49, ask=0.51, mid=0.50):
        self.bid = bid
        self.ask = ask
        self.mid = mid
        self.mid_calls = 0

    def book(self, token):
        return {
            "bids": [{"price": str(self.bid), "size": "1000"}],
            "asks": [{"price": str(self.ask), "size": "1000"}],
            "tick_size": "0.001",
        }

    def midpoints(self, tokens):
        self.mid_calls += 1
        return {token: self.mid for token in tokens}


class FakeData:
    def open_positions(self, *args, **kwargs):
        return None


class FakeGamma:
    def market_by_condition(self, condition_id):
        return {
            "closed": False,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["t1", "t2"]',
        }


class MakerGateway:
    """Fake SDK gateway with GTC, FAK and cancel state."""

    def __init__(self):
        self.fill_shares = 0.0
        self.avg_price = 0.5
        self.canceled = False
        self.sell_price = 0.55
        self.tail_fill = 0.0
        self.tail_price = 0.0
        self.sell_size = 0.0
        self.placed = []
        self.cancels = []

    def tick_size(self, token):
        return 0.001

    def neg_risk(self, token):
        return False

    def balance(self):
        return 1000.0

    def redeem(self, condition_id):
        return True

    def place_limit_buy(self, *, token_id, price, size, fee_rate,
                        on_prepared=None):
        if on_prepared:
            on_prepared("client-maker", {"side": "BUY"})
        self.placed.append(("maker", token_id, price, size))
        return LiveOrderResult(status="submitted", order_id="ex-maker",
                               remote_status="live")

    def place_fak_buy(self, *, token_id, price, size, fee_rate,
                      on_prepared=None):
        if on_prepared:
            on_prepared("client-tail", {"side": "BUY"})
        self.tail_fill = size
        self.tail_price = price
        self.placed.append(("tail", token_id, price, size))
        return LiveOrderResult(
            filled_size=size, avg_price=price, fee=0.0, status="filled",
            order_id="ex-tail", remote_status="matched")

    def place_fak_sell(self, *, token_id, price, size, fee_rate,
                       on_prepared=None):
        if on_prepared:
            on_prepared("client-sell", {"side": "SELL"})
        self.sell_size = size
        self.placed.append(("sell", token_id, price, size))
        return LiveOrderResult(
            filled_size=size, avg_price=self.sell_price, fee=0.0,
            status="filled", order_id="ex-sell", remote_status="matched")

    def get_order(self, order_id):
        if order_id == "client-tail":
            return {"status": "matched", "id": "tail"}
        if order_id == "client-sell":
            return {"status": "matched", "id": "sell"}
        status = "live"
        if self.canceled:
            status = "canceled"
        elif self.fill_shares > 0:
            status = "matched"
        return {"status": status, "id": "maker"}

    def parse_order(self, response, *, side, fee_rate):
        ident = response.get("id")
        if ident == "tail":
            return LiveOrderResult(
                filled_size=self.tail_fill, avg_price=self.tail_price,
                fee=0.0, status="filled", order_id="ex-tail",
                remote_status="matched")
        if ident == "sell":
            return LiveOrderResult(
                filled_size=self.sell_size, avg_price=self.sell_price,
                fee=0.0, status="filled", order_id="ex-sell",
                remote_status="matched")
        if self.canceled:
            return LiveOrderResult(
                filled_size=self.fill_shares, avg_price=self.avg_price,
                fee=0.0, status="canceled", order_id="ex-maker",
                remote_status="canceled", canceled=True)
        if self.fill_shares > 0:
            return LiveOrderResult(
                filled_size=self.fill_shares, avg_price=self.avg_price,
                fee=0.0,
                status="filled" if self.fill_shares >= 20 else "partial",
                order_id="ex-maker", remote_status="matched")
        return LiveOrderResult(
            status="submitted", remote_status="live", order_id="ex-maker")

    def cancel_order(self, order_id):
        self.canceled = True
        self.cancels.append(order_id)
        return {"canceled": [order_id]}


def trade_row(side, size, price, ts, tx):
    return {
        "type": "TRADE", "transaction_hash": tx, "condition_id": "c1",
        "token_id": "t1", "side": side, "price": price, "size": size,
        "usdc_size": size * price, "timestamp": ts, "outcome": "Yes",
        "title": "Test market", "slug": "test-market",
        "event_slug": "test-event",
    }


class MakerEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "live.db")
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.store.ensure_account(1000.0)
        live = LiveConfig(
            dry_run=False, funder="0x" + "11" * 20, signature_type=0,
            min_order_shares=1.0, max_order_usd=500.0,
            max_total_exposure_usd=1_000_000.0, max_daily_loss_usd=0.0,
            kill_switch_path=os.path.join(self.tmp.name, "HALT"),
            max_book_depth_pct=1.0,
        )
        target = TargetConfig(
            address="0xabc", enabled=True, sizing_mode="fixed",
            fixed_notional_usd=10.0, min_notional_usd=2.0,
            max_notional_usd=100.0, buy_price_min=0.30,
            buy_price_max=0.70, min_target_usd=1.0, max_market_usd=1000.0,
            max_open_positions=10, maker_entry_enabled=True,
            maker_timeout_seconds=300, fak_max_premium_pct=0.02,
            take_profit_pct=0.0, stop_loss_pct=0.0, min_hours_to_end=0.0,
        )
        self.cfg = AppConfig(
            account=AccountConfig(starting_cash=1000.0),
            paper=PaperConfig(
                db_path=self.db,
                log_path=os.path.join(self.tmp.name, "p.log"),
                settle_every_seconds=10 ** 9,
                mark_every_seconds=10 ** 9,
                min_shares=1.0, fee_rate=0.01,
            ),
            live=live,
            targets={"t": target},
        )
        self.clob = FakeClob()
        self.gateway = MakerGateway()
        self.executor = LiveExecutor(
            self.store, live, self.clob, gateway=self.gateway, dry_run=False)
        self.engine = Engine(
            self.cfg, self.store, FakeData(), self.clob, FakeGamma(),
            executor=self.executor,
        )

    def target(self):
        return self.cfg.targets["t"]

    def pending(self):
        rows = self.store.pending_buys(
            ("SUBMITTING", "PENDING", "CANCEL_REQUESTED",
             "TAIL_SUBMITTED"))
        self.assertTrue(rows)
        return rows[0]

    def test_maker_entry_partial_then_full(self):
        now = int(time.time())
        self.engine.ingest_rows(
            "t", self.target(),
            [trade_row("BUY", 1000, 0.50, now - 1, "0x1")], source="test")
        pending = self.pending()
        self.assertEqual(pending["status"], "PENDING")
        self.assertAlmostEqual(pending["shares"], 20.0)
        self.assertAlmostEqual(pending["limit_price"], 0.50)
        signal = self.store.signal_for("0xabc", pending["signal_key"])
        self.assertEqual(signal["reason"], "maker_pending")
        self.assertEqual(signal["copied"], 0)
        self.assertEqual(self.gateway.placed[0], ("maker", "t1", 0.5, 20.0))

        self.gateway.fill_shares = 5.0
        self.store.update_pending_buy(
            pending["id"], ts=int(time.time()) - 3)
        self.store.commit()
        self.engine.trading_tick()
        pending = self.store.pending_buy(pending["id"])
        position = self.store.position("0xabc", "c1", "t1")
        self.assertEqual(pending["status"], "PENDING")
        self.assertAlmostEqual(pending["filled_shares"], 5.0)
        self.assertAlmostEqual(position["shares"], 5.0)
        signal = self.store.signal_for("0xabc", pending["signal_key"])
        self.assertEqual(signal["copied"], 1)
        self.assertAlmostEqual(signal["our_shares"], 5.0)

        self.gateway.fill_shares = 20.0
        self.store.update_pending_buy(
            pending["id"], ts=int(time.time()) - 3)
        self.store.commit()
        self.engine.trading_tick()
        pending = self.store.pending_buy(pending["id"])
        position = self.store.position("0xabc", "c1", "t1")
        self.assertEqual(pending["status"], "DONE")
        self.assertEqual(pending["reason"], "filled")
        self.assertAlmostEqual(position["shares"], 20.0)
        signal = self.store.signal_for("0xabc", pending["signal_key"])
        self.assertEqual(signal["copied"], 1)
        self.assertAlmostEqual(signal["our_shares"], 20.0)

    def test_target_sell_cancels_pending_maker(self):
        now = int(time.time())
        self.engine.ingest_rows(
            "t", self.target(),
            [trade_row("BUY", 1000, 0.50, now - 1, "0x1")], source="test")
        pending = self.pending()
        self.engine.ingest_rows(
            "t", self.target(),
            [trade_row("SELL", 1000, 0.55, now, "0x2")], source="test")
        pending = self.store.pending_buy(pending["id"])
        self.assertEqual(pending["status"], "CANCEL_REQUESTED")
        self.assertEqual(pending["reason"], "target_sell")
        self.assertIn("client-maker", self.gateway.cancels)

        self.engine.trading_tick()
        pending = self.store.pending_buy(pending["id"])
        self.assertEqual(pending["status"], "DONE")
        self.assertEqual(pending["reason"], "target_sell")
        signal = self.store.signal_for("0xabc", pending["signal_key"])
        self.assertEqual(signal["copied"], 0)
        self.assertEqual(signal["reason"], "target_sell")

    def test_timeout_tails_at_cap(self):
        now = int(time.time())
        self.engine.ingest_rows(
            "t", self.target(),
            [trade_row("BUY", 1000, 0.50, now - 1, "0x1")], source="test")
        pending = self.pending()
        self.store.update_pending_buy(
            pending["id"], deadline_ts=now - 1)
        self.store.commit()
        self.engine.trading_tick()
        pending = self.store.pending_buy(pending["id"])
        self.assertEqual(pending["status"], "DONE")
        self.assertEqual(pending["reason"], "filled")
        self.assertAlmostEqual(pending["filled_shares"], 20.0)
        tails = [p for p in self.gateway.placed if p[0] == "tail"]
        self.assertEqual(len(tails), 1)
        _, _token, price, size = tails[0]
        self.assertAlmostEqual(price, 0.51)
        self.assertAlmostEqual(size, 20.0)

    def test_take_profit_exit(self):
        now = int(time.time())
        self.target().take_profit_pct = 0.10
        self.target().stop_loss_pct = 0.20
        self.store.apply_buy(
            ts=now, target="0xabc", signal_id=None, condition_id="c1",
            token_id="t1", outcome="Yes", title="Test", event_slug="event",
            shares=20.0, price=0.50, fee=0.0,
        )
        self.store.commit()
        self.clob.mid = 0.56
        self.engine._last_risk_check = 0
        exited = self.engine.check_risk_exits()
        self.assertEqual(exited, 1)
        position = self.store.position("0xabc", "c1", "t1")
        self.assertEqual(position["status"], "CLOSED")
        fill = self.store.recent_fills(1)[0]
        self.assertEqual(fill["kind"], "RISK")
        self.assertEqual(fill["note"], "take_profit")
        self.assertAlmostEqual(fill["price"], 0.55)

    def test_stop_loss_exit(self):
        now = int(time.time())
        self.target().take_profit_pct = 0.10
        self.target().stop_loss_pct = 0.20
        self.gateway.sell_price = 0.39
        self.store.apply_buy(
            ts=now, target="0xabc", signal_id=None, condition_id="c1",
            token_id="t1", outcome="Yes", title="Test", event_slug="event",
            shares=20.0, price=0.50, fee=0.0,
        )
        self.store.commit()
        self.clob.mid = 0.39
        self.engine._last_risk_check = 0
        exited = self.engine.check_risk_exits()
        self.assertEqual(exited, 1)
        fill = self.store.recent_fills(1)[0]
        self.assertEqual(fill["kind"], "RISK")
        self.assertEqual(fill["note"], "stop_loss")
        self.assertAlmostEqual(fill["price"], 0.39)

    def test_risk_check_skips_when_disabled(self):
        self.store.apply_buy(
            ts=int(time.time()), target="0xabc", signal_id=None,
            condition_id="c1", token_id="t1", outcome="Yes", title="Test",
            event_slug="event", shares=20.0, price=0.50, fee=0.0,
        )
        self.store.commit()
        self.engine._last_risk_check = 0
        self.assertEqual(self.engine.check_risk_exits(), 0)
        self.assertEqual(self.clob.mid_calls, 0)

    def test_orphan_grace_protects_new_position(self):
        self.store.apply_buy(
            ts=int(time.time()), target="0xabc", signal_id=None,
            condition_id="c1", token_id="t1", outcome="Yes", title="Test",
            event_slug="event", shares=20.0, price=0.50, fee=0.0,
        )
        self.store.commit()
        self.cfg.paper.orphan_grace_seconds = 120.0
        self.assertEqual(self.engine.reconcile_orphan_positions(), 0)
        self.cfg.paper.orphan_grace_seconds = 0.0
        self.assertEqual(self.engine.reconcile_orphan_positions(), 1)


if __name__ == "__main__":
    unittest.main()
