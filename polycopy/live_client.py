"""Thin adapter around the official Polymarket Python CLOB client.

The rest of the project never imports the SDK directly.  This keeps the live
path optional, and makes it possible to test execution with a fake gateway.

Install the live dependency separately (the dashboard/paper bot do not need
it):

    pip install -r requirements-live.txt

The V2 protocol is still moving; pin the SDK version and verify a $1 order
before enabling `live.dry_run = false`.
"""
from __future__ import annotations

import inspect
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any

from . import broker
from .config import LiveConfig
from .execution import LiveTradingError


@dataclass
class LiveOrderResult:
    filled_size: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    status: str = "rejected"
    reason: str | None = None
    order_id: str | None = None
    raw: dict[str, Any] | None = None
    remote_status: str = ""
    canceled: bool = False


class SdkClobGateway:
    """Concrete gateway backed by ``py_clob_client_v2``.

    Only the methods used by :class:`LiveExecutor` are exposed.  API details
    are wrapped in try/except so a minor SDK rename does not crash the bot
    before the first order; failures are surfaced as clear log messages.
    """

    def __init__(self, cfg: LiveConfig, client: Any = None,
                 logger: logging.Logger | None = None):
        self.cfg = cfg
        self.client = client
        self.log = logger or logging.getLogger("polycopy.live.client")
        self._sdk = None

    @classmethod
    def from_config(cls, cfg: LiveConfig, *,
                    logger: logging.Logger | None = None) -> "SdkClobGateway":
        if not cfg.private_key_env:
            raise LiveTradingError("live.private_key_env is empty")
        if not os.environ.get(cfg.private_key_env):
            raise LiveTradingError(
                f"environment variable {cfg.private_key_env} is not set")
        if not cfg.funder:
            raise LiveTradingError(
                "live.funder is empty; set the proxy wallet that holds funds")
        return cls(cfg, logger=logger)

    # ------------------------------------------------------------------ SDK
    def _load_sdk(self):
        if self._sdk is not None:
            return self._sdk
        try:
            import py_clob_client_v2 as sdk
        except ImportError as exc:
            raise LiveTradingError(
                "py-clob-client-v2 is not installed; run "
                "`pip install -r requirements-live.txt`"
            ) from exc
        self._sdk = sdk
        return sdk

    def _get_client(self):
        if self.client is not None:
            return self.client
        sdk = self._load_sdk()
        key = os.environ.get(self.cfg.private_key_env)
        if not key:
            raise LiveTradingError(
                f"environment variable {self.cfg.private_key_env} is not set")
        client = sdk.ClobClient(
            host=self.cfg.host,
            chain_id=int(self.cfg.chain_id),
            key=key,
            signature_type=int(self.cfg.signature_type),
            funder=self.cfg.funder or None,
        )
        # Prefer the v2 name; older builds expose create_or_derive_api_key.
        try:
            creds = client.create_or_derive_api_creds()
        except AttributeError:
            creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        # Do not log creds or the key.
        self.client = client
        self.log.info("live CLOB client ready funder=%s signature_type=%s",
                      _short(self.cfg.funder), self.cfg.signature_type)
        return client

    def healthcheck(self) -> None:
        """Import the SDK and derive/validate API credentials without orders."""
        self._get_client()

    # --------------------------------------------------------------- market
    def tick_size(self, token_id: str) -> float:
        client = self._get_client()
        for name in ("get_tick_size", "get_tick_size_for_token"):
            fn = getattr(client, name, None)
            if fn is None:
                continue
            try:
                value = fn(token_id)
                if isinstance(value, dict):
                    value = value.get("tick_size") or value.get("tickSize")
                if value:
                    return float(value)
            except Exception:  # noqa: BLE001
                self.log.debug("tick_size failed token=%s", token_id,
                               exc_info=True)
        return 0.001

    def neg_risk(self, token_id: str) -> bool:
        client = self._get_client()
        fn = getattr(client, "get_neg_risk", None)
        if fn is None:
            return False
        try:
            value = fn(token_id)
            if isinstance(value, dict):
                value = value.get("neg_risk") or value.get("negRisk")
            return bool(value)
        except Exception:  # noqa: BLE001
            self.log.debug("neg_risk failed token=%s", token_id, exc_info=True)
            return False

    # ---------------------------------------------------------------- orders
    def place_fak_buy(self, *, token_id: str, price: float, size: float,
                      fee_rate: float, on_prepared=None) -> LiveOrderResult:
        return self._place("BUY", token_id=token_id, price=price, size=size,
                           fee_rate=fee_rate, on_prepared=on_prepared)

    def place_fak_sell(self, *, token_id: str, price: float, size: float,
                       fee_rate: float, on_prepared=None) -> LiveOrderResult:
        return self._place("SELL", token_id=token_id, price=price, size=size,
                           fee_rate=fee_rate, on_prepared=on_prepared)

    def place_limit_buy(self, *, token_id: str, price: float, size: float,
                        fee_rate: float, on_prepared=None) -> LiveOrderResult:
        """Post a resting GTC buy.  size is in outcome shares."""
        sdk = self._load_sdk()
        client = self._get_client()
        tick = self.tick_size(token_id)
        options = None
        if hasattr(sdk, "PartialCreateOrderOptions"):
            try:
                options = sdk.PartialCreateOrderOptions(
                    tick_size=str(tick), neg_risk=self.neg_risk(token_id))
            except Exception:  # noqa: BLE001
                self.log.debug("order options failed token=%s", token_id,
                               exc_info=True)
                options = None
        args = {
            "price": float(price),
            "size": float(size),
            "side": sdk.Side.BUY,
        }
        try:
            params = inspect.signature(sdk.OrderArgs).parameters
        except (TypeError, ValueError):
            params = {}
        if "position_id" in params and "token_id" not in params:
            args["position_id"] = token_id
        else:
            args["token_id"] = token_id
        order_args = sdk.OrderArgs(**args)
        if options is not None:
            try:
                signed = client.create_order(order_args, options=options)
            except TypeError:
                signed = client.create_order(order_args)
        else:
            signed = client.create_order(order_args)
        client_id = _order_hash(signed)
        if on_prepared is not None:
            on_prepared(client_id, signed)
        response = client.post_order(
            signed, sdk.OrderType.GTC, post_only=False)
        return self.parse_order(response, side="BUY", fee_rate=fee_rate)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        client = self._get_client()
        sdk = self._load_sdk()
        payload_cls = getattr(sdk, "OrderPayload", None)
        if payload_cls is not None:
            result = client.cancel_order(payload_cls(orderID=str(order_id)))
        else:
            result = client.cancel_order(str(order_id))
        return result if isinstance(result, dict) else {"response": result}

    def _place(self, side: str, *, token_id: str, price: float, size: float,
               fee_rate: float, on_prepared=None) -> LiveOrderResult:
        sdk = self._load_sdk()
        client = self._get_client()
        tick = self.tick_size(token_id)
        options = None
        if hasattr(sdk, "PartialCreateOrderOptions"):
            try:
                options = sdk.PartialCreateOrderOptions(
                    tick_size=str(tick), neg_risk=self.neg_risk(token_id))
            except Exception:  # noqa: BLE001
                self.log.debug("order options failed token=%s", token_id,
                               exc_info=True)
                options = None
        # CLOB market orders use USDC for BUY and shares for SELL.  The
        # maker/taker amount precision is stricter than a plain limit order:
        # market BUY maker amount is max 2 decimals, taker amount max 5.
        # create_market_order applies those roundings; create_order with
        # OrderArgs produced 5-decimal USDC and was rejected with
        # "invalid amounts ... max accuracy of 2 decimals".
        amount = _market_order_amount(side, price, size)
        if amount <= 0:
            raise ValueError("order amount rounded to zero")
        args = {
            "price": float(price),
            "amount": amount,
            "side": sdk.Side.BUY if side == "BUY" else sdk.Side.SELL,
            "order_type": sdk.OrderType.FAK,
        }
        try:
            params = inspect.signature(sdk.MarketOrderArgs).parameters
        except (TypeError, ValueError):
            params = {}
        # Protocol V2 builds may call the outcome id position_id.
        if "position_id" in params and "token_id" not in params:
            args["position_id"] = token_id
        else:
            args["token_id"] = token_id
        order_args = sdk.MarketOrderArgs(**args)
        if options is not None:
            try:
                signed = client.create_market_order(order_args, options=options)
            except TypeError:
                signed = client.create_market_order(order_args)
        else:
            signed = client.create_market_order(order_args)
        client_id = _order_hash(signed)
        if on_prepared is not None:
            # Record PREPARED even without a hash: the duplicate guard must
            # stop a restart from submitting the same signal again.
            on_prepared(client_id, signed)
        response = client.post_order(signed, sdk.OrderType.FAK)
        result = self.parse_order(response, side=side, fee_rate=fee_rate)
        # FAK normally resolves immediately.  If the venue reports a delayed
        # state, poll by client order id until terminal or timeout; never
        # resubmit.
        deadline = time.time() + max(1, int(self.cfg.order_timeout_seconds))
        while (client_id and result.status in ("submitted", "partial")
               and result.filled_size <= 0 and time.time() < deadline):
            time.sleep(0.5)
            remote = self.get_order(str(client_id))
            if remote is None:
                continue
            result = self.parse_order(remote, side=side, fee_rate=fee_rate)
            if result.status in ("filled", "rejected", "canceled"):
                break
        return result

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        client = self._get_client()
        try:
            body = client.get_order(order_id)
        except Exception:  # noqa: BLE001
            self.log.debug("get_order failed id=%s", order_id, exc_info=True)
            return None
        if isinstance(body, dict):
            return body
        if isinstance(body, list) and body and isinstance(body[0], dict):
            return body[0]
        return None

    def parse_order(self, response: Any, *, side: str,
                    fee_rate: float) -> LiveOrderResult:
        if not isinstance(response, dict):
            return LiveOrderResult(status="rejected",
                                   reason="unparseable_order_response",
                                   raw={"response": str(response)})
        raw = dict(response)
        if response.get("success") is False:
            return LiveOrderResult(
                status="rejected",
                reason=str(response.get("errorMsg") or response.get("error")
                           or "order_rejected"),
                order_id=_first(response, "orderID", "orderId", "id"),
                raw=raw,
                remote_status=str(response.get("status") or ""),
            )
        status = str(response.get("status") or "").lower()
        canceled = status in ("canceled", "cancelled", "expired",
                              "unmatched", "unfilled")
        making = _amount(response.get("makingAmount"))
        taking = _amount(response.get("takingAmount"))
        if side == "BUY":
            shares, price = taking, (making / taking if taking else 0.0)
        else:
            shares, price = making, (taking / making if making else 0.0)
        if shares <= 0:
            # get_order() often reports cumulative size_matched instead of
            # making/taking amounts.
            for key in ("size_matched", "sizeMatched", "matchedSize",
                        "filled_size", "filledSize", "matched_size"):
                if response.get(key) is not None:
                    shares = _amount(response[key])
                    if shares > 0:
                        break
            for key in ("price", "avg_price", "avgPrice", "average_price"):
                if response.get(key) is not None:
                    price = _amount(response[key])
                    if price > 0:
                        break
        if shares <= 0 or price <= 0:
            return LiveOrderResult(
                status="canceled" if canceled else "submitted",
                reason=None if canceled else (status or "no_fill"),
                order_id=_first(response, "orderID", "orderId", "id"),
                raw=raw,
                remote_status=status,
                canceled=canceled,
            )
        fee = _amount(response.get("fee") or response.get("feeAmount"))
        if fee <= 0:
            fee = broker.taker_fee(shares, price, fee_rate)
        return LiveOrderResult(
            filled_size=shares,
            avg_price=price,
            fee=fee,
            status=("filled" if status in ("matched", "filled", "")
                    else "canceled" if canceled else "partial"),
            order_id=_first(response, "orderID", "orderId", "id"),
            raw=raw,
            remote_status=status,
            canceled=canceled,
        )

    # -------------------------------------------------------------- account
    def balance(self) -> float | None:
        client = self._get_client()
        fn = getattr(client, "get_balance_allowance", None)
        if fn is None:
            return None
        sdk = self._load_sdk()
        params = None
        if hasattr(sdk, "BalanceAllowanceParams"):
            try:
                asset = getattr(sdk, "AssetType", None)
                params = sdk.BalanceAllowanceParams(
                    asset_type=getattr(asset, "COLLATERAL", "COLLATERAL"))
            except TypeError:
                params = None
        for arg in (params, {"asset_type": "COLLATERAL"}):
            try:
                body = fn(arg)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(body, dict):
                for key in ("balance", "available", "available_balance"):
                    if body.get(key) is not None:
                        return _amount(body[key])
        return None

    def redeem(self, condition_id: str) -> bool:
        client = self._get_client()
        fn = getattr(client, "redeem_positions", None)
        if fn is None:
            self.log.warning(
                "SDK has no redeem_positions; redeem %s manually", condition_id)
            return False
        try:
            result = fn(condition_id=condition_id)
        except TypeError:
            result = fn(condition_id)
        except Exception:  # noqa: BLE001
            self.log.exception("redeem failed condition=%s", condition_id)
            return False
        return bool(result)


def _market_order_amount(side: str, price: float, size: float) -> float:
    """CLOB market-order precision: BUY maker USDC 2dp, SELL maker shares 2dp."""
    value = float(size) * float(price) if side == "BUY" else float(size)
    return math.floor(value * 100.0 + 1e-9) / 100.0


def _order_hash(signed: Any) -> str | None:
    if isinstance(signed, dict):
        for key in ("hash", "orderHash", "orderID", "orderId", "id"):
            if signed.get(key):
                return str(signed[key])
    for attr in ("hash", "order_hash", "orderHash", "order_id"):
        value = getattr(signed, attr, None)
        if value:
            return str(value)
    return None


def _first(body: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = body.get(key)
        if value:
            return str(value)
    return None


def _amount(value: Any) -> float:
    """CLOB amounts are micro-units (6 decimals); tolerate human values."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number / 1_000_000.0 if number >= 1_000_000 else number


def _short(value: str) -> str:
    return value[:6] + "…" + value[-4:] if len(value) > 12 else value
