"""REST transport for PRISM's existing US execution and exact-fill contracts.

Inherits only validated sizing/session checks from the OpenD adapter; no SDK
or gateway is constructed. Sync reads run in the callers' existing worker paths.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import threading

import httpx
from prism_core.moomoo_trading import MoomooConfig, MoomooStockTrading, MoomooUnavailable, code, number

# US and KR both own a 'trading' package. Load the shared paper implementation
# under its canonical module name without replacing whichever package is active.
def _shared(name):
    key = "trading." + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).resolve().parents[1] / "trading" / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]

paper = _shared("moomoo_paper")
manager_module = _shared("moomoo_paper_manager")


class MoomooRestTrading(MoomooStockTrading):
    transport = "rest"

    def __init__(self, config=None, *, account_name=None):
        self.config = config or MoomooConfig.from_env()
        if self.config.mode != "demo" or self.config.allow_live:
            raise MoomooUnavailable("REST_PAPER_ONLY")
        if self.config.buy_amount > 5000:
            raise MoomooUnavailable("REST_BUY_CAP_EXCEEDED")
        if account_name is not None and account_name != self.config.name:
            raise MoomooUnavailable("REST_ACCOUNT_NAME_MISMATCH")
        self.account_config = self.config.account()
        self.account_name = self.config.name
        self.account_key = self.account_config["account_key"]
        self.mode, self.buy_amount = "demo", self.config.buy_amount
        self.directory = Path(os.environ.get("MOOMOO_REST_STATE_DIR", str(paper.STATE_DIR)))
        self._lock = threading.RLock()
        self._pending = set()
        self._closed = False
        self._intent = threading.local()
        self._validate_account()

    @asynccontextmanager
    async def _connection(self):
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            auth = paper.OAuthSession(client, self.directory)
            broker = paper.MoomooPaperBroker(client, auth)
            yield broker, client, auth

    def _run(self, fn):
        if self._closed:
            raise MoomooUnavailable("REST_CONTEXT_CLOSED")
        async def operation():
            async with self._connection() as args:
                return await fn(*args)
        return asyncio.run(operation())

    def _validate_account(self):
        async def check(b, *_):
            return await b.select_account(str(self.config.account_id))
        self._run(check)

    def close(self):
        self._closed = True

    def get_portfolio(self, *, strict=False):
        async def query(b, *_):
            data = await b.snapshot(str(self.config.account_id))
            result = manager_module.positions(data["positions"])
            return [dict(ticker=r["ticker"], quantity=r["quantity"], sellable_quantity=r["sellable_quantity"],
                         avg_price=r["cost_price"], current_price=r["current_price"],
                         market_value=r["market_value"], profit_loss=r["profit"], exchange=None) for r in result]
        return self._run(query)

    def get_account_summary(self):
        async def query(b, *_):
            await b.select_account(str(self.config.account_id))
            r = await b._request("GET", str(self.config.account_id) + "/cash-info")
            return dict(total_eval_amount=r.get("total_asset"), cash=r.get("balance"),
                        buying_power=r.get("max_power_long"), currency="USD", source="MOOMOO_REST")
        return self._run(query)

    def get_current_price(self, ticker, exchange=None, *, strict=False):
        async def query(b, client, auth):
            response = await client.post(paper.API_HOST + "/api/v1.0/quote/stock-quote",
                headers={"Authorization": "Bearer " + await auth.access_token()},
                json={"code_list": [code(ticker)]})
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data", {}).get("quote_list", [])
            if payload.get("ret_code") != 0 or len(rows) != 1 or rows[0].get("code") != code(ticker):
                raise MoomooUnavailable("REST_EXACT_QUOTE_MISSING")
            row = rows[0]
            observed = number(row.get("data_time"), positive=True) / 1000
            age = Decimal(str(datetime.now(timezone.utc).timestamp())) - observed
            if row.get("suspension") is not False or (self.is_market_open() and not -60 <= age <= 300):
                raise MoomooUnavailable("REST_QUOTE_STALE_OR_SUSPENDED")
            return dict(ticker=ticker.upper(), stock_name=row.get("name", ""),
                        current_price=float(number(row.get("last_price"), positive=True)),
                        exchange=exchange, source="MOOMOO_REST", observed_at=float(observed))
        return self._run(query)

    def _buy(self, *args, paper_intent_id=None, **kwargs):
        with self._lock:
            self._intent.value = paper_intent_id
            return super()._buy(*args, **kwargs)

    def _sell(self, *args, paper_intent_id=None, **kwargs):
        with self._lock:
            self._intent.value = paper_intent_id
            return super()._sell(*args, **kwargs)

    def _place(self, ticker, side, quantity, price, *, market=False):
        intent = getattr(self._intent, "value", None)
        if not intent:
            return self._failure("REST_STABLE_INTENT_REQUIRED")
        stable = hashlib.sha256(str(intent).encode()).hexdigest()
        async def submit(b, *_):
            m = manager_module.PaperManager(b, self.directory)
            return await m.submit(str(self.config.account_id), stable, ticker, side, quantity,
                                  str(price), market_sell=market)
        try:
            row = self._run(submit)
            if row["state"] in {"SENDING", "UNKNOWN"}:
                return self._failure("REST_OUTCOME_UNKNOWN", unknown=True)
            if row["state"] == "REJECTED":
                return self._failure("REST_ORDER_REJECTED")
            return dict(success=True, broker="MOOMOO", order_no=row["order_id"], ticker=ticker,
                        quantity=quantity, limit_price=price, status="SUBMITTED", filled_quantity=None,
                        order_type="market" if market else "limit", message="Paper order submitted; reconcile fills")
        except paper.OutcomeUnknown:
            return self._failure("REST_OUTCOME_UNKNOWN", unknown=True)
        except paper.MoomooError:
            return self._failure("REST_VALIDATION_OR_BROKER_REJECTION")

    @staticmethod
    def normalize_order(row):
        if row.get("market") != 2 or row.get("side") not in (1, 2):
            raise MoomooUnavailable("REST_ORDER_MARKET_OR_SIDE_INVALID")
        state = {1:"WAITING_SUBMIT",2:"SUBMITTED",3:"FILLED_PART",4:"FILLED_ALL",5:"CANCELLED_ALL",6:"FAILED",7:"CANCELLED_PART"}.get(row.get("status"))
        if row.get("status") == 5 and number(row.get("cum_qty")) > 0:
            state = "CANCELLED_PART"
        if state is None:
            raise MoomooUnavailable("REST_ORDER_STATUS_UNKNOWN")
        created = datetime.fromtimestamp(int(row["create_time"]) / 1000000, timezone.utc)
        return dict(order_id=paper.identifier(row.get("order_id")), code=code(row.get("symbol")),
                    trd_side="BUY" if row["side"] == 1 else "SELL", currency="USD",
                    order_status=state, qty=row.get("qty"), dealt_qty=row.get("cum_qty"),
                    dealt_avg_price=row.get("avg_fill_price"), price=row.get("price"),
                    create_time=created.isoformat())

    def order_rows(self, start=None, end=None):
        async def query(b, *_):
            account = str(self.config.account_id)
            rows = (await b.snapshot(account))["orders"]
            if start:
                since = datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() - 86400
                current_ids = {r.get("order_id") for r in rows}
                history = await b.history_orders(account, since)
                rows += [r for r in history if r.get("order_id") not in current_ids]
            normalized = [self.normalize_order(r) for r in rows]
            from zoneinfo import ZoneInfo
            def included(row):
                day = datetime.fromisoformat(row["create_time"]).astimezone(ZoneInfo("America/New_York")).date().isoformat()
                return (not start or day >= start[:10]) and (not end or day <= end[:10])
            return [r for r in normalized if included(r)]
        return self._run(query)

    def get_order(self, order_id, *, start=None, end=None):
        rows = self.order_rows(start, end)
        matches = [r for r in rows if r["order_id"] == str(order_id)]
        if len(matches) != 1:
            raise MoomooUnavailable("REST_EXACT_ORDER_MISSING")
        return matches[0]

    def cancel_order(self, ticker, orgn_odno, quantity, exchange=None, dry_run=False):
        row = self.get_order(orgn_odno)
        if row["code"] != code(ticker) or quantity != number(row["qty"]) - number(row["dealt_qty"]):
            return self._failure("REST_CANCEL_IDENTITY_MISMATCH")
        if dry_run:
            return {"dry_run": True}
        async def cancel(b, *_):
            m = manager_module.PaperManager(b, self.directory)
            matches = [r for r in m.rows(str(self.config.account_id)) if r["order_id"] == str(orgn_odno)]
            if len(matches) != 1:
                raise paper.MoomooError("Untracked order cancellation blocked")
            return await m.cancel(str(self.config.account_id), matches[0]["intent"])
        try:
            self._run(cancel)
            return dict(success=True, broker="MOOMOO", order_no=str(orgn_odno), status="SUBMITTED")
        except paper.OutcomeUnknown:
            return self._failure("REST_CANCEL_UNKNOWN", unknown=True)

    def amend_order(self, *args, **kwargs):
        return self._failure("REST_AMEND_DISABLED_USE_TRACKED_CANCEL")


class AsyncMoomooRestContext:
    def __init__(self, account_name=None):
        self.account_name, self.trader = account_name, None

    async def __aenter__(self):
        self.trader = await asyncio.to_thread(MoomooRestTrading, account_name=self.account_name)
        return self.trader

    async def __aexit__(self, *args):
        if self.trader:
            if self.trader._pending:
                await asyncio.gather(*self.trader._pending, return_exceptions=True)
            self.trader.close()
