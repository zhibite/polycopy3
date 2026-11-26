#!/usr/bin/env python3
"""Analyze a Polymarket wallet probe dataset produced by fetch_wallet.py.

Tuned for wallets that quote rather than take: reconstructs the buy -> merge /
redeem cycle per market, not just round-trip sells.

Usage: python3 analyze_wallet.py probe_data
"""
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

UPDOWN_RE = re.compile(r"^([a-z0-9]+)-updown-(\d+)([mhd])-(\d+)$")
WINDOW_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def load_json(d, name, default=None):
    try:
        with open(os.path.join(d, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def load_jsonl(d, name):
    rows = []
    try:
        with open(os.path.join(d, name), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return rows


def q(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[k]


def fs(x):
    if x is None:
        return "-"
    ax = abs(x)
    if ax >= 1e9:
        return f"${x / 1e9:.2f}B"
    if ax >= 1e6:
        return f"${x / 1e6:.2f}M"
    if ax >= 1e3:
        return f"${x / 1e3:.1f}K"
    return f"${x:.2f}" if ax < 10 else f"${x:.0f}"


def fn(x, nd=2):
    return "-" if x is None else f"{x:,.{nd}f}"


def pct(x, nd=1):
    return "-" if x is None else f"{100 * x:.{nd}f}%"


def ts(t):
    if not t:
        return "-"
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d %H:%M")


def day(t):
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d")


def month(t):
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m")


def updown_info(slug):
    m = UPDOWN_RE.match(slug or "")
    if not m:
        return None
    start = int(m.group(4))
    window = int(m.group(2)) * WINDOW_SECONDS[m.group(3)]
    return m.group(1).upper(), f"{m.group(2)}{m.group(3)}", start + window


def norm_activity(r):
    size = float(r.get("size") or 0)
    price = float(r.get("price") or 0)
    usd = r.get("usdc_size")
    usd = float(usd) if usd is not None else size * price
    if (r.get("type") or "").upper() == "REDEEM":
        usd = float(r.get("usdc_size") or 0)
    return {
        "ts": int(r.get("timestamp") or 0),
        "cond": r.get("condition_id") or "",
        "type": (r.get("type") or "").upper(),
        "size": size,
        "usd": usd,
        "price": price,
        "token": r.get("token_id") or "",
        "side": (r.get("side") or "").upper(),
        "oi": r.get("outcome_index"),
        "title": r.get("title") or "",
        "slug": r.get("slug") or "",
        "event": r.get("event_slug") or "",
        "outcome": r.get("outcome") or "",
    }


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "probe_data"
    now = int(time.time())

    meta = load_json(d, "meta.json", {}) or {}
    stats = load_json(d, "user_stats.json", {}) or {}
    vol = load_json(d, "user_volume.json", {}) or {}
    board = load_json(d, "leaderboard.json", {}) or {}
    pnl_hist = load_json(d, "pnl_history.json", {}) or {}
    event_meta = load_json(d, "event_meta.json", {}) or {}
    closed_pos = load_jsonl(d, "positions_closed.jsonl")
    open_pos = load_jsonl(d, "positions_open.jsonl")
    redeem_pos = load_jsonl(d, "positions_redeemable.jsonl")
    merge_pos = load_jsonl(d, "positions_mergeable.jsonl")
    activity = [norm_activity(r) for r in load_jsonl(d, "activity.jsonl")]
    trades = [r for r in activity if r["type"] == "TRADE"]
    buys = [r for r in trades if r["side"] == "BUY"]
    sells = [r for r in trades if r["side"] == "SELL"]
    merges = [r for r in activity if r["type"] == "MERGE"]
    redeems = [r for r in activity if r["type"] == "REDEEM"]

    total_volume = sum(r["usd"] for r in trades)
    buy_volume = sum(r["usd"] for r in buys)
    sell_volume = sum(r["usd"] for r in sells)
    ts_trades = [r["ts"] for r in trades if r["ts"]]
    first_ts, last_ts = (min(ts_trades), max(ts_trades)) if ts_trades else (None, None)
    span_days = (last_ts - first_ts) / 86400 if ts_trades else 0

    print("=" * 84)
    print(f"POLYMARKET WALLET PROFILE   {meta.get('address', '')}")
    print("=" * 84)
    print(f"join date                     : {ts(stats.get('join_date'))}")
    print(f"activity window fetched       : {ts(first_ts)}  ..  {ts(last_ts)}  ({span_days:.0f} days)")
    print(f"last trade                    : {ts(last_ts)} ({(now - last_ts) / 86400 if last_ts else float('nan'):.2f} days ago)")
    print(f"rows fetched                  : activity={len(activity)}  closed_pos={len(closed_pos)}  "
          f"open={len(open_pos)} redeemable={len(redeem_pos)} mergeable={len(merge_pos)}")
    print(f"activity truncated            : {meta.get('truncated')}")

    # ------------------------------------------------------------- all-time
    apnl = stats.get("all_time_pnl") or {}
    print("\n--- ALL-TIME ACCOUNT " + "-" * 61)
    print(f"  volume (shares)             : {fn(apnl.get('volume') or vol.get('volume'), 0)}   "
          f"one-side USD {fs(apnl.get('volume_usdc') or vol.get('volume_usdc'))}")
    print(f"  trade count (stats/volume)  : {stats.get('trades')} / {apnl.get('trade_count') or vol.get('trade_count')}")
    print(f"  realized market PnL         : {fs(apnl.get('realized_market_pnl'))}")
    print(f"  unrealized PnL (now)        : {fs(apnl.get('unrealized_pnl'))}")
    print(f"  maker rebate                : {fs(apnl.get('maker_rebate'))}")
    print(f"  taker rebate                : {fs(apnl.get('taker_rebate'))}")
    print(f"  reward income               : {fs(apnl.get('reward_income'))}")
    print(f"  fees paid                   : {fs(apnl.get('fees'))}")
    print(f"  economic PnL (all-in)       : {fs(apnl.get('economic_pnl'))}")
    print(f"  biggest single win          : {fs(stats.get('biggest_win'))}")
    volume_usd = apnl.get("volume_usdc") or vol.get("volume_usdc")
    if volume_usd:
        print(f"  economic PnL / one-side USD : "
              f"{pct((apnl.get('economic_pnl') or 0) / volume_usd, 3)}")
    for period in ("day", "week", "month", "all"):
        b = (board.get(period) or {}).get("data") or {}
        if b:
            print(f"  leaderboard {period:<5}          : rank_pnl {b.get('rank_pnl')}  "
                  f"rank_volume {b.get('rank_volume')}  name {b.get('user_name')}")

    # ------------------------------------------------------------- pnl curve
    maxpnl = pnl_hist.get("max") or {}
    pts = maxpnl.get("points") or []
    if pts:
        print("\n--- PnL CURVE (daily, cumulative) " + "-" * 47)
        vals = [float(p.get("economic_pnl") or 0) for p in pts]
        peak, mdd = vals[0], 0.0
        for v in vals:
            peak = max(peak, v)
            mdd = min(mdd, v - peak)
        print(f"  first / last point          : {ts(pts[0].get('timestamp'))} {fs(vals[0])}  ..  "
              f"{ts(pts[-1].get('timestamp'))} {fs(vals[-1])}")
        print(f"  peak / max drawdown         : {fs(peak)} / {fs(mdd)}")
        by_month = {}
        for p in pts:
            by_month[month(p["timestamp"])] = p
        print("  monthly economic PnL / cumulative shares / cumulative trades:")
        prev = None
        for mth in sorted(by_month):
            p = by_month[mth]
            cur = float(p.get("economic_pnl") or 0)
            d_pnl = cur - prev if prev is not None else cur
            print(f"    {mth}  pnl {fs(d_pnl):>10}   shares {fn(p.get('volume'), 0):>13}   "
                  f"trades {p.get('trade_count')}")
            prev = cur

        def value_days_ago(days):
            cutoff = now - days * 86400
            best = None
            for p in pts:
                if p.get("timestamp", 0) <= cutoff:
                    best = p
            return best
        for days in (7, 30, 90):
            base = value_days_ago(days)
            if base:
                delta = vals[-1] - float(base.get("economic_pnl") or 0)
                print(f"  last {days}d economic PnL      : {fs(delta)}")

    # ------------------------------------------------------------- activity mix
    print("\n--- ACTIVITY MIX " + "-" * 66)
    type_counts = Counter(r["type"] for r in activity)
    type_usd = defaultdict(float)
    for r in activity:
        type_usd[r["type"]] += r["usd"]
    for t, c in type_counts.most_common():
        print(f"  {t:<12} {c:>8} rows   {fs(type_usd[t]):>12}")
    print(f"  TRADE sides                 : BUY {len(buys)} / SELL {len(sells)}"
          f"{'   <-- never sells' if not sells else ''}")

    # ------------------------------------------------------------- exits
    print("\n--- EXIT MECHANISM " + "-" * 63)
    merge_usd = sum(r["usd"] for r in merges)
    merge_sh = sum(r["size"] for r in merges)
    redeem_usd = sum(r["usd"] for r in redeems)
    redeem_win = [r for r in redeems if r["usd"] > 0]
    redeem_loss = [r for r in redeems if r["usd"] <= 0]
    print(f"  MERGE                       : {len(merges)} rows, {fn(merge_sh, 0)} pairs -> {fs(merge_usd)} "
          f"(avg {fs(merge_usd / merge_sh if merge_sh else None)}/share)")
    print(f"  REDEEM                      : {len(redeems)} rows -> {fs(redeem_usd)}   "
          f"winning {len(redeem_win)} ({fs(sum(r['size'] for r in redeem_win))} sh), "
          f"worthless {len(redeem_loss)} ({fs(sum(r['size'] for r in redeem_loss))} sh)")
    exit_usd = merge_usd + redeem_usd
    print(f"  exit mix by USD             : merge {pct(merge_usd / exit_usd if exit_usd else None)} / "
          f"redeem {pct(redeem_usd / exit_usd if exit_usd else None)}")
    print(f"  buy USD vs exits (this window): buys {fs(buy_volume)}  exits {fs(exit_usd)}  "
          f"gross PnL {fs(exit_usd - buy_volume)}")

    # ------------------------------------------------------------- per-condition reconstruction
    cond = {}

    def cslot(c):
        s = cond.get(c)
        if s is None:
            s = {
                "up_usd": 0.0, "up_sh": 0.0, "dn_usd": 0.0, "dn_sh": 0.0,
                "buy_usd": 0.0, "buy_sh": 0.0, "merge_usd": 0.0, "merge_sh": 0.0,
                "red_usd": 0.0, "red_sh": 0.0, "red_zero_sh": 0.0,
                "first_buy": None, "first_merge": None, "first_red": None,
                "last_ts": 0, "slug": "", "event": "", "title": "",
            }
            cond[c] = s
        return s

    for r in activity:
        if not r["cond"]:
            continue
        s = cslot(r["cond"])
        s["last_ts"] = max(s["last_ts"], r["ts"])
        if r["title"]:
            s["title"] = r["title"]
        if r["slug"]:
            s["slug"] = r["slug"]
        if r["event"]:
            s["event"] = r["event"]
        if r["type"] == "TRADE" and r["side"] == "BUY":
            s["buy_usd"] += r["usd"]
            s["buy_sh"] += r["size"]
            if r["oi"] == 0:
                s["up_usd"] += r["usd"]
                s["up_sh"] += r["size"]
            elif r["oi"] == 1:
                s["dn_usd"] += r["usd"]
                s["dn_sh"] += r["size"]
            if s["first_buy"] is None:
                s["first_buy"] = r["ts"]
        elif r["type"] == "MERGE":
            s["merge_usd"] += r["usd"]
            s["merge_sh"] += r["size"]
            if s["first_merge"] is None:
                s["first_merge"] = r["ts"]
        elif r["type"] == "REDEEM":
            s["red_usd"] += r["usd"]
            s["red_sh"] += r["size"]
            if r["usd"] <= 0:
                s["red_zero_sh"] += r["size"]
            if s["first_red"] is None:
                s["first_red"] = r["ts"]

    both = [s for s in cond.values() if s["up_sh"] > 0 and s["dn_sh"] > 0]
    fill_counts = Counter(r["cond"] for r in trades)
    print("\n--- MARKET-MAKING SIGNATURE " + "-" * 54)
    print(f"  distinct conditions         : {len(cond)}")
    print(f"  fills per condition         : median {fn(q(list(fill_counts.values()), .5), 0)}  "
          f"p90 {fn(q(list(fill_counts.values()), .9), 0)}  "
          f"max {max(fill_counts.values()) if fill_counts else 0}")
    if cond:
        print(f"  bought BOTH outcomes        : {len(both)} ({pct(len(both) / len(cond))})")
    # combined price for markets where both sides bought
    combos = []
    pair_usd = 0.0
    pair_sh = 0.0
    for s in both:
        cu = s["up_usd"] / s["up_sh"] if s["up_sh"] else None
        cd = s["dn_usd"] / s["dn_sh"] if s["dn_sh"] else None
        if cu is not None and cd is not None:
            combos.append(cu + cd)
            pairs = min(s["up_sh"], s["dn_sh"])
            pair_usd += (cu + cd) * pairs
            pair_sh += pairs
    if combos:
        print(f"  combined pair cost (VWAP)   : p10 {fn(q(combos, .1), 4)}  p25 {fn(q(combos, .25), 4)}  "
              f"median {fn(q(combos, .5), 4)}  p75 {fn(q(combos, .75), 4)}  p90 {fn(q(combos, .9), 4)}")
        print(f"  volume-weighted pair cost   : {fn(pair_usd / pair_sh, 4)}  "
              f"(edge {pct(1 - pair_usd / pair_sh, 3)} of notional at merge)")
        for cutoff in (0.95, 0.98, 0.99, 1.0, 1.01):
            n = sum(1 for c in combos if c < cutoff)
            print(f"    pair cost < {cutoff:<5}          : {pct(n / len(combos))}")
    # merge coverage vs theoretical pairs
    theoretical_pairs = sum(min(s["up_sh"], s["dn_sh"]) for s in both)
    merged_sh = sum(s["merge_sh"] for s in both)
    print(f"  pairs available / merged    : {fn(theoretical_pairs, 0)} / {fn(merged_sh, 0)} "
          f"({pct(merged_sh / theoretical_pairs if theoretical_pairs else None)})")
    # realized PnL estimate from activity
    est = [(s["merge_usd"] + s["red_usd"] - s["buy_usd"], s["buy_usd"], s) for s in cond.values() if s["buy_usd"] > 0]
    if est:
        pnls = [x[0] for x in est]
        vol_sum = sum(x[1] for x in est)
        print(f"  activity-window est. PnL    : {fs(sum(pnls))}  on {fs(vol_sum)} bought  "
              f"({pct(sum(pnls) / vol_sum if vol_sum else None, 3)})")
        print(f"  per-market est. PnL         : median {fs(q(pnls, .5))}  p10 {fs(q(pnls, .1))}  "
              f"p90 {fs(q(pnls, .9))}  min {fs(min(pnls))}  max {fs(max(pnls))}")
        pos_mk = sum(1 for x in pnls if x > 0)
        print(f"  profitable markets          : {pct(pos_mk / len(pnls))}")
        # PnL by pair-cost bucket
        print("  est. PnL by combined pair cost bucket:")
        for lo, hi, lab in ((0, .95, "<0.95"), (.95, .98, "0.95-0.98"), (.98, 1.0, "0.98-1.00"),
                            (1.0, 1.02, "1.00-1.02"), (1.02, 99, ">1.02")):
            sel = []
            for s in both:
                if not (s["up_sh"] and s["dn_sh"]):
                    continue
                c = s["up_usd"] / s["up_sh"] + s["dn_usd"] / s["dn_sh"]
                if lo <= c < hi:
                    sel.append(s)
            if sel:
                n = len(sel)
                p = sum(x["merge_usd"] + x["red_usd"] - x["buy_usd"] for x in sel)
                print(f"    {lab:<9} n={n:>6}  PnL {fs(p):>10}  "
                      f"avg {fs(p / n)}/market")
        # merge / redeem timing
        merge_delays = [(s["first_merge"] - s["first_buy"]) / 60.0 for s in cond.values()
                        if s["first_merge"] and s["first_buy"]]
        if merge_delays:
            print(f"  first buy -> first merge    : median {fn(q(merge_delays, .5), 1)}m  "
                  f"p25 {fn(q(merge_delays, .25), 1)}m  p75 {fn(q(merge_delays, .75), 1)}m  "
                  f"p90 {fn(q(merge_delays, .9), 1)}m")
        end_delays = []
        for s in cond.values():
            if not s["first_red"]:
                continue
            info = updown_info(s["slug"])
            if info:
                end_delays.append((s["first_red"] - info[2]) / 60.0)
        if end_delays:
            print(f"  market end -> redeem        : median {fn(q(end_delays, .5), 1)}m  "
                  f"p90 {fn(q(end_delays, .9), 1)}m")

    # ------------------------------------------------------------- frequency / size
    print("\n--- FILL FREQUENCY & SIZE " + "-" * 55)
    active_days = sorted({day(t) for t in ts_trades})
    per_day = Counter(day(t) for t in ts_trades)
    cal_days = max(1.0, span_days)
    if per_day:
        pc = list(per_day.values())
        print(f"  active days                 : {len(active_days)} / {cal_days:.0f} calendar days "
              f"({100 * len(active_days) / cal_days:.1f}%)")
        print(f"  fills per active day        : median {fn(q(pc, .5), 0)}  p90 {fn(q(pc, .9), 0)}  max {max(pc)}")
        print(f"  avg fills/day overall       : {len(trades) / cal_days:.1f}")
        print(f"  busiest days                : " + ", ".join(f"{dd} ({c})" for dd, c in per_day.most_common(5)))
    notionals = [r["usd"] for r in trades if r["usd"] > 0]
    share_sizes = [r["size"] for r in trades if r["size"] > 0]
    for label, arr, unit in (("fill notional", notionals, "$"), ("fill shares", share_sizes, "")):
        if arr:
            print(f"  {label:<27}: p10 {unit}{fn(q(arr, .1)):>9} p25 {unit}{fn(q(arr, .25)):>9} "
                  f"p50 {unit}{fn(q(arr, .5)):>9} p75 {unit}{fn(q(arr, .75)):>9} "
                  f"p90 {unit}{fn(q(arr, .9)):>9} p99 {unit}{fn(q(arr, .99)):>9} max {unit}{fn(max(arr)):>9}")
    if notionals:
        tot = len(notionals)
        for lo, hi, label in ((0, 10, "<$10"), (10, 50, "$10-50"), (50, 100, "$50-100"),
                              (100, 500, "$100-500"), (500, 1000, "$500-1K"),
                              (1000, 5000, "$1K-5K"), (5000, float("inf"), ">$5K")):
            n = sum(1 for v in notionals if lo <= v < hi)
            print(f"    {label:<9} {n:>8} ({100 * n / tot:>5.1f}%)   volume "
                  f"{fs(sum(v for v in notionals if lo <= v < hi))}")
    rounded = Counter(round(v) for v in notionals if v >= 1)
    print("  most repeated fill notional : " + ", ".join(f"${k} x{c}" for k, c in rounded.most_common(6)))

    # ------------------------------------------------------------- price levels
    print("\n--- BUY PRICE LEVELS " + "-" * 62)
    buckets = [(0, .05, "0-5c"), (.05, .15, "5-15c"), (.15, .30, "15-30c"),
               (.30, .50, "30-50c"), (.50, .70, "50-70c"), (.70, .85, "70-85c"),
               (.85, .95, "85-95c"), (.95, 1.01, "95-100c")]
    bv = defaultdict(float)
    bn = Counter()
    for r in buys:
        for lo, hi, label in buckets:
            if lo <= r["price"] < hi:
                bv[label] += r["usd"]
                bn[label] += 1
                break
    tot = sum(bv.values())
    if tot > 0:
        bmax = max(bv.values())
        for _, _, label in buckets:
            bar = "#" * int(round(28 * bv[label] / bmax)) if bmax else ""
            print(f"    {label:<10} {bn[label]:>8}  {fs(bv[label]):>9} ({100 * bv[label] / tot:>5.1f}%) {bar}")
    if buys:
        print(f"  share-weighted avg buy px   : "
              f"{sum(r['usd'] for r in buys) / max(1e-9, sum(r['size'] for r in buys)):.4f}")

    # ------------------------------------------------------------- timing
    print("\n--- WHEN THEY QUOTE (UTC) " + "-" * 57)
    hours = Counter(datetime.fromtimestamp(t, timezone.utc).hour for t in ts_trades)
    hmax = max(hours.values()) if hours else 0
    for h in range(24):
        c = hours.get(h, 0)
        bar = "#" * int(round(56 * c / hmax)) if hmax else ""
        print(f"  {h:02d}:00 {c:>7} {bar}")
    wd_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    wd = Counter(datetime.fromtimestamp(t, timezone.utc).weekday() for t in ts_trades)
    print("  weekday: " + "  ".join(f"{wd_name[k]} {wd.get(k, 0)}" for k in range(7)))

    # ------------------------------------------------------------- time to resolution
    ttres = defaultdict(list)
    for r in buys:
        info = updown_info(r["slug"])
        if info:
            ttres[info[1]].append((info[2] - r["ts"]) / 60.0)
    print("\n--- TIME FROM BUY FILL TO MARKET END " + "-" * 43)
    if ttres:
        for window, mins in sorted(ttres.items(), key=lambda kv: -len(kv[1])):
            dist = "  ".join(
                f"{lab} {100 * sum(1 for m in mins if lo <= m < hi) / len(mins):.0f}%"
                for lo, hi, lab in ((0, .5, "<30s"), (.5, 1, "30-60s"), (1, 2, "1-2m"),
                                    (2, 4, "2-4m"), (4, 10, "4-10m"), (10, 60, "10-60m"),
                                    (60, float("inf"), ">60m")))
            print(f"  {window:<4} n={len(mins):>7}  median {fn(q(mins, .5), 1)}m  "
                  f"p10 {fn(q(mins, .1), 1)}m  p90 {fn(q(mins, .9), 1)}m")
            print(f"       {dist}")
    else:
        print("  no up/down slug pattern matched")

    # ------------------------------------------------------------- market mix
    print("\n--- MARKET MIX " + "-" * 67)

    def catkey(slug, event_slug=""):
        info = updown_info(slug)
        if info:
            return f"{info[0]} up/down {info[1]}"
        md = event_meta.get(event_slug) or {}
        tags = md.get("tags") or []
        if tags:
            return "other: " + "/".join(tags[:2])
        return "other: " + ((event_slug or "?").split("-")[0])

    cat_stats = defaultdict(lambda: {"fills": 0, "vol": 0.0})
    for r in trades:
        k = catkey(r["slug"], r["event"])
        cat_stats[k]["fills"] += 1
        cat_stats[k]["vol"] += r["usd"]
    for key, s in sorted(cat_stats.items(), key=lambda kv: -kv[1]["vol"])[:12]:
        print(f"  {key[:32]:<32} fills {s['fills']:>8}  volume {fs(s['vol']):>10} "
              f"({pct(s['vol'] / (total_volume or 1))})")
    closed_cat = defaultdict(list)
    for p in closed_pos:
        closed_cat[catkey(p.get("slug") or "", p.get("event_slug") or "")].append(
            float(p.get("realized_pnl") or 0))
    print("  closed-position PnL by category:")
    for key, pnls in sorted(closed_cat.items(), key=lambda kv: -sum(kv[1]))[:10]:
        wins = sum(1 for x in pnls if x > 0)
        print(f"    {key[:32]:<32} n={len(pnls):>7}  pnl {fs(sum(pnls)):>10}  win {pct(wins / len(pnls))}")
    ev_stats = defaultdict(lambda: {"n": 0, "vol": 0.0})
    for r in trades:
        ev_stats[r["event"]]["n"] += 1
        ev_stats[r["event"]]["vol"] += r["usd"]
    ev_rank = sorted(ev_stats.items(), key=lambda kv: -kv[1]["vol"])
    print(f"  distinct events             : {len(ev_rank)}")
    print("  top 8 events by volume:")
    for slug, s in ev_rank[:8]:
        print(f"    {pct(s['vol'] / (total_volume or 1)):>6} {fs(s['vol']):>10}  fills {s['n']:>6}  {slug[:52]}")
    if ev_rank:
        top3 = sum(s["vol"] for _, s in ev_rank[:3])
        print(f"  top-3 / top-10 event share  : {pct(top3 / (total_volume or 1))} / "
              f"{pct(sum(s['vol'] for _, s in ev_rank[:10]) / (total_volume or 1))}")

    # ------------------------------------------------------------- closed positions
    print("\n--- CLOSED POSITIONS: TOKEN LEVEL " + "-" * 47)
    holds = []
    times_to_end = []
    if closed_pos:
        pnls = [float(p.get("realized_pnl") or 0) for p in closed_pos]
        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x <= 0]
        rois = []
        per_share = []
        for p in closed_pos:
            cost = float(p.get("total_cost_usdc") or 0) or (
                float(p.get("avg_price") or 0) * float(p.get("total_size") or 0))
            pnl = float(p.get("realized_pnl") or 0)
            if cost > 0:
                rois.append(pnl / cost)
            if float(p.get("total_size") or 0) > 0:
                per_share.append(pnl / float(p["total_size"]))
            t0, t1 = p.get("first_entry_at"), p.get("last_event_at")
            if t0 and t1:
                holds.append((int(t1) - int(t0)) / 3600)
            info = updown_info(p.get("slug") or "")
            if info and t0:
                times_to_end.append((info[2] - int(t0)) / 60.0)
        print(f"  closed token positions      : {len(closed_pos)}")
        print(f"  win rate (pnl>0)            : {pct(len(wins) / len(pnls))}  ({len(wins)}W / {len(losses)}L)")
        print(f"  total realized PnL          : {fs(sum(pnls))}")
        print(f"  avg / median win            : {fs(sum(wins) / len(wins) if wins else 0)} / {fs(q(wins, .5))}")
        print(f"  avg / median loss           : {fs(sum(losses) / len(losses) if losses else 0)} / {fs(q(losses, .5))}")
        if losses and sum(losses) < 0:
            print(f"  profit factor               : {fn(sum(wins) / abs(sum(losses)), 2)}")
        print(f"  ROI per token               : p25 {pct(q(rois, .25))}  median {pct(q(rois, .5))}  "
              f"p75 {pct(q(rois, .75))}  p90 {pct(q(rois, .9))}")
        print(f"  PnL per share               : median {fs(q(per_share, .5))}  p10 {fs(q(per_share, .1))}  "
              f"p90 {fs(q(per_share, .9))}")
        print(f"  hold hours (first->last)    : median {fn(q(holds, .5), 1)}  p25 {fn(q(holds, .25), 1)}  "
              f"p75 {fn(q(holds, .75), 1)}  p90 {fn(q(holds, .9), 1)}")
        hb = [(0, 2 / 60, "<2m"), (2 / 60, 5 / 60, "2-5m"), (5 / 60, 1, "5-60m"),
              (1, 6, "1-6h"), (6, 24, "6-24h"), (24, 168, "1-7d"), (168, float("inf"), ">7d")]
        if holds:
            print("  hold buckets                : " + "  ".join(
                f"{lab} {100 * sum(1 for h in holds if lo <= h < hi) / len(holds):.0f}%"
                for lo, hi, lab in hb))
        if times_to_end:
            print(f"  entry->market end (updown)  : median {fn(q(times_to_end, .5), 1)}m  "
                  f"p10 {fn(q(times_to_end, .1), 1)}m  p90 {fn(q(times_to_end, .9), 1)}m")
        dust = sum(1 for x in pnls if abs(x) < 1)
        print(f"  dust positions (|PnL|<$1)   : {dust} ({pct(dust / len(pnls))})")
        print("  best 5:")
        for p in sorted(closed_pos, key=lambda p: float(p.get("realized_pnl") or 0), reverse=True)[:5]:
            print(f"    {fs(float(p.get('realized_pnl') or 0)):>10}  {p.get('outcome') or '':<5} "
                  f"{float(p.get('avg_price') or 0):.3f}  {(p.get('title') or '')[:48]}")
        print("  worst 5:")
        for p in sorted(closed_pos, key=lambda p: float(p.get("realized_pnl") or 0))[:5]:
            print(f"    {fs(float(p.get('realized_pnl') or 0)):>10}  {p.get('outcome') or '':<5} "
                  f"{float(p.get('avg_price') or 0):.3f}  {(p.get('title') or '')[:48]}")

    # ------------------------------------------------------------- closed positions: market level
    print("\n--- CLOSED POSITIONS: MARKET LEVEL (all history) " + "-" * 33)
    mkt = {}
    for p in closed_pos:
        c = p.get("condition_id") or ""
        if not c:
            continue
        s = mkt.get(c)
        if s is None:
            s = {"pnl": 0.0, "n": 0, "px": {}, "size": 0.0, "first": None, "last": None,
                 "slug": p.get("slug") or "", "month": None}
            mkt[c] = s
        s["pnl"] += float(p.get("realized_pnl") or 0)
        s["n"] += 1
        s["size"] += float(p.get("total_size") or 0)
        oi = p.get("outcome_index")
        ap = float(p.get("avg_price") or 0)
        if oi in (0, 1) and ap > 0:
            s["px"][oi] = ap
        t0, t1 = p.get("first_entry_at"), p.get("last_event_at")
        if t0:
            s["first"] = int(t0) if s["first"] is None else min(s["first"], int(t0))
        if t1:
            s["last"] = int(t1) if s["last"] is None else max(s["last"], int(t1))
            s["month"] = month(int(t1))
    print(f"  distinct markets            : {len(mkt)}")
    if mkt:
        two = [s for s in mkt.values() if len(s["px"]) >= 2]
        mp = [s["pnl"] for s in mkt.values()]
        mp_pos = sum(1 for x in mp if x > 0)
        print(f"  markets with both outcomes  : {len(two)} ({pct(len(two) / len(mkt))})")
        print(f"  net PnL per market          : median {fs(q(mp, .5))}  p10 {fs(q(mp, .1))}  "
              f"p90 {fs(q(mp, .9))}  min {fs(min(mp))}  max {fs(max(mp))}")
        print(f"  profitable markets          : {pct(mp_pos / len(mp))}   total {fs(sum(mp))}")
        if two:
            combos_all = [sum(s["px"].values()) for s in two]
            print(f"  combined avg price (both)   : p10 {fn(q(combos_all, .1), 4)}  "
                  f"median {fn(q(combos_all, .5), 4)}  p90 {fn(q(combos_all, .9), 4)}")
            for cutoff in (0.95, 0.98, 0.99, 1.0, 1.01):
                n = sum(1 for c in combos_all if c < cutoff)
                print(f"    combined < {cutoff:<5}         : {pct(n / len(combos_all))}")
            print("  market PnL by combined price bucket:")
            for lo, hi, lab in ((0, .95, "<0.95"), (.95, .98, "0.95-0.98"), (.98, 1.0, "0.98-1.00"),
                                (1.0, 1.02, "1.00-1.02"), (1.02, 99, ">1.02")):
                sel = [s for s in two if lo <= sum(s["px"].values()) < hi]
                if sel:
                    p = sum(s["pnl"] for s in sel)
                    print(f"    {lab:<9} n={len(sel):>7}  PnL {fs(p):>11}  avg {fs(p / len(sel))}/market")
        # monthly market-level PnL (close time)
        monthly = defaultdict(float)
        for s in mkt.values():
            if s["month"]:
                monthly[s["month"]] += s["pnl"]
        print("  monthly PnL by position close month:")
        print("    " + "  ".join(f"{m}:{fs(v)}" for m, v in sorted(monthly.items())[-12:]))

    # ------------------------------------------------------------- open book
    print("\n--- OPEN BOOK " + "-" * 71)

    def book_summary(rows, label):
        val = sum(float(p.get("current_value") or 0) for p in rows)
        unrl = sum(float(p.get("unrealized_pnl") or 0) for p in rows)
        cost = sum(float(p.get("total_cost_usdc") or 0) for p in rows)
        print(f"  {label:<12} n={len(rows):>4}  current {fs(val):>9}  cost {fs(cost):>9}  "
              f"unrealized {fs(unrl):>9}")
        return val, unrl

    ov, ou = book_summary(open_pos, "open")
    rv, _ = book_summary(redeem_pos, "redeemable")
    mv, _ = book_summary(merge_pos, "mergeable")
    print(f"  total unrealized PnL        : {fs(ou)}")
    print(f"  redeemable value (unclaimed): {fs(rv)}   mergeable {fs(mv)}")
    if open_pos:
        big = sorted(open_pos, key=lambda p: float(p.get("current_value") or 0), reverse=True)[:8]
        print("  largest open positions:")
        for p in big:
            print(f"    {fs(float(p.get('current_value') or 0)):>9}  avg {float(p.get('avg_price') or 0):.3f} "
                  f"-> {float(p.get('current_price') or 0):.3f}  {(p.get('outcome') or '')[:5]:<5} "
                  f"{(p.get('title') or '')[:42]}")

    # ------------------------------------------------------------- maker/taker
    print("\n--- MAKER / TAKER & FEES " + "-" * 56)
    c = meta.get("counts") or {}
    taker24, allf24 = c.get("taker_fills_24h"), c.get("all_fills_24h")
    if taker24 is not None and allf24:
        print(f"  last 24h fills              : all {allf24}  taker {taker24}  "
              f"maker {allf24 - taker24} ({pct((allf24 - taker24) / allf24)})")
    print(f"  fees paid (all time)        : {fs(apnl.get('fees'))}")
    print(f"  maker + taker rebates       : {fs((apnl.get('maker_rebate') or 0) + (apnl.get('taker_rebate') or 0))}")
    print(f"  rewards                     : {fs(apnl.get('reward_income'))}")
    if apnl.get("volume"):
        print(f"  fees / volume               : {pct((apnl.get('fees') or 0) / apnl['volume'], 3)}")
        print(f"  rebates / volume            : {pct(((apnl.get('maker_rebate') or 0) + (apnl.get('taker_rebate') or 0)) / apnl['volume'], 3)}")

    # ------------------------------------------------------------- copy readout
    print("\n--- COPY-TRADE RELEVANT NUMBERS " + "-" * 50)
    if notionals:
        print(f"  median fill notional        : {fs(q(notionals, .5))}    p90 {fs(q(notionals, .9))}")
        print(f"  fills < $100 / < $500       : {pct(sum(1 for v in notionals if v < 100) / len(notionals))} / "
              f"{pct(sum(1 for v in notionals if v < 500) / len(notionals))}")
    if per_day:
        print(f"  median fills / active day   : {fn(q(list(per_day.values()), .5), 0)}")
    if ev_rank:
        print(f"  top-3 event volume share    : {pct(sum(s['vol'] for _, s in ev_rank[:3]) / (total_volume or 1))}")
    if holds:
        print(f"  median hold                 : {fn(q(holds, .5), 2)} h")
    print(f"  median fills per market     : {fn(q(list(fill_counts.values()), .5), 0)}")
    print(f"  open book value             : {fs(ov)}  (plus redeemable {fs(rv)})")
    print(f"  last trade                  : {ts(last_ts)}")


if __name__ == "__main__":
    main()
