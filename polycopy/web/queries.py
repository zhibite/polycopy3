"""Read-only queries against the paper-trading SQLite database."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BJ = timezone(timedelta(hours=8))

PROJECT_DIR = Path(
    os.environ.get("POLY_PROJECT_DIR", Path(__file__).resolve().parents[2])
)
DB_PATH = Path(os.environ.get("POLY_DB_PATH", PROJECT_DIR / "data" / "paper_f705.db"))
CONFIG_PATH = Path(
    os.environ.get("POLY_CONFIG_PATH", PROJECT_DIR / "configs" / "paper_f705.toml")
)

PAGE_DEFAULT = 50


def connect() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error:
        return None


def connect_write() -> sqlite3.Connection | None:
    """Short-lived write connection for queuing manual sell requests.

    The dashboard never executes trades; it only inserts a request that the
    bot's maintenance loop picks up.  WAL + busy_timeout lets this coexist
    with the bot's connection.
    """
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn
    except sqlite3.Error:
        return None


def close(conn: sqlite3.Connection | None) -> None:
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _all(conn: sqlite3.Connection | None, sql: str,
         params: tuple | list = ()) -> list[dict[str, Any]]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error:
        return []


def _one(conn: sqlite3.Connection | None, sql: str,
         params: tuple | list = ()) -> dict[str, Any] | None:
    rows = _all(conn, sql, params)
    return rows[0] if rows else None


def _scalar(conn: sqlite3.Connection | None, sql: str,
            params: tuple | list = (), default: float = 0.0) -> float:
    row = _one(conn, sql, params)
    if not row:
        return default
    value = next(iter(row.values()))
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- primitives

def account(conn: sqlite3.Connection | None) -> dict[str, Any]:
    row = _one(conn, "SELECT * FROM account WHERE id = 1")
    return row or {
        "starting_cash": 0.0, "cash": 0.0, "realized_pnl": 0.0,
        "fees_paid": 0.0, "created_at": None,
    }


def kv_int(conn: sqlite3.Connection | None, key: str) -> int | None:
    row = _one(conn, "SELECT value FROM kv WHERE key = ?", (key,))
    if not row:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def kv_str(conn: sqlite3.Connection | None, key: str) -> str | None:
    row = _one(conn, "SELECT value FROM kv WHERE key = ?", (key,))
    return str(row["value"]) if row else None


def open_positions(conn: sqlite3.Connection | None,
                   limit: int = 500) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT p.*, m.bid AS mark_bid, m.ask AS mark_ask, m.mid AS mark_mid"
        " FROM positions p LEFT JOIN marks m ON m.token_id = p.token_id"
        " WHERE p.status = 'OPEN'"
        " ORDER BY p.shares * p.avg_cost DESC LIMIT ?",
        (limit,),
    )


def position(conn: sqlite3.Connection | None,
             position_id: int) -> dict[str, Any] | None:
    return _one(
        conn,
        "SELECT * FROM positions WHERE id = ?",
        (int(position_id),),
    )


def queue_manual_sell(position_id: int, pct: float) -> tuple[int | None, str | None]:
    """Insert a manual sell request for the bot to execute.

    Returns ``(request_id, error)``.  The request snapshots the current shares
    so a concurrent target sell cannot make it sell more than intended; the
    bot caps it again at execution time.
    """
    conn = connect_write()
    if conn is None:
        return None, "数据库不可用"
    try:
        row = conn.execute(
            "SELECT * FROM positions WHERE id = ? AND status = 'OPEN'",
            (int(position_id),),
        ).fetchone()
        if row is None:
            return None, "仓位不存在或已平仓"
        shares = float(row["shares"] or 0.0)
        if shares <= 1e-9:
            return None, "仓位数量为 0"
        requested = shares if pct >= 1.0 else shares * pct
        if requested <= 1e-9:
            return None, "卖出数量过小"
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO manual_sells(target, condition_id, token_id, pct,"
            " shares_requested, remaining_shares, status, attempts,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?, 'PENDING', 0, ?, 0)",
            (str(row["target"]), str(row["condition_id"]),
             str(row["token_id"]), float(pct), requested, requested, now),
        )
        conn.commit()
        return int(cur.lastrowid or 0), None
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return None, str(exc)
    finally:
        close(conn)


def request_restart() -> str | None:
    """Queue a bot restart in the database the dashboard is watching."""
    conn = connect_write()
    if conn is None:
        return "数据库不可用"
    try:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES('bot:restart_request', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(int(time.time())),),
        )
        conn.commit()
        return None
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return str(exc)
    finally:
        close(conn)


def pending_manual_sells(conn: sqlite3.Connection | None,
                         limit: int = 20) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT m.*, p.title AS title, p.outcome AS outcome"
        " FROM manual_sells m LEFT JOIN positions p"
        " ON p.target = m.target AND p.condition_id = m.condition_id"
        " AND p.token_id = m.token_id"
        " WHERE m.status IN ('PENDING', 'PARTIAL')"
        " ORDER BY m.id DESC LIMIT ?",
        (limit,),
    )


def recent_fills(conn: sqlite3.Connection | None,
                 limit: int = 15) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT f.*, p.title AS title, p.event_slug AS event_slug,"
        " p.slug AS market_slug"
        " FROM fills f LEFT JOIN positions p ON p.target = f.target"
        " AND p.condition_id = f.condition_id AND p.token_id = f.token_id"
        " ORDER BY f.ts DESC, f.id DESC LIMIT ?",
        (limit,),
    )


def recent_settlements(conn: sqlite3.Connection | None,
                       limit: int = 20) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT s.*, p.title AS title, p.event_slug AS event_slug"
        " FROM settlements s LEFT JOIN positions p"
        " ON p.condition_id = s.condition_id AND p.token_id = s.token_id"
        " ORDER BY s.ts DESC, s.id DESC LIMIT ?",
        (limit,),
    )


# ------------------------------------------------------------------ summary

def summary(conn: sqlite3.Connection | None) -> dict[str, Any]:
    acc = account(conn)
    opens = open_positions(conn, limit=10_000)
    market_value = 0.0
    unrealized = 0.0
    cost = 0.0
    for pos in opens:
        shares = float(pos.get("shares") or 0)
        avg = float(pos.get("avg_cost") or 0)
        mark = pos.get("mark_mid")
        mark = float(mark) if mark is not None else avg
        market_value += shares * mark
        cost += shares * avg
        unrealized += shares * (mark - avg)

    cash = float(acc.get("cash") or 0)
    starting = float(acc.get("starting_cash") or 0)
    realized = float(acc.get("realized_pnl") or 0)
    equity = cash + market_value
    # Deposits/withdrawals change equity but are not trading P&L.
    total_pnl = realized + unrealized

    today_start = int(
        datetime.now(BJ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    today_realized = _scalar(
        conn, "SELECT COALESCE(SUM(realized_pnl),0) FROM fills WHERE ts >= ?",
        (today_start,),
    ) + _scalar(
        conn, "SELECT COALESCE(SUM(realized_pnl),0) FROM settlements WHERE ts >= ?",
        (today_start,),
    )
    today_fills = int(_scalar(
        conn, "SELECT COUNT(*) FROM fills WHERE ts >= ?", (today_start,)
    ))

    signals_total = int(_scalar(conn, "SELECT COUNT(*) FROM signals"))
    signals_copied = int(_scalar(conn, "SELECT COUNT(*) FROM signals WHERE copied = 1"))

    closed = _all(
        conn,
        "SELECT realized_pnl FROM positions WHERE status = 'CLOSED'",
    )
    pnls = [float(r.get("realized_pnl") or 0) for r in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    win_sum = sum(wins)
    loss_sum = sum(losses)
    profit_factor = (win_sum / abs(loss_sum)) if loss_sum < 0 else None

    heartbeat = kv_int(conn, "bot_heartbeat")
    now = int(time.time())
    bot_online = bool(heartbeat and now - heartbeat <= 90)

    last_target_fill = int(_scalar(
        conn, "SELECT COALESCE(MAX(ts),0) FROM target_fills"
    ) or 0)

    return {
        "starting_cash": starting,
        "cash": cash,
        "market_value": market_value,
        "cost": cost,
        "equity": equity,
        "unrealized": unrealized,
        "realized": realized,
        "fees_paid": float(acc.get("fees_paid") or 0),
        "total_pnl": total_pnl,
        "total_return": (total_pnl / starting) if starting else None,
        "today_realized": today_realized,
        "today_fills": today_fills,
        "open_positions": len(opens),
        "signals_total": signals_total,
        "signals_copied": signals_copied,
        "copy_rate": (signals_copied / signals_total) if signals_total else None,
        "closed_positions": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "profit_factor": profit_factor,
        "bot_heartbeat": heartbeat,
        "bot_online": bot_online,
        "last_target_fill": last_target_fill or None,
    }


def daily_pnl(conn: sqlite3.Connection | None, days: int = 14) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT d, ROUND(SUM(pnl), 2) AS pnl, SUM(n) AS n FROM ("
        "  SELECT date(ts, 'unixepoch', '+8 hours') AS d, realized_pnl AS pnl,"
        "         1 AS n FROM fills WHERE realized_pnl IS NOT NULL"
        "  UNION ALL"
        "  SELECT date(ts, 'unixepoch', '+8 hours') AS d, realized_pnl AS pnl,"
        "         1 AS n FROM settlements"
        ") GROUP BY d ORDER BY d DESC LIMIT ?",
        (days,),
    )


# --------------------------------------------------------------- pagination

def _page_bounds(page: int, per_page: int) -> tuple[int, int]:
    page = max(1, int(page or 1))
    per_page = max(1, min(500, int(per_page or PAGE_DEFAULT)))
    return (page - 1) * per_page, per_page


def fills_page(conn: sqlite3.Connection | None, *, side: str = "",
               fill_type: str = "", target: str = "",
               start_ts: int | None = None,
               end_ts: int | None = None, page: int = 1,
               per_page: int = PAGE_DEFAULT) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if side in ("BUY", "SELL"):
        where.append("f.side = ?")
        params.append(side)
    if fill_type == "manual_sell":
        where.append("f.kind = 'MANUAL'")
    elif fill_type == "risk":
        where.append("f.kind = 'RISK'")
    elif fill_type == "copy_buy":
        where.append(
            "f.kind NOT IN ('MANUAL', 'RISK') AND f.side = 'BUY'")
    elif fill_type == "copy_sell":
        where.append(
            "f.kind NOT IN ('MANUAL', 'RISK') AND f.side = 'SELL'")
    if target:
        where.append("f.target = ?")
        params.append(target)
    if start_ts:
        where.append("f.ts >= ?")
        params.append(int(start_ts))
    if end_ts:
        where.append("f.ts < ?")
        params.append(int(end_ts))
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    offset, limit = _page_bounds(page, per_page)
    total = int(_scalar(conn, "SELECT COUNT(*) FROM fills f" + clause, params))
    rows = _all(
        conn,
        "SELECT f.*, p.title AS title, p.event_slug AS event_slug,"
        " p.slug AS market_slug"
        " FROM fills f LEFT JOIN positions p ON p.target = f.target"
        " AND p.condition_id = f.condition_id AND p.token_id = f.token_id"
        + clause + " ORDER BY f.ts DESC, f.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    sums = _one(
        conn,
        "SELECT COALESCE(SUM(f.notional),0) AS notional,"
        " COALESCE(SUM(f.fee),0) AS fee,"
        " COALESCE(SUM(f.realized_pnl),0) AS realized,"
        " COALESCE(SUM(CASE WHEN f.side='BUY' THEN f.notional END),0) AS buy_notional,"
        " COALESCE(SUM(CASE WHEN f.side='SELL' THEN f.notional END),0) AS sell_notional"
        " FROM fills f" + clause,
        params,
    ) or {}
    return {
        "rows": rows, "total": total, "page": page, "per_page": limit,
        "offset": offset, "sums": sums,
    }


def positions_page(conn: sqlite3.Connection | None, *, status: str = "open",
                   page: int = 1, per_page: int = PAGE_DEFAULT,
                   target: str = "") -> dict[str, Any]:
    status = status if status in ("open", "closed", "all") else "open"
    clauses: list[str] = []
    params: list[Any] = []
    if status == "open":
        clauses.append("p.status = 'OPEN'")
    elif status == "closed":
        clauses.append("p.status = 'CLOSED'")
    if target:
        clauses.append("p.target = ?")
        params.append(target)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    offset, limit = _page_bounds(page, per_page)
    total = int(_scalar(
        conn, "SELECT COUNT(*) FROM positions p" + where, params))
    rows = _all(
        conn,
        "SELECT p.*, m.bid AS mark_bid, m.ask AS mark_ask, m.mid AS mark_mid"
        " FROM positions p LEFT JOIN marks m ON m.token_id = p.token_id"
        + where + " ORDER BY p.updated_at DESC, p.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    totals = _one(
        conn,
        "SELECT COUNT(*) AS n, COALESCE(SUM(p.shares * p.avg_cost),0) AS cost,"
        " COALESCE(SUM(p.shares * COALESCE(m.mid, p.avg_cost)),0) AS value,"
        " COALESCE(SUM(p.shares * (COALESCE(m.mid, p.avg_cost) - p.avg_cost)),0)"
        " AS unrealized, COALESCE(SUM(p.realized_pnl),0) AS realized"
        " FROM positions p LEFT JOIN marks m ON m.token_id = p.token_id" + where,
        params,
    ) or {}
    return {
        "rows": rows, "total": total, "page": page, "per_page": limit,
        "offset": offset, "status": status, "target": target,
        "totals": totals,
    }


def signals_page(conn: sqlite3.Connection | None, *, copied: str = "",
                 reason: str = "", page: int = 1,
                 per_page: int = 100, target: str = "") -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if copied in ("1", "0"):
        where.append("s.copied = ?")
        params.append(int(copied))
    if reason:
        where.append("s.reason = ?")
        params.append(reason)
    if target:
        where.append("s.target = ?")
        params.append(target)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    offset, limit = _page_bounds(page, per_page)
    total = int(_scalar(conn, "SELECT COUNT(*) FROM signals s" + clause, params))
    rows = _all(
        conn,
        "SELECT s.*, tf.slug AS market_slug,"
        " tf.event_slug AS target_event_slug,"
        " tb.avg_buy_price AS target_avg_buy"
        " FROM signals s LEFT JOIN target_fills tf"
        " ON tf.dedupe_key = s.dedupe_key AND tf.target = s.target"
        " LEFT JOIN ("
        "   SELECT target, token_id,"
        "          SUM(usd) / NULLIF(SUM(size), 0) AS avg_buy_price"
        "   FROM target_fills WHERE side = 'BUY'"
        "   GROUP BY target, token_id"
        " ) tb ON tb.target = s.target AND tb.token_id = s.token_id" + clause
        + " ORDER BY s.ts DESC, s.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    return {
        "rows": rows, "total": total, "page": page, "per_page": limit,
        "offset": offset, "target": target,
    }


def active_pending_buys(conn: sqlite3.Connection | None,
                        target: str | None = None) -> list[dict[str, Any]]:
    where = ["status IN ('SUBMITTING','PENDING','CANCEL_REQUESTED',"
             "'TAIL_SUBMITTED')"]
    params: list[Any] = []
    if target:
        where.append("target = ?")
        params.append(target)
    return _all(
        conn,
        "SELECT * FROM pending_buys WHERE " + " AND ".join(where)
        + " ORDER BY id",
        params,
    )


def target_blocking_work(conn: sqlite3.Connection | None,
                         address: str) -> str | None:
    """Reason a target cannot be deleted, or None when it is safe."""
    checks = (
        ("positions", "SELECT COUNT(*) FROM positions"
                      " WHERE target = ? AND status = 'OPEN'", "未平仓持仓"),
        ("pending_buys", "SELECT COUNT(*) FROM pending_buys"
                         " WHERE target = ? AND status IN"
                         " ('SUBMITTING','PENDING','CANCEL_REQUESTED',"
                         "'TAIL_SUBMITTED')", "未完成 maker 挂单"),
        ("pending_exits", "SELECT COUNT(*) FROM pending_exits"
                          " WHERE target = ?", "待重试的卖出"),
        ("manual_sells", "SELECT COUNT(*) FROM manual_sells"
                         " WHERE target = ? AND status IN"
                         " ('PENDING','PARTIAL')", "待执行的手动卖出"),
        ("orders", "SELECT COUNT(*) FROM orders"
                   " WHERE target = ? AND mode = 'live' AND status IN"
                   " ('INTENT','PREPARED','SUBMITTED','PARTIAL')",
         "未完成的实盘订单"),
    )
    for _table, sql, label in checks:
        if int(_scalar(conn, sql, (address,))) > 0:
            return label
    return None


def signal_summary(conn: sqlite3.Connection | None) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT copied, reason, COUNT(*) AS n,"
        " COALESCE(SUM(our_notional),0) AS notional"
        " FROM signals GROUP BY copied, reason ORDER BY n DESC",
    )


def target_summary(conn: sqlite3.Connection | None) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT target, COUNT(*) AS fills, MAX(ts) AS last_ts"
        " FROM target_fills GROUP BY target ORDER BY fills DESC",
    )
