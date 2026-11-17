"""Durable, paper-only order lifecycle. Broker snapshots remain authoritative.

Intent IDs are required and never replayed. An uncertain send blocks further
orders for that account until exact broker evidence resolves it. No KIS ledger
or real-money endpoint is used.
"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from trading.moomoo_paper import MoomooError, OutcomeUnknown, identifier, limit_order


def decimal(value):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise MoomooError("Missing or invalid broker numeric evidence.") from None


def positions(rows):
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise MoomooError("Invalid paper position row.")
        symbol = row.get("symbol", "")
        if (row.get("market") != 2 or row.get("pstn_type") != 0
                or not re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", symbol)
                or symbol in seen):
            raise MoomooError("Unsupported or duplicate paper position; manual review required.")
        seen.add(symbol)
        qty, available = decimal(row.get("qty")), decimal(row.get("qty_avbl"))
        if available > qty or qty != int(qty) or available != int(available):
            raise MoomooError("Invalid whole-share position evidence.")
        result.append({"ticker": symbol, "quantity": int(qty), "sellable_quantity": int(available),
                       "cost_price": row.get("cost_price"), "current_price": row.get("cur_price"),
                       "market_value": row.get("mv"), "profit": row.get("profit")})
    return result


class PaperManager:
    def __init__(self, broker, directory: Path):
        self.broker = broker
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "paper-ledger.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS intents (
                intent TEXT PRIMARY KEY, account TEXT NOT NULL, symbol TEXT NOT NULL,
                side TEXT NOT NULL, qty INTEGER NOT NULL, price TEXT NOT NULL,
                state TEXT NOT NULL, order_id TEXT, filled TEXT, avg_price TEXT,
                broker_status INTEGER, created REAL NOT NULL, observed REAL)''')
            if "order_type" not in {r[1] for r in db.execute("PRAGMA table_info(intents)")}:
                db.execute("ALTER TABLE intents ADD COLUMN order_type INTEGER NOT NULL DEFAULT 1")
            db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
                account TEXT PRIMARY KEY, observed REAL NOT NULL, payload TEXT NOT NULL)''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / "paper-manager.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MoomooError("Another paper lifecycle operation is running.") from None
            yield
        finally:
            os.close(fd)

    def rows(self, account):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM intents WHERE account=?", (account,))]

    async def _reconcile(self, account):
        snapshot = await self.broker.snapshot(account)
        portfolio = positions(snapshot["positions"])
        orders = snapshot["orders"]
        if any(not isinstance(r, dict) for r in orders):
            raise MoomooError("Invalid broker order list.")
        today_count = len(orders)
        current_ids = {r.get("order_id") for r in orders}
        unresolved = [r for r in self.rows(account)
                      if r["state"] in {"SENDING", "UNKNOWN", "ACKNOWLEDGED", "OPEN", "PARTIAL", "CANCEL_REQUESTED"}
                      and r["order_id"] not in current_ids]
        if unresolved:
            history = await self.broker.history_orders(account, min(r["created"] for r in unresolved) - 86400)
            orders = orders + [r for r in history if r.get("order_id") not in current_ids]
        now = time.time()
        with self.connect() as db:
            for intent in self.rows(account):
                if intent["state"] == "REJECTED":
                    continue
                matches = [r for r in orders if
                           (r.get("order_id") == intent["order_id"] if intent["order_id"]
                            else r.get("text") == "PRISM:" + intent["intent"])]
                if not matches:
                    # Today's list cannot resolve older orders or unknown writes.
                    continue
                if len(matches) != 1:
                    raise MoomooError("Ambiguous paper order evidence.")
                row = matches[0]
                oid = identifier(row.get("order_id"))
                filled, qty = decimal(row.get("cum_qty")), decimal(row.get("qty"))
                status = row.get("status")
                if (row.get("symbol") != intent["symbol"] or row.get("market") != 2
                        or row.get("side") != (1 if intent["side"] == "BUY" else 2)
                        or row.get("order_type") != intent["order_type"] or qty != intent["qty"]
                        or (intent["order_type"] == 1 and decimal(row.get("price")) != Decimal(intent["price"]))
                        or filled > qty or filled != int(filled)
                        or type(status) is not int or status not in range(1, 8)
                        or (status == 4 and filled != qty)
                        or (status in (1, 2, 6) and filled != 0)
                        or (status in (3, 7) and not 0 < filled < qty)
                        or (filled and decimal(row.get("avg_fill_price")) <= 0)
                        or (intent["filled"] is not None and filled < Decimal(intent["filled"]))):
                    raise MoomooError("Paper order evidence mismatch; no fill inferred.")
                state = {1: "OPEN", 2: "OPEN", 3: "PARTIAL", 4: "FILLED",
                         5: "CANCELLED", 6: "REJECTED", 7: "PARTIAL_CANCELLED"}[status]
                if intent["state"] == "CANCEL_REQUESTED" and status in (1, 2, 3):
                    state = "CANCEL_REQUESTED"
                db.execute('''UPDATE intents SET state=?,order_id=?,filled=?,avg_price=?,
                              broker_status=?,observed=? WHERE intent=?''',
                           (state, oid, str(filled), row.get("avg_fill_price") if filled else None,
                            status, now, intent["intent"]))
            db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)",
                       (account, now, json.dumps(snapshot)))
        return {"environment": "SIMULATE", "account_suffix": account[-4:], "observed_at": now,
                "positions": portfolio, "intents": self.rows(account), "orders_today": today_count, "broker_orders": orders}

    async def reconcile(self, account):
        with self.locked():
            return await self._reconcile(await self.broker.select_account(account))

    async def submit(self, account, intent, symbol, side, qty, price, *, market_sell=False):
        if market_sell and side != "SELL":
            raise MoomooError("Market buys are not supported.")
        body = limit_order(symbol, side, qty, "1" if market_sell else price)
        if market_sell:
            body.update(price="0", order_type=3)
            price = "0"
        symbol = body["symbol"][3:]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", intent):
            raise MoomooError("A stable intent ID (1-64 letters/digits/_/-) is required.")
        if side == "BUY" and Decimal(body["price"]) * qty > 5000:
            raise MoomooError("Paper buy exceeds the authorized USD 5000 limit.")
        with self.locked():
            account = await self.broker.select_account(account)
            with self.connect() as db:
                existing = db.execute("SELECT * FROM intents WHERE intent=?", (intent,)).fetchone()
            if existing:
                if (existing["account"], existing["symbol"], existing["side"], existing["qty"],
                    Decimal(existing["price"]), existing["order_type"]) != (account, symbol, side, qty, Decimal(price), body["order_type"]):
                    raise MoomooError("Intent ID already belongs to a different order.")
                return dict(existing)
            status = await self._reconcile(account)
            for previous in status["intents"]:
                if previous["state"] in {"SENDING", "UNKNOWN"}:
                    raise MoomooError("Unresolved submission blocks new orders for this account.")
                if previous["symbol"] == symbol and previous["state"] in {"ACKNOWLEDGED", "OPEN", "PARTIAL", "CANCEL_REQUESTED"}:
                    raise MoomooError("An unresolved order already exists for this symbol.")
            for order in status["broker_orders"]:
                if order.get("symbol") == symbol:
                    if type(order.get("status")) is not int or order["status"] not in range(1, 8):
                        raise MoomooError("Unknown broker order state blocks this symbol.")
                    if order["status"] in (1, 2, 3):
                        raise MoomooError("Broker has an open order for this symbol.")
            if side == "SELL":
                holding = next((p for p in status["positions"] if p["ticker"] == symbol), None)
                if holding is None or holding["sellable_quantity"] < qty:
                    raise MoomooError("Sell exceeds broker-confirmed available long shares.")
            with self.connect() as db:
                db.execute('''INSERT INTO intents(intent,account,symbol,side,qty,price,state,created,order_type)
                              VALUES (?,?,?,?,?,?,?,?,?)''',
                           (intent, account, symbol, side, qty, body["price"], "SENDING", time.time(), body["order_type"]))
            # Persist before network I/O. A crash/cancellation leaves SENDING, never replayable.
            try:
                if market_sell:
                    ack = await self.broker.submit_market_sell(account, symbol, qty, remark="PRISM:" + intent)
                else:
                    ack = await self.broker.submit_limit(account, symbol, side, qty, price, remark="PRISM:" + intent)
            except OutcomeUnknown:
                with self.connect() as db:
                    db.execute("UPDATE intents SET state='UNKNOWN' WHERE intent=?", (intent,))
                raise
            except MoomooError:
                with self.connect() as db:
                    db.execute("UPDATE intents SET state='REJECTED' WHERE intent=?", (intent,))
                raise
            with self.connect() as db:
                db.execute("UPDATE intents SET state='ACKNOWLEDGED',order_id=? WHERE intent=?",
                           (ack["order_id"], intent))
            return next(r for r in self.rows(account) if r["intent"] == intent)

    async def cancel(self, account, intent):
        with self.locked():
            account = await self.broker.select_account(account)
            await self._reconcile(account)
            row = next((r for r in self.rows(account) if r["intent"] == intent), None)
            if not row or not row["order_id"]:
                raise MoomooError("Cancellation requires an exact known order ID.")
            if row["state"] not in {"ACKNOWLEDGED", "OPEN", "PARTIAL"}:
                return row
            with self.connect() as db:
                db.execute("UPDATE intents SET state='CANCEL_REQUESTED' WHERE intent=?", (intent,))
            await self.broker.cancel(account, row["order_id"])
            # A cancellation acknowledgement is not proof of cancellation or no fills.
            return next(r for r in self.rows(account) if r["intent"] == intent)
