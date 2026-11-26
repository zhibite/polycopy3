"""Configuration loading (TOML, stdlib only)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class AccountConfig:
    starting_cash: float = 10_000.0


@dataclass
class PaperConfig:
    poll_seconds: int = 5
    overlap_seconds: int = 900
    stale_signal_seconds: int = 300
    fee_rate: float = 0.01          # per-market: monthly/weekly crypto 0.01, BTC 5m/15m 0.05
    extra_slippage: float = 0.0     # added to buy VWAP / subtracted from sell VWAP
    min_fill_ratio: float = 0.5     # skip when the book can only fill less than this share
    max_buy_drift: float = 0.03     # max price above the target's fill we will pay
    max_sell_drift: float = 0.10    # max price below the target's fill we will accept
    max_buy_drift_pct: float = 0.10   # relative cap for cheap tokens
    max_sell_drift_pct: float = 0.15  # relative cap for cheap tokens
    # Exits always follow the target: any bid is eligible, partial fills are
    # retried.  Entries keep the buy drift caps above.
    strict_exits: bool = True
    settle_every_seconds: int = 300
    mark_every_seconds: int = 300
    target_reconcile_seconds: int = 60
    risk_check_seconds: int = 30
    orphan_grace_seconds: float = 0.0
    mark_limit: int = 100
    max_pages: int = 3
    page_limit: int = 500
    cash_reserve_pct: float = 0.05
    min_shares: float = 1.0         # paper-only guard; live CLOB minimum is usually 5 shares
    request_timeout: int = 20
    request_retries: int = 4
    db_path: str = "data/paper.db"
    log_path: str = "data/paper.log"


@dataclass
class TargetConfig:
    address: str
    enabled: bool = True
    sizing_mode: str = "scale"      # "scale" | "fixed"
    scale_factor: float = 0.02      # our notional = target notional * scale_factor
    fixed_notional_usd: float = 10.0
    min_notional_usd: float = 2.0
    max_notional_usd: float = 100.0
    buy_price_min: float = 0.30
    buy_price_max: float = 0.70
    min_target_usd: float = 20.0
    max_target_usd: float = 1e12
    copy_sells: bool = True
    initial_lookback_seconds: int = 300
    max_market_usd: float = 300.0
    max_open_positions: int = 100
    fee_rate: float | None = None   # per-target override (e.g. 0.05 for BTC 5m/15m)
    min_hours_to_end: float = 0.0        # 0 = gate off; production 2.0
    maker_entry_enabled: bool = False    # live + non-dry-run only
    maker_timeout_seconds: int = 300
    fak_max_premium_pct: float = 0.02    # tail FAK cap = signal price * (1 + pct)
    take_profit_pct: float = 0.0         # 0.10 = +10%, 0 = off
    stop_loss_pct: float = 0.0           # 0.20 = -20%, 0 = off
    exclude_event_substrings: list[str] = field(default_factory=list)
    exclude_slug_substrings: list[str] = field(default_factory=list)


@dataclass
class RunConfig:
    """Which execution path the bot uses.

    Changing this value requires a bot restart; the engine refuses to hot-swap
    an executor because paper and live books are different books.
    """
    mode: str = "paper"             # "paper" | "live"

    def __post_init__(self) -> None:
        self.mode = str(self.mode or "paper").lower()
        if self.mode not in ("paper", "live"):
            raise ValueError(f"run.mode must be 'paper' or 'live', got {self.mode!r}")


@dataclass
class LiveConfig:
    """Live CLOB settings. Secrets are read from the environment, never TOML."""
    dry_run: bool = True            # true = compute/record orders, submit nothing
    private_key_env: str = "POLY_PRIVATE_KEY"
    funder: str = ""                # proxy/funder wallet that holds USDC / positions
    signature_type: int = 2         # 0 EOA, 1 email/Magic, 2 Safe proxy, 3 deposit wallet
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    max_order_usd: float = 500.0
    max_total_exposure_usd: float = 5_000.0
    max_daily_loss_usd: float = 500.0
    min_order_shares: float = 5.0   # Polymarket CLOB minimum is commonly 5 shares
    order_timeout_seconds: int = 30
    kill_switch_path: str = "data/LIVE_HALT"
    redeem: bool = True
    max_book_depth_pct: float = 0.25  # max share of visible book depth per order
    db_path: str = "data/live.db"     # never point this at the paper DB
    log_path: str = "data/live.log"

    def validate(self) -> None:
        self.dry_run = bool(self.dry_run)
        self.private_key_env = str(self.private_key_env or "").strip()
        self.funder = str(self.funder or "").strip()
        self.db_path = str(self.db_path or "").strip()
        self.log_path = str(self.log_path or "").strip()
        self.host = str(self.host or "").strip()
        if not self.db_path:
            raise ValueError("live.db_path must not be empty")
        if not self.log_path:
            raise ValueError("live.log_path must not be empty")
        if not self.host:
            raise ValueError("live.host must not be empty")
        self.chain_id = int(self.chain_id)
        if self.chain_id <= 0:
            raise ValueError("live.chain_id must be positive")
        if self.signature_type not in (0, 1, 2, 3):
            raise ValueError("live.signature_type must be 0, 1, 2 or 3")
        for name in ("max_order_usd", "max_total_exposure_usd",
                     "max_daily_loss_usd", "min_order_shares"):
            value = float(getattr(self, name))
            if value < 0:
                raise ValueError(f"live.{name} must be >= 0")
            setattr(self, name, value)
        if self.max_order_usd <= 0:
            raise ValueError("live.max_order_usd must be > 0")
        if self.order_timeout_seconds < 1:
            raise ValueError("live.order_timeout_seconds must be >= 1")
        if not (0.0 <= self.max_book_depth_pct <= 1.0):
            raise ValueError("live.max_book_depth_pct must be between 0 and 1")


@dataclass
class AppConfig:
    account: AccountConfig
    paper: PaperConfig
    targets: dict[str, TargetConfig]
    run: RunConfig = field(default_factory=RunConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    path: Path | None = None


def _filter_fields(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        # Tolerate keys added by a newer version (rolling deploy) instead of
        # crashing the dashboard/engine on an otherwise valid config file.
        import logging

        logging.getLogger("polycopy.config").warning(
            "ignoring unknown config keys for %s: %s",
            cls.__name__, sorted(unknown),
        )
    return {key: value for key, value in data.items() if key in allowed}


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    account = AccountConfig(**_filter_fields(AccountConfig, raw.get("account", {})))
    paper = PaperConfig(**_filter_fields(PaperConfig, raw.get("paper", {})))
    run = RunConfig(**_filter_fields(RunConfig, raw.get("run", {})))
    live = LiveConfig(**_filter_fields(LiveConfig, raw.get("live", {})))
    live.validate()

    if paper.risk_check_seconds < 5:
        raise ValueError("paper.risk_check_seconds must be >= 5")
    if not (0.0 <= float(paper.orphan_grace_seconds) <= 3600.0):
        raise ValueError("paper.orphan_grace_seconds must be between 0 and 3600")

    targets: dict[str, TargetConfig] = {}
    seen_addresses: dict[str, str] = {}
    for name, tdata in (raw.get("targets") or {}).items():
        if not isinstance(tdata, dict) or not tdata.get("address"):
            raise ValueError(f"target {name!r} is missing an address")
        data = dict(tdata)
        data["address"] = str(data["address"]).strip().lower()
        target = TargetConfig(**_filter_fields(TargetConfig, data))
        _validate_target(target, name)
        if target.address in seen_addresses:
            raise ValueError(
                f"duplicate target address {target.address}: "
                f"{seen_addresses[target.address]!r} and {name!r}")
        seen_addresses[target.address] = name
        targets[name] = target
    if not targets:
        raise ValueError("config must define at least one [targets.<name>]")

    return AppConfig(account=account, paper=paper, targets=targets,
                     run=run, live=live, path=path)


def _validate_target(target: TargetConfig, name: str) -> None:
    if target.sizing_mode not in ("scale", "fixed"):
        raise ValueError(f"target {name!r}: sizing_mode must be scale or fixed")
    if not (0.0 <= float(target.min_hours_to_end) <= 168.0):
        raise ValueError(f"target {name!r}: min_hours_to_end must be 0..168")
    if int(target.maker_timeout_seconds) < 1:
        raise ValueError(f"target {name!r}: maker_timeout_seconds must be >= 1")
    if not (0.0 <= float(target.fak_max_premium_pct) <= 0.5):
        raise ValueError(
            f"target {name!r}: fak_max_premium_pct must be 0..0.5")
    if not (0.0 <= float(target.take_profit_pct) <= 1.0):
        raise ValueError(f"target {name!r}: take_profit_pct must be 0..1")
    if not (0.0 <= float(target.stop_loss_pct) <= 1.0):
        raise ValueError(f"target {name!r}: stop_loss_pct must be 0..1")
