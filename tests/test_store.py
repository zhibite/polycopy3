import os
import tempfile
import unittest

from polycopy.store import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "paper.db"))
        self.store.ensure_account(1000.0)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_buy_sell_settle(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", title="m", event_slug="e", shares=10, price=0.5,
            fee=0.025,
        )
        acc = self.store.account()
        self.assertAlmostEqual(acc["cash"], 1000 - 5.025)
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["shares"], 10)
        self.assertAlmostEqual(pos["avg_cost"], 0.5025)

        result = self.store.apply_sell(
            ts=2, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", shares=4, price=0.6, fee=0.0096,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["realized"], 2.3904 - 4 * 0.5025)
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["shares"], 6)

        realized = self.store.settle_position(pos, 1.0, ts=3)
        self.assertAlmostEqual(realized, 6 - 6 * 0.5025)
        closed = self.store.position("t", "c", "k")
        self.assertEqual(closed["status"], "CLOSED")
        self.assertAlmostEqual(closed["shares"], 0.0)
        acc = self.store.account()
        self.assertAlmostEqual(acc["cash"], 1000 - 5.025 + 2.3904 + 6.0)
        self.assertAlmostEqual(acc["realized_pnl"], 2.3904 - 4 * 0.5025 + 6 - 6 * 0.5025)
        self.assertEqual(self.store.count_open_positions(), 0)

    def test_set_cash_reconciles_balance(self):
        self.store.set_cash(499.190567)
        self.store.commit()
        self.assertAlmostEqual(self.store.account()["cash"], 499.190567)

    def test_target_ledger(self):
        self.store.add_target_shares("t", "c", "k", 100)
        self.assertAlmostEqual(self.store.target_shares("t", "k"), 100)
        self.store.add_target_shares("t", "c", "k", -40)
        self.assertAlmostEqual(self.store.target_shares("t", "k"), 60)
        self.store.add_target_shares("t", "c", "k", -100)
        self.assertAlmostEqual(self.store.target_shares("t", "k"), 0)

    def test_pending_exit_caps_and_clears(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", title="m", event_slug="e", shares=10, price=0.5,
            fee=0.0,
        )
        self.store.queue_pending_exit(
            target="t", condition_id="c", token_id="k", outcome="Yes",
            shares=4, reason="target_flat", ts=2,
        )
        self.assertAlmostEqual(
            self.store.pending_exit("t", "c", "k")["shares"], 4)
        # A second target exit is additive but capped at the held shares.
        self.store.queue_pending_exit(
            target="t", condition_id="c", token_id="k", outcome="Yes",
            shares=8, reason="partial_exit", ts=3,
        )
        self.assertAlmostEqual(
            self.store.pending_exit("t", "c", "k")["shares"], 10)

        result = self.store.apply_sell(
            ts=4, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", shares=10, price=0.4, fee=0.01, note="strict_exit",
        )
        self.assertIsNotNone(result)
        self.assertIsNone(self.store.pending_exit("t", "c", "k"))
        self.assertEqual(self.store.recent_fills(1)[0]["note"], "strict_exit")

    def test_replace_target_positions(self):
        self.store.add_target_shares("t", "c1", "k1", 100)
        self.store.add_target_shares("t", "c2", "k2", 50)
        updated = self.store.replace_target_positions(
            "t", [("c1", "k1", 70.0), ("c3", "k3", 10.0)], ts=10,
        )
        self.assertEqual(updated, 2)
        self.assertAlmostEqual(self.store.target_shares("t", "k1"), 70.0)
        self.assertEqual(self.store.target_shares("t", "k2"), 0.0)
        self.assertAlmostEqual(self.store.target_shares("t", "k3"), 10.0)

    def test_market_exposure(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", title="m", event_slug="e", shares=10, price=0.5,
            fee=0.0,
        )
        self.assertAlmostEqual(self.store.market_exposure("c"), 5.0)

    def test_order_intent_is_idempotent(self):
        first = self.store.create_order(
            dedupe_key="d1", target="t", mode="live", side="BUY",
            condition_id="c", token_id="k", outcome="Yes",
            limit_price=0.5, size=10, fee_rate=0.01, ts=1,
        )
        second = self.store.create_order(
            dedupe_key="d1", target="t", mode="live", side="BUY",
            condition_id="c", token_id="k", outcome="Yes",
            limit_price=0.5, size=10, fee_rate=0.01, ts=2,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "INTENT")
        self.store.update_order(first["id"], status="FILLED",
                                filled_size=10, avg_price=0.5, ts=3)
        self.store.commit()
        order = self.store.order_for("d1", "BUY")
        self.assertEqual(order["status"], "FILLED")
        self.assertAlmostEqual(order["filled_size"], 10.0)
        self.assertAlmostEqual(order["fee_rate"], 0.01)

    def test_manual_sell_credit_survives_and_offsets_copy_sell(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", title="m", event_slug="e",
            shares=10, price=0.5, fee=0.0,
        )
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["basis_shares"], 10.0)
        self.assertAlmostEqual(pos["manual_credit"], 0.0)

        # Manual 20%: current shrinks, basis stays.
        self.store.apply_sell(
            ts=2, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", shares=2, price=0.5, fee=0.0,
            manual=True,
        )
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["shares"], 8.0)
        self.assertAlmostEqual(pos["basis_shares"], 10.0)
        self.assertAlmostEqual(pos["manual_credit"], 2.0)
        self.assertEqual(self.store.recent_fills(1)[0]["kind"], "MANUAL")

        # Target sells 50%: equivalent 5 shares, 2 covered by manual credit.
        self.store.apply_sell(
            ts=3, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", shares=3, price=0.5, fee=0.0,
            basis_reduction=5, credit_reduction=2,
        )
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["shares"], 5.0)
        self.assertAlmostEqual(pos["basis_shares"], 5.0)
        self.assertAlmostEqual(pos["manual_credit"], 0.0)

        self.store.apply_sell(
            ts=4, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", shares=5, price=0.5, fee=0.0,
            basis_reduction=5,
        )
        pos = self.store.position("t", "c", "k")
        self.assertEqual(pos["status"], "CLOSED")
        self.assertAlmostEqual(pos["basis_shares"], 0.0)
        self.assertAlmostEqual(pos["manual_credit"], 0.0)

    def test_fill_order_link_and_realized_pnl_since(self):
        self.store.apply_buy(
            ts=10, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", title="m", event_slug="e",
            shares=10, price=0.5, fee=0.0, order_id=7,
        )
        self.store.apply_sell(
            ts=20, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", shares=10, price=0.6, fee=0.0,
            order_id=8,
        )
        fills = self.store.recent_fills(2)
        self.assertEqual({f["order_id"] for f in fills}, {7, 8})
        self.assertAlmostEqual(self.store.realized_pnl_since(15), 1.0)

    def test_pending_buy_lifecycle_and_signal_update(self):
        self.store.record_signal(
            target="t", dedupe_key="d1", ts=1, latency_seconds=1.0,
            side="BUY", condition_id="c", token_id="k", outcome="Yes",
            price=0.5, size=100, usd=50.0, title="m", event_slug="e",
            copied=False, reason="maker_pending",
        )
        pid = self.store.create_pending_buy(
            signal_key="d1", target="t", condition_id="c", token_id="k",
            outcome="Yes", title="m", event_slug="e", slug="s",
            signal_price=0.5, limit_price=0.5, fee_rate=0.01, shares=20,
            target_shares_at_entry=100, end_ts=0, deadline_ts=100,
            signal_ts=1, source="onchain", ts=2,
        )
        self.assertGreater(pid, 0)
        row = self.store.pending_buy(pid)
        self.assertEqual(row["status"], "SUBMITTING")
        self.assertAlmostEqual(self.store.pending_buy_commitment(), 10.0)
        self.assertEqual(self.store.count_pending_buy_tokens(), 1)

        ok = self.store.transition_pending_buy(
            pid, from_statuses=("SUBMITTING",), to_status="PENDING",
            reason=None)
        self.assertTrue(ok)
        ok = self.store.transition_pending_buy(
            pid, from_statuses=("SUBMITTING",), to_status="PENDING",
            reason=None)
        self.assertFalse(ok)

        self.store.apply_buy(
            ts=3, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", title="m", event_slug="e",
            shares=5, price=0.5, fee=0.0, order_id=9,
        )
        shares, notional, fee = self.store.fill_totals_for_order(9)
        self.assertAlmostEqual(shares, 5.0)
        self.assertAlmostEqual(notional, 2.5)
        self.assertAlmostEqual(fee, 0.0)
        self.store.update_pending_buy(
            pid, filled_shares=shares, notional=notional, fee=fee)
        self.assertAlmostEqual(self.store.pending_buy_commitment(), 7.5)
        self.store.update_signal_result(
            "t", "d1", copied=True, reason=None, our_shares=5.0,
            our_price=0.5, our_notional=2.5, our_fee=0.0)
        signal = self.store.signal_for("t", "d1")
        self.assertEqual(signal["copied"], 1)
        self.assertAlmostEqual(signal["our_shares"], 5.0)

        changed = self.store.request_cancel_pending_buys(
            "t", "c", "k", "target_sell", ts=4, target_sold_fraction=0.5)
        self.assertEqual(changed, 1)
        row = self.store.pending_buy(pid)
        self.assertEqual(row["status"], "CANCEL_REQUESTED")
        self.assertAlmostEqual(row["target_sold_fraction"], 0.5)

    def test_target_scoped_dedupe_and_orders(self):
        base = {"type": "TRADE", "timestamp": 1, "condition_id": "c",
                "token_id": "k", "side": "BUY", "price": 0.5, "size": 10}
        self.assertTrue(self.store.insert_target_fill("same", "t1", base))
        self.assertTrue(self.store.insert_target_fill("same", "t2", base))
        self.assertFalse(self.store.insert_target_fill("same", "t1", base))
        first = self.store.create_order(
            dedupe_key="d", target="t1", mode="live", side="BUY",
            condition_id="c", token_id="k", outcome="Yes",
            limit_price=0.5, size=10)
        second = self.store.create_order(
            dedupe_key="d", target="t2", mode="live", side="BUY",
            condition_id="c", token_id="k", outcome="Yes",
            limit_price=0.5, size=10)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(
            self.store.order_for("d", "BUY", target="t1")["id"],
            first["id"])

    def test_per_target_limits(self):
        self.store.apply_buy(
            ts=1, target="t1", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", title="m", event_slug="e",
            shares=10, price=0.5, fee=0.0)
        self.store.apply_buy(
            ts=2, target="t2", signal_id=None, condition_id="c",
            token_id="k2", outcome="Yes", title="m", event_slug="e",
            shares=20, price=0.5, fee=0.0)
        self.assertEqual(self.store.count_open_positions("t1"), 1)
        self.assertEqual(self.store.count_open_positions("t2"), 1)
        self.assertEqual(self.store.count_open_positions(), 2)
        self.assertAlmostEqual(self.store.market_exposure("c", "t1"), 5.0)
        self.assertAlmostEqual(self.store.market_exposure("c", "t2"), 10.0)
        self.assertAlmostEqual(self.store.market_exposure("c"), 15.0)

    def test_pending_exit_kind(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", title="m", event_slug="e",
            shares=10, price=0.5, fee=0.0)
        self.store.queue_pending_exit(
            target="t", condition_id="c", token_id="k", outcome="Yes",
            shares=10, reason="take_profit", ts=2, kind="RISK")
        row = self.store.pending_exit("t", "c", "k")
        self.assertEqual(row["kind"], "RISK")
        self.assertEqual(row["reason"], "take_profit")


class LegacyMigrationTest(unittest.TestCase):
    def test_multi_target_keys_are_migrated(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE target_fills (
                dedupe_key TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                ts INTEGER NOT NULL,
                kind TEXT NOT NULL,
                condition_id TEXT NOT NULL DEFAULT '',
                token_id TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL DEFAULT 0,
                size REAL NOT NULL DEFAULT 0,
                usd REAL NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL DEFAULT '',
                event_slug TEXT NOT NULL DEFAULT '',
                slug TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                raw TEXT NOT NULL DEFAULT '',
                processed_at INTEGER NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL,
                target TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'live',
                side TEXT NOT NULL,
                condition_id TEXT NOT NULL DEFAULT '',
                token_id TEXT NOT NULL,
                outcome TEXT NOT NULL DEFAULT '',
                limit_price REAL NOT NULL DEFAULT 0,
                size REAL NOT NULL DEFAULT 0,
                filled_size REAL NOT NULL DEFAULT 0,
                avg_price REAL NOT NULL DEFAULT 0,
                fee REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                reason TEXT,
                client_order_id TEXT,
                exchange_order_id TEXT,
                raw TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(dedupe_key, side, mode)
            );
            INSERT INTO target_fills(dedupe_key, target, ts, kind, processed_at)
                VALUES ('d1', 't1', 1, 'TRADE', 1);
            INSERT INTO orders(dedupe_key, target, side, token_id, status,
                               created_at, updated_at)
                VALUES ('d1', 't1', 'BUY', 'k', 'INTENT', 1, 1);
            """
        )
        conn.commit()
        conn.close()

        store = Store(path)
        self.addCleanup(store.close)
        backup = path + ".pre-multitarget.bak"
        self.assertTrue(os.path.exists(backup))
        # Old rows preserved, and the same dedupe key now works per target.
        row = store.conn.execute(
            "SELECT target FROM target_fills WHERE dedupe_key='d1'"
        ).fetchone()
        self.assertEqual(row["target"], "t1")
        self.assertTrue(store.insert_target_fill(
            "d1", "t2", {"type": "TRADE", "timestamp": 2}))
        first = store.create_order(
            dedupe_key="d1", target="t1", mode="live", side="BUY",
            condition_id="c", token_id="k", outcome="Yes",
            limit_price=0.5, size=10)
        self.assertEqual(first["status"], "INTENT")
        second = store.create_order(
            dedupe_key="d1", target="t2", mode="live", side="BUY",
            condition_id="c", token_id="k", outcome="Yes",
            limit_price=0.5, size=10)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(store.get_kv("schema:multi_target_keys"), "1")


if __name__ == "__main__":
    unittest.main()
