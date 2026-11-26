import os
import tempfile
import unittest
from pathlib import Path

from polycopy.store import Store
from polycopy.web import queries


class WebQueriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "paper.db"
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.store.ensure_account(1000.0)
        self._old_db = queries.DB_PATH
        queries.DB_PATH = self.db
        self.addCleanup(lambda: setattr(queries, "DB_PATH", self._old_db))

    def _buy(self, ts=1000):
        self.store.apply_buy(
            ts=ts, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", title="M", event_slug="e", shares=10, price=0.5,
            fee=0.01,
        )

    def test_summary(self):
        self._buy()
        self.store.set_mark("k", 0.49, 0.51, 0.50, 1000)
        self.store.record_signal(
            target="t", dedupe_key="x", ts=1000, latency_seconds=1.5,
            side="BUY", condition_id="c", token_id="k", outcome="Yes",
            price=0.5, size=10, usd=5.0, title="M", event_slug="e",
            copied=True, reason=None, our_shares=10, our_price=0.5,
            our_notional=5.0, our_fee=0.01,
        )
        self.store.commit()

        conn = queries.connect()
        self.addCleanup(queries.close, conn)
        summary = queries.summary(conn)
        self.assertAlmostEqual(summary["cash"], 1000.0 - 5.01)
        self.assertAlmostEqual(summary["market_value"], 5.0)
        self.assertAlmostEqual(summary["equity"], 999.99)
        self.assertEqual(summary["open_positions"], 1)
        self.assertEqual(summary["signals_total"], 1)
        self.assertEqual(summary["signals_copied"], 1)

    def test_pages_include_position_title(self):
        self._buy()
        self.store.commit()
        conn = queries.connect()
        self.addCleanup(queries.close, conn)

        fills = queries.fills_page(conn, page=1, per_page=50)
        self.assertEqual(fills["total"], 1)
        self.assertEqual(fills["rows"][0]["title"], "M")
        self.assertAlmostEqual(fills["sums"]["buy_notional"], 5.0)

        positions = queries.positions_page(conn, status="open", page=1)
        self.assertEqual(positions["total"], 1)
        self.assertEqual(positions["rows"][0]["title"], "M")
        self.assertEqual(positions["status"], "open")

    def test_queue_manual_sell(self):
        self._buy()
        self.store.commit()
        pos = self.store.position("t", "c", "k")
        request_id, error = queries.queue_manual_sell(pos["id"], 0.25)
        self.assertIsNone(error)
        self.assertIsNotNone(request_id)
        req = self.store.manual_sell(request_id)
        self.assertEqual(req["status"], "PENDING")
        self.assertAlmostEqual(req["shares_requested"], 2.5)
        self.assertAlmostEqual(req["remaining_shares"], 2.5)
        self.assertEqual(self.store.pending_manual_sells()[0]["id"], request_id)

    def test_signals_page_joins_market_slug(self):
        self.store.insert_target_fill(
            "d1", "t",
            {"type": "TRADE", "timestamp": 1, "condition_id": "c",
             "token_id": "k", "side": "BUY", "price": 0.5, "size": 10,
             "slug": "test-market", "event_slug": ""},
        )
        self.store.record_signal(
            target="t", dedupe_key="d1", ts=1, latency_seconds=1.0,
            side="BUY", condition_id="c", token_id="k", outcome="Yes",
            price=0.5, size=10, usd=5.0, title="M", event_slug="",
            copied=False, reason="price_out_of_range", slug="test-market",
        )
        self.store.commit()
        conn = queries.connect()
        self.addCleanup(queries.close, conn)
        page = queries.signals_page(conn, page=1, per_page=20)
        self.assertEqual(page["rows"][0]["market_slug"], "test-market")
        self.assertEqual(page["rows"][0]["slug"], "test-market")
        self.assertAlmostEqual(page["rows"][0]["target_avg_buy"], 0.5)

    def test_signals_page_target_avg_buy(self):
        for key, price in (("a", 0.4), ("b", 0.6)):
            self.store.insert_target_fill(
                key, "t",
                {"type": "TRADE", "timestamp": 10, "condition_id": "c",
                 "token_id": "k", "side": "BUY", "price": price, "size": 10},
            )
        self.store.record_signal(
            target="t", dedupe_key="s", ts=11, latency_seconds=1.0,
            side="SELL", condition_id="c", token_id="k", outcome="Yes",
            price=0.55, size=10, usd=5.5, title="M", event_slug="",
            copied=False, reason="no_position", slug="",
        )
        self.store.commit()
        conn = queries.connect()
        self.addCleanup(queries.close, conn)
        page = queries.signals_page(conn, page=1, per_page=20)
        self.assertAlmostEqual(page["rows"][0]["target_avg_buy"], 0.5)

    def test_request_restart_writes_kv(self):
        error = queries.request_restart()
        self.assertIsNone(error)
        self.assertTrue(self.store.get_kv("bot:restart_request"))

    def test_fill_type_filter(self):
        self._buy()
        self.store.apply_sell(
            ts=1001, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", shares=2, price=0.6, fee=0.0,
            manual=True, kind="MANUAL",
        )
        self.store.commit()
        conn = queries.connect()
        self.addCleanup(queries.close, conn)
        self.assertEqual(queries.fills_page(conn, fill_type="copy_buy")["total"], 1)
        self.assertEqual(queries.fills_page(conn, fill_type="copy_sell")["total"], 0)
        self.assertEqual(queries.fills_page(conn, fill_type="manual_sell")["total"], 1)
        manual = queries.fills_page(conn, fill_type="manual_sell")["rows"][0]
        self.assertEqual(manual["kind"], "MANUAL")

    def test_signal_summary(self):
        self.store.record_signal(
            target="t", dedupe_key="a", ts=1, latency_seconds=1.0, side="BUY",
            condition_id="c", token_id="k", outcome="Yes", price=0.5, size=10,
            usd=5.0, title="M", event_slug="e", copied=True, reason=None,
        )
        self.store.record_signal(
            target="t", dedupe_key="b", ts=2, latency_seconds=1.0, side="BUY",
            condition_id="c", token_id="k", outcome="Yes", price=0.8, size=10,
            usd=8.0, title="M", event_slug="e", copied=False,
            reason="price_out_of_range",
        )
        self.store.commit()
        conn = queries.connect()
        self.addCleanup(queries.close, conn)
        summary = {r["reason"]: r for r in queries.signal_summary(conn)}
        self.assertEqual(summary[None]["n"], 1)
        self.assertEqual(summary["price_out_of_range"]["n"], 1)

    def test_target_filters_and_risk_fill_type(self):
        self.store.apply_buy(
            ts=1, target="t1", signal_id=None, condition_id="c1",
            token_id="k1", outcome="Yes", title="M1", event_slug="e",
            shares=10, price=0.5, fee=0.0)
        self.store.apply_buy(
            ts=2, target="t2", signal_id=None, condition_id="c2",
            token_id="k2", outcome="Yes", title="M2", event_slug="e",
            shares=10, price=0.5, fee=0.0)
        self.store.apply_sell(
            ts=3, target="t1", signal_id=None, condition_id="c1",
            token_id="k1", outcome="Yes", shares=10, price=0.6, fee=0.0,
            note="take_profit", kind="RISK")
        for target, key in (("t1", "s1"), ("t2", "s2")):
            self.store.record_signal(
                target=target, dedupe_key=key, ts=4,
                latency_seconds=1.0, side="BUY", condition_id="c",
                token_id="k", outcome="Yes", price=0.5, size=10, usd=5.0,
                title="M", event_slug="e", copied=False,
                reason="maker_pending",
            )
        self.store.commit()
        conn = queries.connect()
        self.addCleanup(queries.close, conn)
        self.assertEqual(
            queries.positions_page(conn, status="all", target="t1")["total"], 1)
        self.assertEqual(
            queries.positions_page(conn, status="all", target="t2")["total"], 1)
        self.assertEqual(
            queries.signals_page(conn, target="t1")["total"], 1)
        self.assertEqual(
            queries.signals_page(conn, target="t2")["total"], 1)
        self.assertEqual(
            queries.fills_page(conn, fill_type="risk")["total"], 1)
        self.assertEqual(
            queries.fills_page(conn, fill_type="copy_sell")["total"], 0)
        self.assertEqual(
            queries.target_blocking_work(conn, "t2"), "未平仓持仓")
        self.assertIsNone(queries.target_blocking_work(conn, "t1"))

    def test_active_pending_buys(self):
        self.store.create_pending_buy(
            signal_key="s1", target="t1", condition_id="c1",
            token_id="k1", outcome="Yes", title="M", event_slug="e",
            slug="", signal_price=0.5, limit_price=0.5, fee_rate=0.0,
            shares=10, target_shares_at_entry=0, end_ts=0,
            deadline_ts=9999999999, signal_ts=1, source="test",
        )
        self.store.commit()
        conn = queries.connect()
        self.addCleanup(queries.close, conn)
        rows = queries.active_pending_buys(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "t1")


if __name__ == "__main__":
    unittest.main()
