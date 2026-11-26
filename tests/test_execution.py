import os
import tempfile
import threading
import time
import unittest

from polycopy.config import LiveConfig, PaperConfig
from polycopy.execution import (
    Fill, LiveExecutor, PaperExecutor, TradeIntent,
)
from polycopy.live_client import (
    LiveOrderResult, SdkClobGateway, _market_order_amount,
)
from polycopy.store import Store


class FakeClob:
    def __init__(self, bid=0.49, ask=0.50, tick=0.001):
        self.bid = bid
        self.ask = ask
        self.tick = tick

    def book(self, token):
        return {
            "bids": [{"price": str(self.bid), "size": "1000"}],
            "asks": [{"price": str(self.ask), "size": "1000"}],
            "tick_size": str(self.tick),
        }


class FakeGateway:
    def __init__(self, fill_ratio=1.0):
        self.fill_ratio = fill_ratio
        self.prepared = []
        self.orders = []

    def tick_size(self, token):
        return 0.001

    def neg_risk(self, token):
        return False

    def place_fak_buy(self, *, token_id, price, size, fee_rate,
                      on_prepared=None):
        if on_prepared:
            on_prepared("client-1", {"side": "BUY"})
        filled = size * self.fill_ratio
        self.orders.append(("BUY", token_id, price, size))
        return LiveOrderResult(
            filled_size=filled, avg_price=price,
            fee=filled * price * (1 - price) * fee_rate,
            status="filled" if self.fill_ratio >= 1 else "partial",
            order_id="exchange-1", raw={"status": "matched"},
        )

    def place_fak_sell(self, *, token_id, price, size, fee_rate,
                       on_prepared=None):
        if on_prepared:
            on_prepared("client-2", {"side": "SELL"})
        self.orders.append(("SELL", token_id, price, size))
        return LiveOrderResult(
            filled_size=size, avg_price=price,
            fee=size * price * (1 - price) * fee_rate,
            status="filled", order_id="exchange-2", raw={"status": "matched"},
        )

    def get_order(self, order_id):
        return None

    def parse_order(self, response, *, side, fee_rate):
        return LiveOrderResult(status="rejected", reason="not_used")

    def balance(self):
        return 1000.0

    def redeem(self, condition_id):
        return True


def make_store(testcase):
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    store = Store(os.path.join(tmp.name, "paper.db"))
    store.ensure_account(1000.0)
    testcase.addCleanup(store.close)
    return store, tmp.name


def intent(side="BUY", shares=10.0, price=0.50, dedupe="d1"):
    return TradeIntent(
        dedupe_key=dedupe, target="0xabc", condition_id="c1", token_id="t1",
        outcome="Yes", title="Test", event_slug="test", side=side,
        shares=shares, limit_price=price, signal_price=price,
        fee_rate=0.01, max_spend=1000.0, max_market=1000.0,
        min_notional=2.0,
    )


class MarketOrderPrecisionTest(unittest.TestCase):
    def test_buy_amount_is_usdc_with_two_decimals(self):
        # The CLOB rejected 25.89 * 0.374 = 9.68286 USDC (5 decimals).
        self.assertAlmostEqual(
            _market_order_amount("BUY", 0.374, 25.89), 9.68)

    def test_sell_amount_is_shares_with_two_decimals(self):
        self.assertAlmostEqual(
            _market_order_amount("SELL", 0.374, 25.897), 25.89)


class PaperExecutorTest(unittest.TestCase):
    def test_buy_and_strict_sell(self):
        cfg = PaperConfig(min_fill_ratio=0.5)
        executor = PaperExecutor(FakeClob(ask=0.50), cfg)
        result = executor.buy(intent(price=0.50))
        self.assertEqual(result.status, "filled")
        self.assertAlmostEqual(result.fills[0].shares, 10.0)
        self.assertAlmostEqual(result.fills[0].price, 0.50)
        self.assertAlmostEqual(result.fills[0].fee, 10 * 0.5 * 0.5 * 0.01)

        sell = executor.sell(TradeIntent(
            dedupe_key="d2", target="0xabc", condition_id="c1",
            token_id="t1", outcome="Yes", title="Test", event_slug="test",
            side="SELL", shares=4.0, limit_price=0.001, signal_price=0.60,
            fee_rate=0.01, strict=True,
        ))
        self.assertEqual(sell.status, "filled")
        self.assertAlmostEqual(sell.fills[0].price, 0.49)

    def test_no_book_is_no_fill(self):
        class NoBook:
            def book(self, token):
                return None

        executor = PaperExecutor(NoBook(), PaperConfig())
        result = executor.buy(intent())
        self.assertEqual(result.status, "no_fill")
        self.assertEqual(result.reason, "book_unavailable")


class LiveExecutorTest(unittest.TestCase):
    def live_cfg(self, tmp, **overrides):
        values = dict(
            dry_run=True, min_order_shares=1.0, max_order_usd=500.0,
            max_total_exposure_usd=1_000_000.0, max_daily_loss_usd=0.0,
            kill_switch_path=os.path.join(tmp, "LIVE_HALT"),
            max_book_depth_pct=1.0,
        )
        values.update(overrides)
        return LiveConfig(**values)

    def test_available_cash_uses_live_balance(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)
        executor = LiveExecutor(store, cfg, FakeClob(),
                                gateway=FakeGateway(), dry_run=False)
        self.assertAlmostEqual(executor.available_cash(9999.0), 1000.0)

    def test_available_cash_blocks_when_balance_unavailable(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)

        class NoBalance(FakeGateway):
            def balance(self):
                return None

        executor = LiveExecutor(store, cfg, FakeClob(),
                                gateway=NoBalance(), dry_run=False)
        self.assertEqual(executor.available_cash(9999.0), 0.0)

    def test_dry_run_records_order_but_does_not_submit(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp)
        executor = LiveExecutor(store, cfg, FakeClob(), gateway=None,
                                dry_run=True)
        result = executor.buy(intent())
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.fills, [])
        order = store.order_for("d1", "BUY")
        self.assertEqual(order["status"], "DRY_RUN")
        self.assertEqual(order["side"], "BUY")

    def test_gateway_fill_updates_order_ledger(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)
        gateway = FakeGateway()
        executor = LiveExecutor(store, cfg, FakeClob(), gateway=gateway,
                                dry_run=False)
        result = executor.buy(intent(price=0.50))
        self.assertEqual(result.status, "filled")
        self.assertAlmostEqual(result.filled_shares, 10.0)
        order = store.order_for("d1", "BUY")
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["client_order_id"], "client-1")
        self.assertEqual(order["exchange_order_id"], "exchange-1")
        self.assertAlmostEqual(order["filled_size"], 10.0)
        self.assertEqual(gateway.orders[0][0], "BUY")

    def test_partial_fill_is_recorded_as_partial(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)
        executor = LiveExecutor(store, cfg, FakeClob(),
                                gateway=FakeGateway(fill_ratio=0.4),
                                dry_run=False)
        result = executor.buy(intent())
        self.assertEqual(result.status, "partial")
        self.assertAlmostEqual(result.filled_shares, 4.0)
        self.assertEqual(store.order_for("d1", "BUY")["status"], "PARTIAL")

    def test_kill_switch_blocks_buys_not_sells(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)
        kill = cfg.kill_switch_path
        with open(kill, "w", encoding="utf-8") as fh:
            fh.write("halt")
        executor = LiveExecutor(store, cfg, FakeClob(),
                                gateway=FakeGateway(), dry_run=False)
        buy = executor.buy(intent())
        self.assertEqual(buy.reason, "kill_switch")
        self.assertEqual(store.orders(mode="live"), [])
        sell = executor.sell(intent(side="SELL"))
        self.assertEqual(sell.status, "filled")

    def test_max_order_usd_clamps_size(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False, max_order_usd=5.0)
        gateway = FakeGateway()
        executor = LiveExecutor(store, cfg, FakeClob(), gateway=gateway,
                                dry_run=False)
        result = executor.buy(intent(shares=100.0, price=0.50))
        self.assertAlmostEqual(result.filled_shares, 10.0)
        self.assertAlmostEqual(gateway.orders[0][3], 10.0)

    def test_daily_loss_limit_blocks_new_buys(self):
        store, tmp = make_store(self)
        store.apply_buy(
            ts=int(time.time()), target="0xabc", signal_id=None,
            condition_id="c1", token_id="t1", outcome="Yes", title="Test",
            event_slug="test", shares=10.0, price=0.50, fee=0.0,
        )
        store.apply_sell(
            ts=int(time.time()), target="0xabc", signal_id=None,
            condition_id="c1", token_id="t1", outcome="Yes", shares=10.0,
            price=0.10, fee=0.0,
        )
        cfg = self.live_cfg(tmp, dry_run=False, max_daily_loss_usd=1.0)
        executor = LiveExecutor(store, cfg, FakeClob(),
                                gateway=FakeGateway(), dry_run=False)
        result = executor.buy(intent(dedupe="d2"))
        self.assertEqual(result.reason, "daily_loss_limit")

    def test_reconcile_never_resubmits_an_unknown_prepared_order(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)
        store.create_order(
            dedupe_key="d1", target="0xabc", mode="live", side="BUY",
            condition_id="c1", token_id="t1", outcome="Yes",
            limit_price=0.5, size=10.0, status="PREPARED",
            client_order_id="client-1", fee_rate=0.01,
        )
        store.commit()
        gateway = FakeGateway()
        executor = LiveExecutor(store, cfg, FakeClob(), gateway=gateway,
                                dry_run=False)
        executor.reconcile()
        order = store.order_for("d1", "BUY")
        self.assertEqual(order["status"], "UNKNOWN")
        self.assertEqual(gateway.orders, [])

    def test_duplicate_signal_does_not_submit_twice(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)
        gateway = FakeGateway()
        executor = LiveExecutor(store, cfg, FakeClob(), gateway=gateway,
                                dry_run=False)
        self.assertEqual(executor.buy(intent()).status, "filled")
        second = executor.buy(intent())
        self.assertEqual(second.reason, "duplicate_order")
        self.assertEqual(len(gateway.orders), 1)

    def test_reconcile_repairs_crash_between_fill_and_local_apply(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)
        order = store.create_order(
            dedupe_key="d1", target="0xabc", mode="live", side="BUY",
            condition_id="c1", token_id="t1", outcome="Yes",
            title="Test", event_slug="test", limit_price=0.5, size=10.0,
            status="FILLED", client_order_id="client-9", fee_rate=0.01,
        )
        store.update_order(order["id"], filled_size=10.0, avg_price=0.5,
                           fee=0.025)
        store.commit()

        class ReconcileGateway(FakeGateway):
            def get_order(self, order_id):
                return {"status": "matched"}

            def parse_order(self, response, *, side, fee_rate):
                return LiveOrderResult(
                    filled_size=10.0, avg_price=0.5, fee=0.025,
                    status="filled", order_id="exchange-9", raw=response,
                )

        executor = LiveExecutor(store, cfg, FakeClob(),
                                gateway=ReconcileGateway(), dry_run=False)
        executor.reconcile()
        pos = store.position("0xabc", "c1", "t1")
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos["shares"], 10.0)
        self.assertEqual(store.fill_count_for_order(order["id"]), 1)
        # A second pass must not duplicate the local fill.
        executor.reconcile()
        self.assertEqual(store.fill_count_for_order(order["id"]), 1)

    def test_refresh_order_applies_cumulative_deltas_once(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)
        order = store.create_order(
            dedupe_key="d1", target="0xabc", mode="live", side="BUY",
            condition_id="c1", token_id="t1", outcome="Yes",
            title="Test", event_slug="test", limit_price=0.5, size=10.0,
            status="SUBMITTED", client_order_id="client-9", fee_rate=0.0,
        )
        store.commit()

        class RefreshGateway(FakeGateway):
            cum = 4.0

            def get_order(self, order_id):
                return {"id": "o1"}

            def parse_order(self, response, *, side, fee_rate):
                return LiveOrderResult(
                    filled_size=self.cum, avg_price=0.5, fee=0.0,
                    status="filled" if self.cum >= 10 else "partial",
                    order_id="exchange-9", raw=response,
                )

        gateway = RefreshGateway()
        executor = LiveExecutor(store, cfg, FakeClob(), gateway=gateway,
                                dry_run=False)
        executor.refresh_order(order["id"])
        executor.refresh_order(order["id"])
        pos = store.position("0xabc", "c1", "t1")
        self.assertAlmostEqual(pos["shares"], 4.0)
        self.assertAlmostEqual(
            store.fill_totals_for_order(order["id"])[0], 4.0)

        gateway.cum = 10.0
        executor.refresh_order(order["id"])
        pos = store.position("0xabc", "c1", "t1")
        self.assertAlmostEqual(pos["shares"], 10.0)
        self.assertAlmostEqual(
            store.fill_totals_for_order(order["id"])[0], 10.0)

    def test_refresh_order_can_use_another_thread_store(self):
        store, tmp = make_store(self)
        cfg = self.live_cfg(tmp, dry_run=False)
        executor = LiveExecutor(store, cfg, FakeClob(), gateway=FakeGateway(),
                                dry_run=False)
        errors = []

        def worker():
            worker_store = Store(store.path)
            try:
                result = executor.sell(intent(side="SELL"),
                                       store=worker_store)
                if result.status != "filled":
                    errors.append(f"status={result.status}")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                worker_store.close()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_parse_order_keeps_canceled_with_partial_fill(self):
        gateway = SdkClobGateway(LiveConfig())
        parsed = gateway.parse_order(
            {"status": "canceled", "size_matched": "5", "price": "0.5"},
            side="BUY", fee_rate=0.0,
        )
        self.assertTrue(parsed.canceled)
        self.assertEqual(parsed.status, "canceled")
        self.assertAlmostEqual(parsed.filled_size, 5.0)
        self.assertAlmostEqual(parsed.avg_price, 0.5)

    def test_gateway_places_gtc_and_cancels(self):
        class FakeClobClient:
            def __init__(self):
                self.args = None
                self.posts = []
                self.cancels = []

            def create_order(self, order_args, options=None):
                self.args = order_args
                return {"hash": "order-hash-1"}

            def post_order(self, order, order_type="GTC", post_only=False,
                           defer_exec=False):
                self.posts.append((order, order_type, post_only))
                return {"status": "live", "orderID": "exchange-1"}

            def cancel_order(self, payload):
                self.cancels.append(payload.orderID)
                return {"canceled": [payload.orderID]}

        client = FakeClobClient()
        gateway = SdkClobGateway(LiveConfig(dry_run=False), client=client)
        prepared = []
        result = gateway.place_limit_buy(
            token_id="t1", price=0.5, size=20, fee_rate=0.0,
            on_prepared=lambda cid, raw: prepared.append(cid),
        )
        self.assertEqual(prepared, ["order-hash-1"])
        self.assertEqual(result.status, "submitted")
        self.assertEqual(result.remote_status, "live")
        self.assertEqual(client.posts[0][1], "GTC")
        self.assertFalse(client.posts[0][2])
        self.assertAlmostEqual(client.args.price, 0.5)
        self.assertAlmostEqual(client.args.size, 20.0)
        self.assertEqual(
            gateway.cancel_order("exchange-1"),
            {"canceled": ["exchange-1"]})
        self.assertEqual(client.cancels, ["exchange-1"])


if __name__ == "__main__":
    unittest.main()
