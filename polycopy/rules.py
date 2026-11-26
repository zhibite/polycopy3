"""Pure rule helpers: dedupe keys, activity normalisation, buy gates, sizing."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import TargetConfig


def parse_ts(value: Any) -> int | None:
    """Parse an ISO timestamp / epoch (s or ms) into UTC seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        if number > 1e12:
            number /= 1000.0
        return int(number)
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = 0.0
    if number > 0:
        if number > 1e12:
            number /= 1000.0
        return int(number)
    moment: datetime | None = None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def market_end_ts(market: dict[str, Any] | None) -> int | None:
    """Extract a market end/settlement timestamp from a CLOB/Gamma dict."""
    if not isinstance(market, dict):
        return None
    for key in ("end_ts", "endDate", "endDateIso", "end_date_iso",
                "end_date", "endDateISO"):
        stamp = parse_ts(market.get(key))
        if stamp is not None:
            return stamp
    return None


def dedupe_key(row: dict[str, Any]) -> str:
    # Deliberately identical for data-api and on-chain rows: the on-chain
    # decoder reproduces every field (tx, condition, token, side, size, price,
    # block timestamp), so running both sources into one DB will process a
    # fill once.  The per-process feed still dedupes on (tx_hash, log_index).
    return "|".join(
        [
            str(row.get("transaction_hash") or ""),
            str(row.get("type") or "").upper(),
            str(row.get("condition_id") or ""),
            str(row.get("token_id") or ""),
            str(row.get("side") or "").upper(),
            f"{to_float(row.get('size')):.8f}",
            f"{to_float(row.get('price')):.8f}",
            str(int(row.get("timestamp") or 0)),
        ]
    )


def target_usd(row: dict[str, Any]) -> float:
    value = row.get("usdc_size")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return to_float(row.get("size")) * to_float(row.get("price"))


def buy_skip_reason(t: TargetConfig, row: dict[str, Any], *, now: int,
                    stale_seconds: int, open_positions: int,
                    has_position: bool, market_usd: float,
                    end_ts: int | None = None) -> str | None:
    if not t.enabled:
        return "disabled"
    ts = int(row.get("timestamp") or 0)
    if now - ts > stale_seconds:
        return "stale"
    price = to_float(row.get("price"))
    usd = target_usd(row)
    event_slug = str(row.get("event_slug") or "").lower()
    slug = str(row.get("slug") or "").lower()

    if price < t.buy_price_min or price > t.buy_price_max:
        return "price_out_of_range"
    if usd < t.min_target_usd:
        return "target_too_small"
    if usd > t.max_target_usd:
        return "target_too_large"
    if t.min_hours_to_end > 0:
        if end_ts is None:
            return "expiry_unknown"
        if end_ts - now < int(t.min_hours_to_end * 3600):
            return "expiry_too_close"
    for needle in t.exclude_event_substrings:
        if needle and needle.lower() in event_slug:
            return "event_excluded"
    for needle in t.exclude_slug_substrings:
        if needle and needle.lower() in slug:
            return "slug_excluded"
    if not has_position and open_positions >= t.max_open_positions:
        return "max_open_positions"
    if market_usd >= t.max_market_usd:
        return "max_market_exposure"
    return None


def desired_notional(t: TargetConfig, usd: float) -> float:
    if t.sizing_mode == "fixed":
        desired = t.fixed_notional_usd
    elif t.sizing_mode == "scale":
        desired = usd * t.scale_factor
    else:
        raise ValueError(f"unknown sizing_mode {t.sizing_mode!r}")
    return max(0.0, min(desired, t.max_notional_usd))


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
