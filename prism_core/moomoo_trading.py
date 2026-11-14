"""Moomoo US OpenD adapter. No KIS credentials and no implicit live selection.

Synchronous SDK I/O is offloaded by the async entrypoints. An acknowledgement
only means submitted; reconciliation queries exact account + order identities.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import importlib
import os
import re
import threading

from prism_core.order_budget import order_budget_evidence, whole_share_quantity


class MoomooUnavailable(RuntimeError):
    """Missing or invalid broker evidence; never an empty position or a fill."""


def number(value, *, integer=False, positive=False):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError()
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
            raise ValueError()
        if integer and parsed != parsed.to_integral_value():
            raise ValueError()
        return int(parsed) if integer else parsed
    except (ValueError, InvalidOperation, TypeError):
        raise MoomooUnavailable("MOOMOO_NUMBER_INVALID") from None


def code(ticker):
    value = str(ticker).upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", value) or value.startswith("US."):
        raise ValueError("US stock ticker required without market prefix")
    return "US." + value


def limit_value(value):
    price = number(value, positive=True)
    tick = Decimal(".0001") if price < 1 else Decimal(".01")
    if price != price.quantize(tick):
        raise MoomooUnavailable("MOOMOO_PRICE_PRECISION_INVALID")
    return price


@dataclass(frozen=True)
class MoomooConfig:
    account_id: int
    buy_amount: float
    mode: str = "demo"
    name: str = "moomoo-us"
    host: str = "127.0.0.1"
    port: int = 11111
    allow_live: bool = False

    def __post_init__(self):
        number(self.account_id, integer=True, positive=True)
        number(self.buy_amount, positive=True)
        if self.mode not in {"demo", "real"} or not self.name.strip():
            raise ValueError("Invalid moomoo account configuration")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Use a local OpenD or an SSH tunnel on loopback")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Invalid OpenD port")

    @classmethod
    def from_env(cls):
        # Require an explicit budget: changing brokers must not invent risk limits.
        return cls(
            account_id=number(os.getenv("MOOMOO_ACCOUNT_ID"), integer=True, positive=True),
            buy_amount=float(number(os.getenv("MOOMOO_BUY_AMOUNT_USD"), positive=True)),
            mode=os.getenv("MOOMOO_TRADING_MODE", "demo").strip().lower(),
            name=os.getenv("MOOMOO_ACCOUNT_NAME", "moomoo-us").strip(),
            host=os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1").strip(),
            port=int(os.getenv("MOOMOO_OPEND_PORT", "11111")),
            allow_live=os.getenv("MOOMOO_ALLOW_LIVE", "false").lower() == "true",
        )

    def account(self):
        return dict(name=self.name, account_key=f"moomoo:{self.mode}:{self.account_id}",
                    mode=self.mode, market="us", broker="MOOMOO", primary=True,
                    buy_amount_usd=self.buy_amount, product="US")


class MoomooStockTrading:
    broker_name = "MOOMOO"
    _OPEN = {"WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART"}
    _TERMINAL = {"FILLED_ALL", "CANCELLED_PART", "CANCELLED_ALL", "FAILED", "DISABLED", "DELETED"}

    def __init__(self, config=None, *, account_name=None, sdk=None):
        self.config = config or MoomooConfig.from_env()
        if account_name is not None and account_name != self.config.name:
            raise ValueError("MOOMOO_ACCOUNT_NAME_MISMATCH")
        self.account_config = self.config.account()
        self.account_name = self.config.name
        self.account_key = self.account_config["account_key"]
        self.mode, self.buy_amount = self.config.mode, self.config.buy_amount
        self.sdk = sdk or importlib.import_module("moomoo")
        self.trd_env = self.sdk.TrdEnv.SIMULATE if self.mode == "demo" else self.sdk.TrdEnv.REAL
        self._lock = threading.RLock()
        self._trade = self._quote = None
        self._closed = False
        self._pending = set()
        try:
            self._trade = self.sdk.OpenSecTradeContext(
                filter_trdmarket=self.sdk.TrdMarket.US, host=self.config.host,
                port=self.config.port, security_firm=self.sdk.SecurityFirm.FUTUINC)
            self._validate_account()
            self._quote = self.sdk.OpenQuoteContext(host=self.config.host, port=self.config.port)
        except BaseException:
            self.close()
            raise

    def _rows(self, result):
        ret, data = result
        if ret != self.sdk.RET_OK or not hasattr(data, "to_dict"):
            # Broker errors may include account identifiers; do not echo them.
            raise MoomooUnavailable("MOOMOO_QUERY_FAILED")
        return data.to_dict("records")

    def _scope(self):
        return dict(acc_id=self.config.account_id, trd_env=self.trd_env)

    def _validate_account(self):
        rows = self._rows(self._trade.get_acc_list())
        matches = [r for r in rows if str(r.get("acc_id")) == str(self.config.account_id)]
        if len(matches) != 1:
            raise MoomooUnavailable("MOOMOO_ACCOUNT_NOT_FOUND")
        row = matches[0]
        if row.get("trd_env") != self.trd_env or row.get("acc_status") != "ACTIVE":
            raise MoomooUnavailable("MOOMOO_ACCOUNT_ENVIRONMENT_OR_STATUS_MISMATCH")
        if self.sdk.TrdMarket.US not in (row.get("trdmarket_auth") or []):
            raise MoomooUnavailable("MOOMOO_US_PERMISSION_MISSING")
        if self.mode == "real" and row.get("security_firm") != self.sdk.SecurityFirm.FUTUINC:
            raise MoomooUnavailable("MOOMOO_US_FIRM_MISMATCH")
        if self.mode == "demo" and row.get("sim_acc_type") not in {"STOCK", "STOCK_AND_OPTION"}:
            raise MoomooUnavailable("MOOMOO_STOCK_SIMULATION_REQUIRED")

    def close(self):
        with self._lock:
            self._closed = True
            for resource in (self._quote, self._trade):
                if resource is not None:
                    resource.close()
            self._quote = self._trade = None

    def _query(self, method, **kwargs):
        with self._lock:
            if self._closed:
                raise MoomooUnavailable("MOOMOO_CONTEXT_CLOSED")
            return self._rows(getattr(self._trade, method)(**self._scope(), **kwargs))

    def get_current_price(self, ticker, exchange=None, *, strict=False):
        with self._lock:
            rows = self._rows(self._quote.get_market_snapshot([code(ticker)]))
        if len(rows) != 1 or rows[0].get("code") != code(ticker):
            raise MoomooUnavailable("MOOMOO_QUOTE_IDENTITY_MISMATCH")
        row = rows[0]
        return dict(ticker=ticker.upper(), stock_name=row.get("name", ""),
                    current_price=float(number(row.get("last_price"), positive=True)),
                    exchange=exchange, source="MOOMOO", observed_at=row.get("update_time"))

    def get_portfolio(self, *, strict=False):
        rows = self._query("position_list_query", position_market=self.sdk.TrdMarket.US, refresh_cache=True)
        result = []
        seen = set()
        for row in rows:
            symbol = str(row.get("code", ""))
            if not symbol.startswith("US.") or symbol in seen:
                raise MoomooUnavailable("MOOMOO_POSITION_IDENTITY_INVALID")
            seen.add(symbol)
            # Long whole-share stock positions only. Never truncate a fractional/short holding.
            quantity = number(row.get("qty"), integer=True)
            sellable = number(row.get("can_sell_qty"), integer=True)
            if sellable > quantity:
                raise MoomooUnavailable("MOOMOO_POSITION_QUANTITY_INVALID")
            result.append(dict(ticker=symbol[3:], company_name=row.get("stock_name", ""),
                               quantity=quantity, sellable_quantity=sellable,
                               avg_price=row.get("average_cost"), current_price=row.get("nominal_price"),
                               market_value=row.get("market_val"), profit_loss=row.get("pl_val"),
                               profit_rate=row.get("pl_ratio"), exchange=None))
        return result

    def get_holding_quantity(self, ticker):
        return next((r["quantity"] for r in self.get_portfolio(strict=True) if r["ticker"] == ticker.upper()), 0)

    def get_account_summary(self):
        rows = self._query("accinfo_query", currency=self.sdk.Currency.USD, refresh_cache=True)
        if len(rows) != 1:
            raise MoomooUnavailable("MOOMOO_FUNDS_INVALID")
        row = rows[0]
        return dict(total_eval_amount=row.get("total_assets"), cash=row.get("cash"),
                    buying_power=row.get("power"), currency="USD", source="MOOMOO")

    def is_market_open(self):
        from prism_core.oneil_service import regular_session
        return regular_session(datetime.now(timezone.utc).isoformat())

    def is_reserved_order_available(self):
        # Native DAY limit orders; never use the KIS 10:05 local queue.
        return True

    def _failure(self, reason, *, unknown=False):
        return dict(success=False, broker="MOOMOO", order_no=None, message=reason,
                    outcome_unknown=unknown, status="UNKNOWN" if unknown else "REJECTED")

    def _place(self, ticker, side, quantity, price, *, market=False):
        with self._lock:
            if self.mode == "real" and not self.config.allow_live:
                return self._failure("MOOMOO_LIVE_NOT_ENABLED")
            self._validate_account()
            try:
                rows = self._rows(self._trade.place_order(
                    **self._scope(), code=code(ticker), qty=quantity, price=price,
                    trd_side=getattr(self.sdk.TrdSide, side),
                    order_type=self.sdk.OrderType.MARKET if market else self.sdk.OrderType.NORMAL,
                    time_in_force=self.sdk.TimeInForce.DAY, session=self.sdk.Session.RTH,
                    remark="PRISM", adjust_limit=0))
                if len(rows) != 1 or not str(rows[0].get("order_id") or ""):
                    return self._failure("MOOMOO_ORDER_ACK_MISSING", unknown=True)
                row = rows[0]
                if (row.get("code") != code(ticker) or row.get("trd_side") != side
                        or number(row.get("qty"), integer=True) != quantity):
                    return self._failure("MOOMOO_ORDER_ACK_MISMATCH", unknown=True)
                if row.get("order_status") not in self._OPEN | self._TERMINAL:
                    return self._failure("MOOMOO_ORDER_STATUS_UNKNOWN", unknown=True)
                if row.get("order_status") in {"FAILED", "DISABLED", "DELETED"}:
                    return self._failure("MOOMOO_ORDER_REJECTED")
                return dict(success=True, broker="MOOMOO", order_no=str(row["order_id"]),
                            ticker=ticker.upper(), quantity=quantity, limit_price=price,
                            status="SUBMITTED", order_type="market" if market else "limit",
                            message="Order submitted; fill unconfirmed", filled_quantity=None)
            except Exception:
                return self._failure("MOOMOO_SUBMISSION_UNCONFIRMED", unknown=True)

    def _buy(self, ticker, buy_amount=None, exchange=None, limit_price=None, *,
             quote_validator=None, strict_budget=False, regular_session_only=False, exact_quantity=None):
        with self._lock:
            try:
                amount = number(buy_amount if buy_amount is not None else (0 if strict_budget else self.buy_amount), positive=True)
                quote = self.get_current_price(ticker, exchange, strict=True)["current_price"]
                price = limit_value(limit_price if limit_price is not None else quote)
                if strict_budget and limit_price is None:
                    raise ValueError()
                if quote_validator is not None:
                    quote_validator(quote)
                    quote_validator(float(price))
                quantity = whole_share_quantity(float(amount), float(price))
                if quantity <= 0 or (exact_quantity is not None and (type(exact_quantity) is not int or quantity != exact_quantity)):
                    raise ValueError()
                if regular_session_only and (quote_validator is None or exact_quantity is None or not self.is_market_open()):
                    raise ValueError()
            except Exception:
                return self._failure("MOOMOO_BUY_VALIDATION_FAILED")
            result = self._place(ticker, "BUY", quantity, float(price))
            result.update(order_budget_evidence(float(amount), float(price), quantity))
            result.update(current_price=quote, total_amount=float(price * quantity))
            return result

    def _sell(self, ticker, exchange=None, limit_price=None, use_moo=False, quantity=None, *,
              regular_session_only=False, quote_validator=None):
        with self._lock:
            try:
                # An opening auction cannot be silently replaced by a market order.
                if use_moo:
                    return self._failure("MOOMOO_MOO_NOT_SUPPORTED")
                rows = [r for r in self.get_portfolio(strict=True) if r["ticker"] == ticker.upper()]
                if len(rows) != 1:
                    return self._failure("MOOMOO_POSITION_NOT_FOUND")
                held = rows[0]
                qty = number(held["quantity"] if quantity is None else quantity, integer=True, positive=True)
                if qty > held["sellable_quantity"]:
                    return self._failure("MOOMOO_INSUFFICIENT_SELLABLE_QUANTITY")
                market = limit_price is None
                price = 0.0 if market else float(limit_value(limit_price))
                if (market or regular_session_only) and not self.is_market_open():
                    return self._failure("MOOMOO_REGULAR_SESSION_REQUIRED")
                if regular_session_only and quote_validator is None:
                    raise ValueError()
                if quote_validator is not None:
                    quote_validator(self.get_current_price(ticker, exchange, strict=True)["current_price"])
                    if not market:
                        quote_validator(price)
            except Exception:
                return self._failure("MOOMOO_SELL_VALIDATION_FAILED")
            return self._place(ticker, "SELL", qty, price, market=market)

    async def _async_order(self, method, timeout, **kwargs):
        try:
            timeout = float(number(timeout, positive=True))
        except MoomooUnavailable:
            return self._failure("MOOMOO_TIMEOUT_INVALID")
        task = asyncio.create_task(asyncio.to_thread(method, **kwargs))
        self._pending.add(task)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            return self._failure("MOOMOO_ORDER_TIMEOUT", unknown=True)
        finally:
            if task.done():
                self._pending.discard(task)

    async def async_buy_stock(self, ticker, buy_amount=None, exchange=None, timeout=30.0, limit_price=None, **kwargs):
        return await self._async_order(self._buy, timeout, ticker=ticker, buy_amount=buy_amount,
                                       exchange=exchange, limit_price=limit_price, **kwargs)

    async def async_sell_stock(self, ticker, exchange=None, timeout=30.0, limit_price=None, use_moo=False, quantity=None, **kwargs):
        return await self._async_order(self._sell, timeout, ticker=ticker, exchange=exchange,
                                       limit_price=limit_price, use_moo=use_moo, quantity=quantity, **kwargs)

    def buy_reserved_order(self, ticker, limit_price, buy_amount=None, exchange=None, **kwargs):
        kwargs["strict_budget"] = True
        return self._buy(ticker, buy_amount, exchange, limit_price, **kwargs)

    def sell_reserved_order(self, ticker, limit_price=None, exchange=None, use_moo=False, quantity=None, **kwargs):
        return self._sell(ticker, exchange, limit_price, use_moo, quantity, **kwargs)

    def order_rows(self, start=None, end=None):
        if start is None:
            return self._query("order_list_query", order_market=self.sdk.TrdMarket.US, refresh_cache=True)
        return self._query("history_order_list_query", order_market=self.sdk.TrdMarket.US, start=start, end=end)

    def get_unfilled_orders(self, ticker=None, exchange=None):
        from prism_core.moomoo_evidence import order_facts
        result = []
        for row in self.order_rows():
            if ticker and row.get("code") != code(ticker):
                continue
            state = row.get("order_status")
            if state not in self._OPEN | self._TERMINAL:
                raise MoomooUnavailable("MOOMOO_ORDER_STATE_UNKNOWN")
            if state in self._OPEN:
                facts = order_facts(row)
                ordered, filled = facts["ordered_qty"], facts["filled_qty"]
                result.append(dict(order_no=str(row["order_id"]), ticker=row["code"][3:],
                                   ord_qty=ordered, ccld_qty=filled, nccs_qty=ordered - filled,
                                   sll_buy_dvsn_cd=facts["side"],
                                   ord_unpr=float(number(row.get("price"))), exchange=None))
        return result

    def get_order(self, order_id, *, start=None, end=None):
        rows = self.order_rows(start, end) if start else self._query(
            "order_list_query", order_id=str(order_id), refresh_cache=True)
        matches = [r for r in rows if str(r.get("order_id")) == str(order_id)]
        if len(matches) != 1:
            raise MoomooUnavailable("MOOMOO_EXACT_ORDER_MISSING")
        return matches[0]

    def _modify(self, action, ticker, order_id, quantity, price=0.0, *, dry_run=False):
        if self.mode == "real" and not self.config.allow_live and not dry_run:
            return self._failure("MOOMOO_LIVE_NOT_ENABLED")
        with self._lock:
            row = self.get_order(order_id)
            if row.get("code") != code(ticker) or row.get("order_status") not in self._OPEN:
                return self._failure("MOOMOO_MODIFY_IDENTITY_OR_STATUS_INVALID")
            qty = number(quantity, integer=True, positive=True)
            ordered, filled = number(row["qty"], integer=True), number(row["dealt_qty"], integer=True)
            if action == "CANCEL" and qty != ordered - filled:
                return self._failure("MOOMOO_PARTIAL_CANCEL_NOT_SUPPORTED")
            if action == "NORMAL" and qty != ordered - filled:
                return self._failure("MOOMOO_AMEND_QUANTITY_CHANGE_NOT_SUPPORTED")
            # SDK modify qty is the total order quantity, unlike KIS remaining qty.
            params = dict(**self._scope(), modify_order_op=getattr(self.sdk.ModifyOrderOp, action),
                          order_id=str(order_id), qty=ordered,
                          price=float(limit_value(price)) if action == "NORMAL" else 0.0)
            if dry_run:
                return dict(dry_run=True, broker="MOOMOO", action=action, quantity=ordered, limit_price=price)
            self._validate_account()
            try:
                acknowledgements = self._rows(self._trade.modify_order(**params))
                if len(acknowledgements) != 1 or str(acknowledgements[0].get("order_id")) != str(order_id):
                    return self._failure("MOOMOO_MODIFY_ACK_MISMATCH", unknown=True)
                return dict(success=True, broker="MOOMOO", order_no=str(order_id),
                            status="SUBMITTED", message="Modification submitted; outcome unconfirmed")
            except Exception:
                return self._failure("MOOMOO_MODIFY_UNCONFIRMED", unknown=True)

    def cancel_order(self, ticker, orgn_odno, quantity, exchange=None, dry_run=False):
        return self._modify("CANCEL", ticker, orgn_odno, quantity, dry_run=dry_run)

    def amend_order(self, ticker, orgn_odno, limit_price, quantity, exchange=None, dry_run=False):
        return self._modify("NORMAL", ticker, orgn_odno, quantity, limit_price, dry_run=dry_run)


class AsyncMoomooTradingContext:
    def __init__(self, account_name=None):
        self.account_name, self.trader = account_name, None

    async def __aenter__(self):
        opening = asyncio.create_task(asyncio.to_thread(MoomooStockTrading, account_name=self.account_name))
        try:
            self.trader = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # Cancelling to_thread does not stop its worker or close its sockets.
            try:
                opened = await opening
                await asyncio.to_thread(opened.close)
            finally:
                raise
        return self.trader

    async def __aexit__(self, *args):
        if self.trader is not None:
            # A timed-out SDK worker can still submit. Finish it before closing its socket.
            if self.trader._pending:
                await asyncio.gather(*self.trader._pending, return_exceptions=True)
            await asyncio.to_thread(self.trader.close)
