"""SQLite persistence for the paper trading account."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    starting_cash REAL NOT NULL,
    cash REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    fees_paid REAL NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS target_fills (
    dedupe_key TEXT NOT NULL,
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
    processed_at INTEGER NOT NULL,
    PRIMARY KEY (target, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_target_fills_target_ts ON target_fills(target, ts);

CREATE TABLE IF NOT EXISTS target_positions (
    target TEXT NOT NULL,
    condition_id TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL,
    shares REAL NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (target, token_id)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    ts INTEGER NOT NULL,
    latency_seconds REAL,
    side TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    price REAL,
    size REAL,
    usd REAL,
    title TEXT,
    event_slug TEXT,
    slug TEXT NOT NULL DEFAULT '',
    copied INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    our_shares REAL,
    our_price REAL,
    our_notional REAL,
    our_fee REAL,
    source TEXT NOT NULL DEFAULT 'data_api',
    created_at INTEGER NOT NULL,
    UNIQUE(target, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_copied ON signals(copied);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    title TEXT,
    event_slug TEXT,
    slug TEXT NOT NULL DEFAULT '',
    shares REAL NOT NULL DEFAULT 0,
    basis_shares REAL NOT NULL DEFAULT 0,
    manual_credit REAL NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    opened_at INTEGER,
    updated_at INTEGER,
    status TEXT NOT NULL DEFAULT 'OPEN',
    UNIQUE(target, condition_id, token_id)
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    target TEXT,
    signal_id INTEGER,
    order_id INTEGER,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    side TEXT NOT NULL,
    shares REAL NOT NULL,
    price REAL NOT NULL,
    notional REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'COPY',
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    target TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'live',
    side TEXT NOT NULL,
    condition_id TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    event_slug TEXT NOT NULL DEFAULT '',
    limit_price REAL NOT NULL DEFAULT 0,
    size REAL NOT NULL DEFAULT 0,
    filled_size REAL NOT NULL DEFAULT 0,
    avg_price REAL NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    fee_rate REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reason TEXT,
    client_order_id TEXT,
    exchange_order_id TEXT,
    raw TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(target, dedupe_key, side, mode)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_token ON orders(token_id);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    shares REAL NOT NULL,
    payout REAL NOT NULL,
    realized_pnl REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS marks (
    token_id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    bid REAL,
    ask REAL,
    mid REAL
);

CREATE TABLE IF NOT EXISTS pending_exits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    condition_id TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    shares REAL NOT NULL,
    reason TEXT,
    kind TEXT NOT NULL DEFAULT 'COPY',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(target, condition_id, token_id)
);
CREATE INDEX IF NOT EXISTS idx_pending_exits_target ON pending_exits(target);

CREATE TABLE IF NOT EXISTS pending_buys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key TEXT NOT NULL,
    target TEXT NOT NULL,
    condition_id TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    event_slug TEXT NOT NULL DEFAULT '',
    slug TEXT NOT NULL DEFAULT '',
    signal_price REAL NOT NULL DEFAULT 0,
    limit_price REAL NOT NULL DEFAULT 0,
    fee_rate REAL NOT NULL DEFAULT 0,
    shares REAL NOT NULL DEFAULT 0,
    target_shares_at_entry REAL NOT NULL DEFAULT 0,
    target_sold_fraction REAL NOT NULL DEFAULT 0,
    filled_shares REAL NOT NULL DEFAULT 0,
    notional REAL NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    end_ts INTEGER NOT NULL DEFAULT 0,
    order_id INTEGER,
    fak_order_id INTEGER,
    deadline_ts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'SUBMITTING',
    cancel_confirmed INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    signal_ts INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'onchain',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(target, signal_key)
);
CREATE INDEX IF NOT EXISTS idx_pending_buys_status ON pending_buys(status);
CREATE INDEX IF NOT EXISTS idx_pending_buys_token ON pending_buys(target, token_id);

CREATE TABLE IF NOT EXISTS manual_sells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    condition_id TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL,
    pct REAL NOT NULL,
    shares_requested REAL NOT NULL,
    remaining_shares REAL NOT NULL,
    filled_shares REAL NOT NULL DEFAULT 0,
    avg_price REAL NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_manual_sells_status ON manual_sells(status);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(fills)")}
        if "realized_pnl" not in cols:
            self.conn.execute(
                "ALTER TABLE fills ADD COLUMN realized_pnl REAL NOT NULL DEFAULT 0"
            )
        if "order_id" not in cols:
            self.conn.execute("ALTER TABLE fills ADD COLUMN order_id INTEGER")
        pos_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(positions)")
        }
        added_basis = False
        if "basis_shares" not in pos_cols:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN basis_shares REAL NOT NULL"
                " DEFAULT 0")
            added_basis = True
        if "manual_credit" not in pos_cols:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN manual_credit REAL NOT NULL"
                " DEFAULT 0")
        if added_basis:
            # Existing positions predate manual-sell accounting.
            self.conn.execute(
                "UPDATE positions SET basis_shares = shares"
                " WHERE basis_shares = 0 AND shares > 0")
        order_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(orders)")
        }
        if order_cols:
            if "title" not in order_cols:
                self.conn.execute(
                    "ALTER TABLE orders ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            if "event_slug" not in order_cols:
                self.conn.execute(
                    "ALTER TABLE orders ADD COLUMN event_slug TEXT NOT NULL"
                    " DEFAULT ''")
            if "fee_rate" not in order_cols:
                self.conn.execute(
                    "ALTER TABLE orders ADD COLUMN fee_rate REAL NOT NULL"
                    " DEFAULT 0")
        signal_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(signals)")
        }
        if "source" not in signal_cols:
            self.conn.execute(
                "ALTER TABLE signals ADD COLUMN source TEXT NOT NULL"
                " DEFAULT 'data_api'"
            )
        if "slug" not in signal_cols:
            self.conn.execute(
                "ALTER TABLE signals ADD COLUMN slug TEXT NOT NULL DEFAULT ''")
            self.conn.execute(
                "UPDATE signals SET slug = COALESCE(("
                " SELECT tf.slug FROM target_fills tf"
                " WHERE tf.dedupe_key = signals.dedupe_key"
                " AND tf.target = signals.target"
                " AND tf.slug != '' LIMIT 1), '')"
                " WHERE slug = ''")
        if "slug" not in pos_cols:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN slug TEXT NOT NULL DEFAULT ''")
            self.conn.execute(
                "UPDATE positions SET slug = COALESCE(("
                " SELECT tf.slug FROM target_fills tf"
                " WHERE tf.target = positions.target"
                " AND tf.condition_id = positions.condition_id"
                " AND tf.token_id = positions.token_id"
                " AND tf.slug != '' ORDER BY tf.ts DESC LIMIT 1), '')"
                " WHERE slug = ''")
        exit_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(pending_exits)")
        }
        if exit_cols and "kind" not in exit_cols:
            self.conn.execute(
                "ALTER TABLE pending_exits ADD COLUMN kind TEXT NOT NULL"
                " DEFAULT 'COPY'")
        self._migrate_multi_target_keys()

    # -------------------------------------------------- multi-target schemas
    def _table_sql(self, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return str(row["sql"]) if row and row["sql"] else None

    @staticmethod
    def _norm_sql(sql: str | None) -> str:
        return " ".join((sql or "").lower().split())

    def _backup_database(self) -> None:
        """One-time backup before rebuilding multi-target unique keys."""
        backup = self.path.with_name(self.path.name + ".pre-multitarget.bak")
        if backup.exists():
            return
        tmp = backup.with_name(backup.name + ".tmp")
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        dest = sqlite3.connect(str(tmp))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        os.replace(tmp, backup)

    def _migrate_multi_target_keys(self) -> None:
        """Scope target_fills/orders uniqueness by target wallet.

        Before this migration both tables were keyed by dedupe_key alone, so
        two target wallets whose fills landed in the same transaction with
        identical size/price could silently drop the second fill or reuse the
        first target's order row.
        """
        if self.get_kv("schema:multi_target_keys") == "1":
            return
        tf_sql = self._norm_sql(self._table_sql("target_fills"))
        order_sql = self._norm_sql(self._table_sql("orders"))
        tf_ok = not tf_sql or "primary key (target, dedupe_key)" in tf_sql
        orders_ok = (not order_sql
                     or "unique(target, dedupe_key, side, mode)" in order_sql)
        if tf_ok and orders_ok:
            self.set_kv("schema:multi_target_keys", "1")
            self.conn.commit()
            return

        self._backup_database()
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not tf_ok:
                self._rebuild_target_fills()
            if not orders_ok:
                self._rebuild_orders()
            self.set_kv("schema:multi_target_keys", "1")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _rebuild_target_fills(self) -> None:
        old_cols = [row[1] for row in self.conn.execute(
            "PRAGMA table_info(target_fills)")]
        new_cols = [
            "dedupe_key", "target", "ts", "kind", "condition_id", "token_id",
            "side", "price", "size", "usd", "outcome", "event_slug", "slug",
            "title", "raw", "processed_at",
        ]
        common = [c for c in new_cols if c in old_cols]
        col_sql = ", ".join(common)
        self.conn.execute("ALTER TABLE target_fills RENAME TO target_fills_old")
        self.conn.execute("DROP INDEX IF EXISTS idx_target_fills_target_ts")
        self.conn.execute(
            """
            CREATE TABLE target_fills (
                dedupe_key TEXT NOT NULL,
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
                processed_at INTEGER NOT NULL,
                PRIMARY KEY (target, dedupe_key)
            )
            """)
        if common:
            self.conn.execute(
                f"INSERT INTO target_fills ({col_sql})"
                f" SELECT {col_sql} FROM target_fills_old")
        self.conn.execute("DROP TABLE target_fills_old")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_target_fills_target_ts"
            " ON target_fills(target, ts)")

    def _rebuild_orders(self) -> None:
        old_cols = [row[1] for row in self.conn.execute(
            "PRAGMA table_info(orders)")]
        new_cols = [
            "id", "dedupe_key", "target", "mode", "side", "condition_id",
            "token_id", "outcome", "title", "event_slug", "limit_price",
            "size", "filled_size", "avg_price", "fee", "fee_rate", "status",
            "reason", "client_order_id", "exchange_order_id", "raw",
            "created_at", "updated_at",
        ]
        common = [c for c in new_cols if c in old_cols]
        col_sql = ", ".join(common)
        self.conn.execute("ALTER TABLE orders RENAME TO orders_old")
        self.conn.execute("DROP INDEX IF EXISTS idx_orders_status")
        self.conn.execute("DROP INDEX IF EXISTS idx_orders_token")
        self.conn.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL,
                target TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'live',
                side TEXT NOT NULL,
                condition_id TEXT NOT NULL DEFAULT '',
                token_id TEXT NOT NULL,
                outcome TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                event_slug TEXT NOT NULL DEFAULT '',
                limit_price REAL NOT NULL DEFAULT 0,
                size REAL NOT NULL DEFAULT 0,
                filled_size REAL NOT NULL DEFAULT 0,
                avg_price REAL NOT NULL DEFAULT 0,
                fee REAL NOT NULL DEFAULT 0,
                fee_rate REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                reason TEXT,
                client_order_id TEXT,
                exchange_order_id TEXT,
                raw TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(target, dedupe_key, side, mode)
            )
            """)
        if common:
            self.conn.execute(
                f"INSERT INTO orders ({col_sql})"
                f" SELECT {col_sql} FROM orders_old")
        self.conn.execute("DROP TABLE orders_old")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_token ON orders(token_id)")

    # ------------------------------------------------------------------ account
    def ensure_account(self, starting_cash: float) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO account(id, starting_cash, cash, realized_pnl,"
            " fees_paid, created_at) VALUES (1, ?, ?, 0, 0, ?)",
            (starting_cash, starting_cash, int(time.time())),
        )
        self.conn.commit()

    def account(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("account not initialised; call ensure_account()")
        return dict(row)

    def set_cash(self, cash: float) -> None:
        """Reconcile the ledger cash line with the live collateral balance."""
        self.conn.execute(
            "UPDATE account SET cash = ? WHERE id = 1",
            (float(cash),),
        )

    # ------------------------------------------------------------------- kv
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --------------------------------------------------------- target ledger
    def insert_target_fill(self, dedupe_key: str, target: str,
                           row: dict[str, Any]) -> bool:
        price = _f(row.get("price"))
        size = _f(row.get("size"))
        usd = row.get("usdc_size")
        usd = float(usd) if usd is not None else size * price
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO target_fills(dedupe_key, target, ts, kind,"
            " condition_id, token_id, side, price, size, usd, outcome, event_slug,"
            " slug, title, raw, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                dedupe_key,
                target,
                int(row.get("timestamp") or 0),
                (row.get("type") or "").upper(),
                row.get("condition_id") or "",
                row.get("token_id") or "",
                (row.get("side") or "").upper(),
                price,
                size,
                usd,
                row.get("outcome") or "",
                row.get("event_slug") or "",
                row.get("slug") or "",
                row.get("title") or "",
                json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                int(time.time()),
            ),
        )
        return cur.rowcount > 0

    def target_shares(self, target: str, token_id: str) -> float:
        row = self.conn.execute(
            "SELECT shares FROM target_positions WHERE target = ? AND token_id = ?",
            (target, token_id),
        ).fetchone()
        return float(row["shares"]) if row else 0.0

    def add_target_shares(self, target: str, condition_id: str,
                          token_id: str, delta: float) -> None:
        if not token_id or abs(delta) < 1e-12:
            return
        now = int(time.time())
        row = self.conn.execute(
            "SELECT shares FROM target_positions WHERE target = ? AND token_id = ?",
            (target, token_id),
        ).fetchone()
        if row:
            new_shares = max(0.0, float(row["shares"]) + delta)
            self.conn.execute(
                "UPDATE target_positions SET shares = ?, updated_at = ?"
                " WHERE target = ? AND token_id = ?",
                (new_shares, now, target, token_id),
            )
        elif delta > 0:
            self.conn.execute(
                "INSERT INTO target_positions(target, condition_id, token_id, shares,"
                " updated_at) VALUES (?,?,?,?,?)",
                (target, condition_id, token_id, delta, now),
            )

    def set_target_shares(self, target: str, token_id: str, shares: float) -> None:
        if not token_id:
            return
        self.conn.execute(
            "UPDATE target_positions SET shares = ?, updated_at = ?"
            " WHERE target = ? AND token_id = ?",
            (max(0.0, shares), int(time.time()), target, token_id),
        )

    def replace_target_positions(
            self, target: str, positions: list[tuple[str, str, float]],
            ts: int) -> int:
        """Replace the target ledger with the target's actual open positions.

        ``positions`` is ``(condition_id, token_id, shares)``.  Called only
        after a successful data-api /positions read, never on API failure.
        """
        self.conn.execute(
            "DELETE FROM target_positions WHERE target = ?", (target,))
        rows = [(target, cond, token, float(shares), ts)
                for cond, token, shares in positions
                if token and float(shares) > 1e-12]
        if rows:
            self.conn.executemany(
                "INSERT INTO target_positions(target, condition_id, token_id,"
                " shares, updated_at) VALUES (?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def target_tokens_for_condition(self, target: str, condition_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT token_id FROM target_positions WHERE target = ? AND condition_id = ?",
            (target, condition_id),
        ).fetchall()
        return [r["token_id"] for r in rows]

    # ------------------------------------------------------------ pending exits
    def queue_pending_exit(self, *, target: str, condition_id: str, token_id: str,
                           outcome: str, shares: float, reason: str,
                           ts: int, kind: str = "COPY") -> None:
        """Remember an unfilled target exit; retried by the engine.

        Shares are capped at the current paper position, so a stale pending
        amount can never sell more than we actually hold.
        """
        if shares <= 1e-12:
            return
        pos = self.position(target, condition_id, token_id)
        held = float(pos["shares"]) if pos and pos.get("status") == "OPEN" else 0.0
        if held <= 1e-12:
            return
        row = self.pending_exit(target, condition_id, token_id)
        pending = float(row["shares"]) if row else 0.0
        total = min(held, pending + float(shares))
        if total <= 1e-12:
            return
        if row:
            self.conn.execute(
                "UPDATE pending_exits SET shares = ?, outcome = ?, reason = ?,"
                " kind = ?, updated_at = ? WHERE id = ?",
                (total, outcome, reason, kind, ts, row["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO pending_exits(target, condition_id, token_id, outcome,"
                " shares, reason, kind, attempts, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,0,?,?)",
                (target, condition_id, token_id, outcome, total, reason, kind,
                 ts, ts),
            )

    def pending_exit(self, target: str, condition_id: str,
                     token_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pending_exits WHERE target = ? AND condition_id = ?"
            " AND token_id = ?",
            (target, condition_id, token_id),
        ).fetchone()
        return dict(row) if row else None

    def pending_exits(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM pending_exits ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_pending_exit(self, exit_id: int, *, shares: float,
                            attempts: int, reason: str, ts: int) -> None:
        if shares <= 1e-12:
            self.delete_pending_exit(exit_id)
            return
        self.conn.execute(
            "UPDATE pending_exits SET shares = ?, attempts = ?, reason = ?,"
            " updated_at = ? WHERE id = ?",
            (shares, attempts, reason, ts, exit_id),
        )

    def delete_pending_exit(self, exit_id: int) -> None:
        self.conn.execute("DELETE FROM pending_exits WHERE id = ?", (exit_id,))

    def clear_pending_exits(self, target: str, condition_id: str,
                            token_id: str) -> None:
        self.conn.execute(
            "DELETE FROM pending_exits WHERE target = ? AND condition_id = ?"
            " AND token_id = ?",
            (target, condition_id, token_id),
        )

    # ----------------------------------------------------------- pending buys
    def create_pending_buy(self, *, signal_key: str, target: str,
                           condition_id: str, token_id: str, outcome: str,
                           title: str, event_slug: str, slug: str,
                           signal_price: float, limit_price: float,
                           fee_rate: float, shares: float,
                           target_shares_at_entry: float, end_ts: int,
                           deadline_ts: int, signal_ts: int,
                           source: str = "onchain", order_id: int | None = None,
                           status: str = "SUBMITTING",
                           ts: int | None = None) -> int:
        now = int(ts or time.time())
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO pending_buys(signal_key, target,"
            " condition_id, token_id, outcome, title, event_slug, slug,"
            " signal_price, limit_price, fee_rate, shares,"
            " target_shares_at_entry, end_ts, order_id, deadline_ts, status,"
            " signal_ts, source, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (signal_key, target, condition_id, token_id, outcome, title,
             event_slug, slug, float(signal_price), float(limit_price),
             float(fee_rate), float(shares), float(target_shares_at_entry),
             int(end_ts or 0), order_id, int(deadline_ts), status,
             int(signal_ts), source, now, now),
        )
        if cur.rowcount > 0:
            return int(cur.lastrowid or 0)
        row = self.pending_buy_for(target, signal_key)
        return int(row["id"]) if row else 0

    def pending_buy(self, pending_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pending_buys WHERE id = ?", (int(pending_id),)
        ).fetchone()
        return dict(row) if row else None

    def pending_buy_for(self, target: str,
                        signal_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pending_buys WHERE target = ? AND signal_key = ?",
            (target, signal_key),
        ).fetchone()
        return dict(row) if row else None

    def pending_buys(self, statuses: tuple[str, ...] | None = None, *,
                     target: str | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if statuses:
            where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        if target is not None:
            where.append("target = ?")
            params.append(target)
        sql = "SELECT * FROM pending_buys"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def active_pending_buys(self, target: str, condition_id: str,
                            token_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM pending_buys WHERE target = ? AND condition_id = ?"
            " AND token_id = ? AND status IN"
            " ('SUBMITTING','PENDING','CANCEL_REQUESTED','TAIL_SUBMITTED')"
            " ORDER BY id",
            (target, condition_id, token_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def request_cancel_pending_buys(self, target: str, condition_id: str,
                                    token_id: str, reason: str,
                                    ts: int | None = None,
                                    target_sold_fraction: float | None = None
                                    ) -> int:
        now = int(ts or time.time())
        if target_sold_fraction is None:
            cur = self.conn.execute(
                "UPDATE pending_buys SET status = 'CANCEL_REQUESTED',"
                " reason = ?, updated_at = ? WHERE target = ?"
                " AND condition_id = ? AND token_id = ?"
                " AND status IN ('SUBMITTING','PENDING')",
                (reason, now, target, condition_id, token_id),
            )
        else:
            cur = self.conn.execute(
                "UPDATE pending_buys SET status = 'CANCEL_REQUESTED',"
                " reason = ?, target_sold_fraction = MAX("
                " target_sold_fraction, ?), updated_at = ? WHERE target = ?"
                " AND condition_id = ? AND token_id = ?"
                " AND status IN ('SUBMITTING','PENDING')",
                (reason, float(target_sold_fraction), now, target,
                 condition_id, token_id),
            )
        return int(cur.rowcount or 0)

    def request_cancel_pending_buys_for_condition(
            self, target: str, condition_id: str, reason: str,
            ts: int | None = None) -> int:
        cur = self.conn.execute(
            "UPDATE pending_buys SET status = 'CANCEL_REQUESTED', reason = ?,"
            " updated_at = ? WHERE target = ? AND condition_id = ?"
            " AND status IN ('SUBMITTING','PENDING')",
            (reason, int(ts or time.time()), target, condition_id),
        )
        return int(cur.rowcount or 0)

    def update_pending_buy(self, pending_id: int, *,
                           status: str | None = None,
                           reason: str | None = None,
                           order_id: int | None = None,
                           fak_order_id: int | None = None,
                           shares: float | None = None,
                           limit_price: float | None = None,
                           filled_shares: float | None = None,
                           notional: float | None = None,
                           fee: float | None = None,
                           deadline_ts: int | None = None,
                           cancel_confirmed: int | None = None,
                           attempts: int | None = None,
                           target_sold_fraction: float | None = None,
                           ts: int | None = None) -> None:
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [int(ts or time.time())]
        mapping = {
            "status": status, "reason": reason, "order_id": order_id,
            "fak_order_id": fak_order_id, "shares": shares,
            "limit_price": limit_price, "filled_shares": filled_shares,
            "notional": notional, "fee": fee, "deadline_ts": deadline_ts,
            "cancel_confirmed": cancel_confirmed, "attempts": attempts,
            "target_sold_fraction": target_sold_fraction,
        }
        for column, value in mapping.items():
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(value)
        params.append(int(pending_id))
        self.conn.execute(
            "UPDATE pending_buys SET " + ", ".join(updates) + " WHERE id = ?",
            params,
        )

    def transition_pending_buy(self, pending_id: int, *,
                               from_statuses: tuple[str, ...],
                               to_status: str, reason: str | None = None,
                               ts: int | None = None,
                               **fields: Any) -> bool:
        """Conditional status transition (used to win cancel/timeout races)."""
        if not from_statuses:
            return False
        allowed = {
            "reason", "order_id", "fak_order_id", "shares", "limit_price",
            "filled_shares", "notional", "fee", "deadline_ts",
            "cancel_confirmed", "attempts", "target_sold_fraction",
        }
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [to_status, int(ts or time.time())]
        if reason is not None:
            updates.append("reason = ?")
            params.append(reason)
        for column, value in fields.items():
            if column not in allowed:
                raise ValueError(f"unknown pending_buy field {column!r}")
            if value is None:
                continue
            updates.append(f"{column} = ?")
            params.append(value)
        params.append(int(pending_id))
        placeholders = ",".join("?" for _ in from_statuses)
        params.extend(from_statuses)
        cur = self.conn.execute(
            "UPDATE pending_buys SET " + ", ".join(updates)
            + f" WHERE id = ? AND status IN ({placeholders})",
            params,
        )
        return bool(cur.rowcount)

    def pending_buy_commitment(self, *, target: str | None = None,
                               condition_id: str | None = None,
                               exclude_id: int | None = None) -> float:
        where = ["status IN ('SUBMITTING','PENDING','CANCEL_REQUESTED')"]
        params: list[Any] = []
        if target is not None:
            where.append("target = ?")
            params.append(target)
        if condition_id is not None:
            where.append("condition_id = ?")
            params.append(condition_id)
        if exclude_id is not None:
            where.append("id != ?")
            params.append(int(exclude_id))
        row = self.conn.execute(
            "SELECT COALESCE(SUM(MAX(shares - filled_shares, 0) * limit_price),"
            " 0) AS v FROM pending_buys WHERE " + " AND ".join(where),
            params,
        ).fetchone()
        return float(row["v"]) if row else 0.0

    def count_pending_buy_tokens(self, target: str | None = None) -> int:
        where = ["status IN ('SUBMITTING','PENDING','CANCEL_REQUESTED')"]
        params: list[Any] = []
        if target is not None:
            where.append("target = ?")
            params.append(target)
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT target || '|' || token_id) AS c"
            " FROM pending_buys WHERE " + " AND ".join(where),
            params,
        ).fetchone()
        return int(row["c"]) if row else 0

    def fill_totals_for_order(self,
                              order_id: int) -> tuple[float, float, float]:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(shares), 0) AS s,"
            " COALESCE(SUM(notional), 0) AS n, COALESCE(SUM(fee), 0) AS f"
            " FROM fills WHERE order_id = ?",
            (int(order_id),),
        ).fetchone()
        if not row:
            return 0.0, 0.0, 0.0
        return float(row["s"]), float(row["n"]), float(row["f"])

    def has_pending_manual_sell(self, target: str, condition_id: str,
                                token_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM manual_sells WHERE target = ? AND condition_id = ?"
            " AND token_id = ? AND status IN ('PENDING', 'PARTIAL') LIMIT 1",
            (target, condition_id, token_id),
        ).fetchone()
        return row is not None

    def consume_manual_credit(self, target: str, condition_id: str,
                              token_id: str, basis_reduction: float,
                              credit_reduction: float,
                              ts: int | None = None) -> None:
        """Advance basis/credit for a target sell fully covered by manual sales.

        No shares change hands, but the target-equivalent position and the
        manual credit must both shrink so later fractions stay correct.
        """
        row = self.conn.execute(
            "SELECT * FROM positions WHERE target = ? AND condition_id = ?"
            " AND token_id = ? AND status = 'OPEN'",
            (target, condition_id, token_id),
        ).fetchone()
        if row is None:
            return
        basis = float(row["basis_shares"] or 0.0) or float(row["shares"])
        credit = float(row["manual_credit"] or 0.0)
        self.conn.execute(
            "UPDATE positions SET basis_shares = ?, manual_credit = ?,"
            " updated_at = ? WHERE id = ?",
            (max(0.0, basis - max(0.0, basis_reduction)),
             max(0.0, credit - max(0.0, credit_reduction)),
             int(ts or time.time()), row["id"]),
        )

    def reduce_pending_exit(self, target: str, condition_id: str,
                            token_id: str, shares: float) -> None:
        """A manual sell takes precedence over a queued target exit."""
        if shares <= 1e-12:
            return
        row = self.pending_exit(target, condition_id, token_id)
        if not row:
            return
        remaining = max(0.0, float(row["shares"]) - float(shares))
        if remaining <= 1e-9:
            self.delete_pending_exit(row["id"])
        else:
            self.conn.execute(
                "UPDATE pending_exits SET shares = ?, updated_at = ? WHERE id = ?",
                (remaining, int(time.time()), row["id"]),
            )

    # ---------------------------------------------------------- manual sells
    def queue_manual_sell(self, *, target: str, condition_id: str,
                          token_id: str, pct: float, shares_requested: float,
                          ts: int | None = None) -> int:
        now = int(ts or time.time())
        cur = self.conn.execute(
            "INSERT INTO manual_sells(target, condition_id, token_id, pct,"
            " shares_requested, remaining_shares, status, attempts,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?, 'PENDING', 0, ?, 0)",
            (target, condition_id, token_id, float(pct),
             float(shares_requested), float(shares_requested), now),
        )
        return int(cur.lastrowid or 0)

    def manual_sell(self, request_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM manual_sells WHERE id = ?", (int(request_id),)
        ).fetchone()
        return dict(row) if row else None

    def pending_manual_sells(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM manual_sells WHERE status IN ('PENDING', 'PARTIAL')"
            " ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def update_manual_sell(self, request_id: int, *,
                           status: str | None = None,
                           remaining_shares: float | None = None,
                           filled_shares: float | None = None,
                           avg_price: float | None = None,
                           fee: float | None = None,
                           attempts: int | None = None,
                           reason: str | None = None,
                           ts: int | None = None) -> None:
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [int(ts or time.time())]
        mapping = {
            "status": status, "remaining_shares": remaining_shares,
            "filled_shares": filled_shares, "avg_price": avg_price,
            "fee": fee, "attempts": attempts, "reason": reason,
        }
        for column, value in mapping.items():
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(value)
        params.append(int(request_id))
        self.conn.execute(
            "UPDATE manual_sells SET " + ", ".join(updates) + " WHERE id = ?",
            params,
        )

    # --------------------------------------------------------------- orders
    def create_order(self, *, dedupe_key: str, target: str, mode: str,
                     side: str, condition_id: str, token_id: str,
                     outcome: str, limit_price: float, size: float,
                     status: str = "INTENT", reason: str | None = None,
                     fee_rate: float = 0.0, title: str = "",
                     event_slug: str = "",
                     client_order_id: str | None = None,
                     ts: int | None = None) -> dict[str, Any]:
        """Insert an order intent before any network call.

        The UNIQUE key makes the intent idempotent: replaying the same target
        fill cannot create a second live order.  Callers must commit before
        submitting so a crash is recoverable.
        """
        now = int(ts or time.time())
        self.conn.execute(
            "INSERT OR IGNORE INTO orders(dedupe_key, target, mode, side,"
            " condition_id, token_id, outcome, title, event_slug, limit_price,"
            " size, filled_size, avg_price, fee, fee_rate, status, reason,"
            " client_order_id, raw, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?,?,?,?,?,?)",
            (dedupe_key, target, mode, side, condition_id, token_id, outcome,
             title, event_slug, float(limit_price), float(size),
             float(fee_rate), status, reason, client_order_id, "", now, now),
        )
        row = self.conn.execute(
            "SELECT * FROM orders WHERE target = ? AND dedupe_key = ?"
            " AND side = ? AND mode = ?",
            (target, dedupe_key, side, mode),
        ).fetchone()
        return dict(row) if row else {}

    def order(self, order_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def order_for(self, dedupe_key: str, side: str, mode: str = "live",
                  target: str | None = None) -> dict[str, Any] | None:
        if target is None:
            row = self.conn.execute(
                "SELECT * FROM orders WHERE dedupe_key = ? AND side = ?"
                " AND mode = ? ORDER BY id LIMIT 1",
                (dedupe_key, side, mode),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM orders WHERE target = ? AND dedupe_key = ?"
                " AND side = ? AND mode = ?",
                (target, dedupe_key, side, mode),
            ).fetchone()
        return dict(row) if row else None

    def orders(self, *, mode: str = "live",
               statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM orders WHERE mode = ?"
        params: list[Any] = [mode]
        if statuses:
            sql += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        sql += " ORDER BY id"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def update_order(self, order_id: int, *, status: str | None = None,
                     filled_size: float | None = None,
                     avg_price: float | None = None,
                     fee: float | None = None,
                     reason: str | None = None,
                     client_order_id: str | None = None,
                     exchange_order_id: str | None = None,
                     raw: str | None = None,
                     ts: int | None = None) -> None:
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [int(ts or time.time())]
        mapping = {
            "status": status, "filled_size": filled_size, "avg_price": avg_price,
            "fee": fee, "reason": reason, "client_order_id": client_order_id,
            "exchange_order_id": exchange_order_id, "raw": raw,
        }
        for column, value in mapping.items():
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(value)
        params.append(int(order_id))
        self.conn.execute(
            "UPDATE orders SET " + ", ".join(updates) + " WHERE id = ?", params)

    def fill_count_for_order(self, order_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM fills WHERE order_id = ?",
            (int(order_id),),
        ).fetchone()
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------ risk views
    def realized_pnl_since(self, start_ts: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS v FROM ("
            " SELECT realized_pnl FROM fills WHERE ts >= ?"
            " UNION ALL SELECT realized_pnl FROM settlements WHERE ts >= ?"
            ")", (int(start_ts), int(start_ts)),
        ).fetchone()
        return float(row["v"]) if row else 0.0

    def total_open_cost(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(shares * avg_cost), 0) AS v FROM positions"
            " WHERE status = 'OPEN'").fetchone()
        return float(row["v"]) if row else 0.0

    # -------------------------------------------------------------- signals
    def record_signal(self, *, target: str, dedupe_key: str, ts: int,
                      latency_seconds: float, side: str, condition_id: str,
                      token_id: str, outcome: str, price: float, size: float,
                      usd: float, title: str, event_slug: str, copied: bool,
                      reason: str | None, our_shares: float | None = None,
                      our_price: float | None = None,
                      our_notional: float | None = None,
                      our_fee: float | None = None,
                      source: str = "data_api", slug: str = "") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO signals(target, dedupe_key, ts, latency_seconds,"
            " side, condition_id, token_id, outcome, price, size, usd, title,"
            " event_slug, slug, copied, reason, our_shares, our_price,"
            " our_notional, our_fee, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                target, dedupe_key, ts, latency_seconds, side, condition_id,
                token_id, outcome, price, size, usd, title, event_slug, slug,
                1 if copied else 0, reason, our_shares, our_price, our_notional,
                our_fee, source, int(time.time()),
            ),
        )

    def update_signal_result(self, target: str, dedupe_key: str, *,
                             copied: bool, reason: str | None,
                             our_shares: float | None = None,
                             our_price: float | None = None,
                             our_notional: float | None = None,
                             our_fee: float | None = None) -> None:
        """Update a maker_pending signal once its entry reaches a terminal state."""
        self.conn.execute(
            "UPDATE signals SET copied = ?, reason = ?, our_shares = ?,"
            " our_price = ?, our_notional = ?, our_fee = ?"
            " WHERE target = ? AND dedupe_key = ?",
            (1 if copied else 0, reason, our_shares, our_price, our_notional,
             our_fee, target, dedupe_key),
        )

    def signal_for(self, target: str,
                   dedupe_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM signals WHERE target = ? AND dedupe_key = ?",
            (target, dedupe_key),
        ).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------- positions
    def position(self, target: str, condition_id: str,
                 token_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE target = ? AND condition_id = ?"
            " AND token_id = ?",
            (target, condition_id, token_id),
        ).fetchone()
        return dict(row) if row else None

    def open_positions(self, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT p.*, m.bid AS mark_bid, m.ask AS mark_ask, m.mid AS mark_mid"
            " FROM positions p LEFT JOIN marks m ON m.token_id = p.token_id"
            " WHERE p.status = 'OPEN' ORDER BY p.shares * p.avg_cost DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def open_conditions(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT condition_id FROM positions WHERE status = 'OPEN'"
        ).fetchall()
        return [r["condition_id"] for r in rows]

    def positions_for_condition(self, condition_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE condition_id = ? AND status = 'OPEN'",
            (condition_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_open_positions(self, target: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM positions WHERE status = 'OPEN'"
        params: list[Any] = []
        if target is not None:
            sql += " AND target = ?"
            params.append(target)
        row = self.conn.execute(sql, params).fetchone()
        return int(row["c"]) if row else 0

    def market_exposure(self, condition_id: str,
                        target: str | None = None) -> float:
        sql = ("SELECT COALESCE(SUM(shares * avg_cost), 0) AS v FROM positions"
               " WHERE condition_id = ? AND status = 'OPEN'")
        params: list[Any] = [condition_id]
        if target is not None:
            sql += " AND target = ?"
            params.append(target)
        row = self.conn.execute(sql, params).fetchone()
        return float(row["v"]) if row else 0.0

    def apply_buy(self, *, ts: int, target: str, signal_id: int | None,
                  condition_id: str, token_id: str, outcome: str, title: str,
                  event_slug: str, shares: float, price: float,
                  fee: float, order_id: int | None = None,
                  slug: str = "") -> None:
        if shares <= 0:
            return
        notional = shares * price
        total_cost = notional + fee
        self.conn.execute(
            "UPDATE account SET cash = cash - ?, fees_paid = fees_paid + ? WHERE id = 1",
            (total_cost, fee),
        )
        row = self.conn.execute(
            "SELECT * FROM positions WHERE target = ? AND condition_id = ?"
            " AND token_id = ?",
            (target, condition_id, token_id),
        ).fetchone()
        if row and row["status"] == "OPEN":
            old_shares = float(row["shares"])
            old_basis = float(row["basis_shares"] or 0.0) or old_shares
            new_shares = old_shares + shares
            new_basis = old_basis + shares
            new_avg = (float(row["avg_cost"]) * old_shares + total_cost) / new_shares
            self.conn.execute(
                "UPDATE positions SET shares = ?, basis_shares = ?,"
                " avg_cost = ?, slug = COALESCE(NULLIF(?, ''), slug),"
                " updated_at = ? WHERE id = ?",
                (new_shares, new_basis, new_avg, slug, ts, row["id"]),
            )
        elif row:
            self.conn.execute(
                "UPDATE positions SET status = 'OPEN', shares = ?,"
                " basis_shares = ?, manual_credit = 0, avg_cost = ?,"
                " outcome = ?, title = ?, event_slug = ?, slug = ?,"
                " opened_at = ?, updated_at = ? WHERE id = ?",
                (shares, shares, total_cost / shares, outcome, title, event_slug,
                 slug, ts, ts, row["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO positions(target, condition_id, token_id, outcome,"
                " title, event_slug, slug, shares, basis_shares, manual_credit,"
                " avg_cost, realized_pnl, opened_at, updated_at, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,?,0,?,?,'OPEN')",
                (target, condition_id, token_id, outcome, title, event_slug,
                 slug, shares, shares, total_cost / shares, ts, ts),
            )
        self.conn.execute(
            "INSERT INTO fills(ts, target, signal_id, order_id, condition_id,"
            " token_id, outcome, side, shares, price, notional, fee, kind)"
            " VALUES (?,?,?,?,?,?,?,'BUY',?,?,?,?,'COPY')",
            (ts, target, signal_id, order_id, condition_id, token_id, outcome,
             shares, price, notional, fee),
        )

    def apply_sell(self, *, ts: int, target: str, signal_id: int | None,
                   condition_id: str, token_id: str, outcome: str,
                   shares: float, price: float, fee: float,
                   note: str | None = None,
                   order_id: int | None = None,
                   basis_reduction: float | None = None,
                   credit_reduction: float = 0.0,
                   manual: bool = False,
                   kind: str = "COPY") -> dict[str, float] | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE target = ? AND condition_id = ?"
            " AND token_id = ? AND status = 'OPEN'",
            (target, condition_id, token_id),
        ).fetchone()
        if row is None:
            return None
        held = float(row["shares"])
        sell_shares = min(shares, held)
        if sell_shares <= 0:
            return None
        if shares > 0:
            fee = fee * (sell_shares / shares)
        notional = sell_shares * price
        proceeds = notional - fee
        realized = proceeds - sell_shares * float(row["avg_cost"])
        remaining = held - sell_shares
        status = "CLOSED" if remaining <= 1e-9 else "OPEN"
        basis = float(row["basis_shares"] or 0.0) or held
        credit = float(row["manual_credit"] or 0.0)
        if manual:
            # A manual sale is a prepayment against future target sells: it
            # reduces current shares but stays in the copy basis.
            new_basis = basis
            new_credit = credit + sell_shares
            if kind == "COPY":
                kind = "MANUAL"
        else:
            reduction = (sell_shares if basis_reduction is None
                         else max(0.0, basis_reduction))
            new_basis = max(0.0, basis - reduction)
            new_credit = max(0.0, credit - max(0.0, credit_reduction))
        if status == "CLOSED":
            new_basis = 0.0
            new_credit = 0.0
        self.conn.execute(
            "UPDATE positions SET shares = ?, basis_shares = ?,"
            " manual_credit = ?, realized_pnl = realized_pnl + ?,"
            " status = ?, updated_at = ? WHERE id = ?",
            (max(0.0, remaining), new_basis, new_credit, realized, status, ts,
             row["id"]),
        )
        self.conn.execute(
            "UPDATE account SET cash = cash + ?, realized_pnl = realized_pnl + ?,"
            " fees_paid = fees_paid + ? WHERE id = 1",
            (proceeds, realized, fee),
        )
        self.conn.execute(
            "INSERT INTO fills(ts, target, signal_id, order_id, condition_id,"
            " token_id, outcome, side, shares, price, notional, fee,"
            " realized_pnl, kind, note)"
            " VALUES (?,?,?,?,?,?,?,'SELL',?,?,?,?,?,?,?)",
            (ts, target, signal_id, order_id, condition_id, token_id, outcome,
             sell_shares, price, notional, fee, realized, kind, note),
        )
        if status == "CLOSED":
            self.clear_pending_exits(target, condition_id, token_id)
        return {"shares": sell_shares, "price": price, "notional": notional,
                "fee": fee, "realized": realized}

    def settle_position(self, pos: dict[str, Any], payout_per_share: float,
                        ts: int) -> float:
        # Re-read inside the write transaction: a risk/target/manual sell may
        # have reduced or closed this position since the settlement scan.
        row = self.conn.execute(
            "SELECT * FROM positions WHERE id = ? AND status = 'OPEN'",
            (int(pos["id"]),),
        ).fetchone()
        if row is None:
            return 0.0
        shares = float(row["shares"])
        avg_cost = float(row["avg_cost"])
        if shares <= 1e-12:
            return 0.0
        payout = shares * payout_per_share
        realized = payout - shares * avg_cost
        self.conn.execute(
            "UPDATE positions SET shares = 0, basis_shares = 0,"
            " manual_credit = 0, status = 'CLOSED',"
            " realized_pnl = realized_pnl + ?, updated_at = ? WHERE id = ?",
            (realized, ts, row["id"]),
        )
        self.conn.execute(
            "UPDATE account SET cash = cash + ?, realized_pnl = realized_pnl + ?"
            " WHERE id = 1",
            (payout, realized),
        )
        self.conn.execute(
            "INSERT INTO settlements(ts, condition_id, token_id, outcome, shares,"
            " payout, realized_pnl) VALUES (?,?,?,?,?,?,?)",
            (ts, row["condition_id"], row["token_id"], row["outcome"],
             shares, payout, realized),
        )
        self.clear_pending_exits(row["target"], row["condition_id"],
                                 row["token_id"])
        return realized

    # ------------------------------------------------------------------ marks
    def set_mark(self, token_id: str, bid: float | None, ask: float | None,
                 mid: float | None, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO marks(token_id, ts, bid, ask, mid) VALUES (?,?,?,?,?)"
            " ON CONFLICT(token_id) DO UPDATE SET ts = excluded.ts,"
            " bid = excluded.bid, ask = excluded.ask, mid = excluded.mid",
            (token_id, ts, bid, ask, mid),
        )

    # --------------------------------------------------------------- reporting
    def signal_summary(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT copied, reason, COUNT(*) AS n, COALESCE(SUM(our_notional),0)"
            " AS notional FROM signals GROUP BY copied, reason ORDER BY n DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def signal_counts(self) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(copied),0) AS copied FROM signals"
        ).fetchone()
        return int(row["total"]), int(row["copied"])

    def recent_fills(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def latency_stats(self) -> dict[str, float] | None:
        rows = self.conn.execute(
            "SELECT latency_seconds FROM signals WHERE copied = 1"
            " AND latency_seconds IS NOT NULL ORDER BY latency_seconds"
        ).fetchall()
        vals = [float(r["latency_seconds"]) for r in rows]
        if not vals:
            return None
        def q(p: float) -> float:
            return vals[min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))]
        return {"n": len(vals), "median": q(0.5), "p90": q(0.9),
                "max": vals[-1]}

    def all_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def all_fills(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM fills ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def all_signals(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM signals ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
