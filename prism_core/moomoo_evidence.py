"""Normalize exact moomoo order observations without inventing KIS evidence."""
from datetime import datetime, timezone
from decimal import Decimal

from prism_core.moomoo_trading import MoomooUnavailable, MoomooStockTrading, code, number


def order_facts(row):
    symbol = str(row.get("code", ""))
    side = row.get("trd_side")
    state = row.get("order_status")
    if (not symbol.startswith("US.") or side not in {"BUY", "SELL"}
            or row.get("currency") != "USD" or not str(row.get("order_id") or "")):
        raise MoomooUnavailable("MOOMOO_ORDER_IDENTITY_INVALID")
    ordered = number(row.get("qty"), integer=True, positive=True)
    filled = number(row.get("dealt_qty"), integer=True)
    if filled > ordered or state not in MoomooStockTrading._OPEN | MoomooStockTrading._TERMINAL:
        raise MoomooUnavailable("MOOMOO_ORDER_STATE_INVALID")
    if (state == "FILLED_ALL" and filled != ordered) or (state in {"FILLED_PART", "CANCELLED_PART"} and not 0 < filled < ordered):
        raise MoomooUnavailable("MOOMOO_FILL_STATE_INCONSISTENT")
    if state in {"FAILED", "DISABLED", "DELETED", "CANCELLED_ALL"} and filled:
        raise MoomooUnavailable("MOOMOO_FILL_STATE_INCONSISTENT")
    average = number(row.get("dealt_avg_price"), positive=True) if filled else None
    created = datetime.fromisoformat(str(row.get("create_time", "")))
    terminal = state in MoomooStockTrading._TERMINAL
    return dict(order_no=str(row["order_id"]), orig_order_no="", symbol=symbol[3:],
                side="02" if side == "BUY" else "01", ordered_qty=ordered, filled_qty=filled,
                remaining_qty=0 if terminal else ordered - filled, rejected_qty=None,
                filled_amount=average * filled if filled else Decimal(0), avg_price=average,
                order_date=created.strftime("%Y%m%d"), order_time=created.strftime("%H%M%S"),
                cancelled=state in {"CANCELLED_PART", "CANCELLED_ALL", "DELETED"},
                terminal=terminal, broker_status=state)


def normalized_daily_rows(service, start, end):
    first = datetime.strptime(start, "%Y%m%d").strftime("%Y-%m-%d")
    last = datetime.strptime(end, "%Y%m%d").strftime("%Y-%m-%d")
    rows = service.order_rows(first, last)
    result = [order_facts(row) for row in rows]
    if len({r["order_no"] for r in result}) != len(result):
        raise MoomooUnavailable("MOOMOO_DUPLICATE_ORDER")
    return result


def exact_order(service, intent, order_id, order_date=None):
    if intent.account_id != service.account_key or intent.market != "US":
        raise MoomooUnavailable("MOOMOO_INTENT_ACCOUNT_MISMATCH")
    if order_date:
        day = datetime.strptime(order_date, "%Y%m%d").strftime("%Y-%m-%d")
        row = service.get_order(order_id, start=day, end=day)
    else:
        row = service.get_order(order_id)
    facts = order_facts(row)
    if (row["code"] != code(intent.symbol) or row["trd_side"] != intent.side
            or facts["ordered_qty"] != intent.quantity
            or (order_date and facts["order_date"] != order_date)):
        raise MoomooUnavailable("MOOMOO_EXACT_ORDER_MISMATCH")
    return facts


def reconcile(service, intent, order_id, order_date=None):
    result = dict(status="UNKNOWN", broker="MOOMOO", broker_order_id=str(order_id),
                  order_date=order_date, broker_order_date=order_date, intent_id=intent.id,
                  virtual=False, account_id=intent.account_id, symbol=intent.symbol, side=intent.side,
                  filled_qty=None, filled_quantity=None, filled_notional=None, remaining_qty=None,
                  fees=None, fees_status="UNKNOWN", observed_at=datetime.now(timezone.utc).isoformat())
    try:
        row = exact_order(service, intent, order_id, order_date)
        state = row["broker_status"]
        if state == "FILLED_ALL":
            status = "FILLED"
        elif row["cancelled"]:
            status = "CANCELLED"
        elif state in {"FAILED", "DISABLED"}:
            status = "REJECTED"
        else:
            status = "PARTIAL" if row["filled_qty"] else "PENDING"
        result.update(status=status, filled_qty=row["filled_qty"], filled_quantity=row["filled_qty"],
                      filled_notional=str(row["filled_amount"]), remaining_qty=row["remaining_qty"],
                      order_date=row["order_date"], broker_order_date=row["order_date"],
                      fill_evidence_status="CONFIRMED")
    except Exception:
        result["reason"] = "MOOMOO_EXACT_FILL_UNCONFIRMED"
    return result


def holdings(service, symbol):
    from prism_core.oneil_adaptive_policy import _hash
    result = dict(status="UNKNOWN", account_id=service.account_key, symbol=symbol,
                  quantity=None, observed_at=datetime.now(timezone.utc).isoformat())
    try:
        quantity = service.get_holding_quantity(symbol)
        pending = service.get_unfilled_orders(symbol)
        result.update(status="OK", quantity=quantity, open_orders_status="OK", open_orders_count=len(pending))
    except Exception:
        result["reason"] = "MOOMOO_HOLDINGS_UNCONFIRMED"
    result["source_ref"] = _hash(result)
    return result
