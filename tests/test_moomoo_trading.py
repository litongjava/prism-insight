import asyncio
from dataclasses import replace
import inspect
import sqlite3
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from prism_core.moomoo_trading import MoomooConfig, MoomooStockTrading, MoomooUnavailable
from prism_core.execution_service import ExecutionService
from prism_core.order_intents import IntentStore, OrderIntent


def frame(rows):
    return 0, pd.DataFrame(rows)


def order(**changes):
    row = dict(order_id="9001", code="US.AAPL", trd_side="BUY", qty=2,
               dealt_qty=0, dealt_avg_price=0, price=100, order_status="SUBMITTED",
               create_time="2026-11-13 10:00:00", currency="USD")
    return dict(row, **changes)


class Trade:
    def __init__(self):
        self.accounts = [dict(acc_id=123, trd_env="SIMULATE", acc_status="ACTIVE",
                              trdmarket_auth=["US"], security_firm="N/A", sim_acc_type="STOCK_AND_OPTION")]
        self.positions = [dict(code="US.AAPL", qty=5, can_sell_qty=3)]
        self.orders = [order()]
        self.calls = []
        self.closed = False
        self.fail_place = False
        self.fail_positions = False

    def get_acc_list(self):
        return frame(self.accounts)

    def position_list_query(self, **kwargs):
        self.calls.append(("positions", kwargs))
        return (-1, "private account error") if self.fail_positions else frame(self.positions)

    def place_order(self, **kwargs):
        self.calls.append(("place", kwargs))
        if self.fail_place:
            raise TimeoutError("private secret")
        return frame([order(code=kwargs["code"], trd_side=kwargs["trd_side"], qty=kwargs["qty"])])

    def order_list_query(self, **kwargs):
        self.calls.append(("orders", kwargs))
        return frame([r for r in self.orders if not kwargs.get("order_id") or r["order_id"] == kwargs["order_id"]])

    def history_order_list_query(self, **kwargs):
        self.calls.append(("history", kwargs))
        return frame(self.orders)

    def accinfo_query(self, **kwargs):
        self.calls.append(("funds", kwargs))
        return frame([dict(total_assets=1000, cash=500, power=500)])

    def modify_order(self, **kwargs):
        self.calls.append(("modify", kwargs))
        return frame([dict(order_id=kwargs["order_id"])])

    def close(self):
        self.closed = True


class Quote:
    def __init__(self):
        self.closed = False
        self.price = 100

    def get_market_snapshot(self, codes):
        return frame([dict(code=codes[0], last_price=self.price, update_time="2026-11-13 10:00:00")])

    def close(self):
        self.closed = True


@pytest.fixture
def setup():
    trade, quote = Trade(), Quote()
    sdk = SimpleNamespace(
        RET_OK=0, TrdEnv=SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL"),
        TrdMarket=SimpleNamespace(US="US"), SecurityFirm=SimpleNamespace(FUTUINC="FUTUINC"),
        TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
        OrderType=SimpleNamespace(NORMAL="NORMAL", MARKET="MARKET"),
        TimeInForce=SimpleNamespace(DAY="DAY"), Session=SimpleNamespace(RTH="RTH"),
        Currency=SimpleNamespace(USD="USD"), ModifyOrderOp=SimpleNamespace(CANCEL="CANCEL", NORMAL="NORMAL"),
        OpenSecTradeContext=lambda **kwargs: trade, OpenQuoteContext=lambda **kwargs: quote,
    )
    return MoomooConfig(123, 250), sdk, trade, quote


def trader(setup):
    config, sdk, _, _ = setup
    return MoomooStockTrading(config, sdk=sdk)


def intent(account="moomoo:demo:123", **kwargs):
    return OrderIntent.create(market="US", account_id=account, symbol="AAPL", side="BUY",
                              order_style="limit", source="test", source_decision_id="decision-1",
                              quantity=2, limit_price=100, **kwargs)


def test_factory_never_imports_kis(monkeypatch, tmp_path):
    import builtins
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert "kis_auth" not in name and "us_stock_trading" not in name
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setenv("PRISM_US_BROKER", "moomoo")
    service = ExecutionService.us(account_name="moomoo-us", db_path=tmp_path / "orders.db")
    assert service._resource.account_name == "moomoo-us"
    with pytest.raises(ValueError, match="originating"):
        ExecutionService.us(db_path=tmp_path / "other.db", intent_store=service._intent_store)


@pytest.mark.parametrize("changes", [
    {"trd_env": "REAL"}, {"acc_id": 456}, {"acc_status": "DISABLED"},
    {"trdmarket_auth": ["HK"]}, {"sim_acc_type": "FUTURES"},
])
def test_account_mismatch_closes_before_quote_or_orders(setup, changes):
    config, sdk, trade, quote = setup
    trade.accounts[0].update(changes)
    with pytest.raises(MoomooUnavailable):
        MoomooStockTrading(config, sdk=sdk)
    assert trade.closed and not trade.calls


def test_real_account_requires_explicit_opt_in(setup):
    config, sdk, trade, _ = setup
    trade.accounts[0].update(trd_env="REAL", security_firm="FUTUINC")
    client = MoomooStockTrading(replace(config, mode="real"), sdk=sdk)
    result = asyncio.run(client.async_buy_stock("AAPL", limit_price=100))
    assert result["message"] == "MOOMOO_LIVE_NOT_ENABLED"
    assert not any(name == "place" for name, _ in trade.calls)


def test_wrong_real_firm_rejected(setup):
    config, sdk, trade, _ = setup
    trade.accounts[0].update(trd_env="REAL", security_firm="FUTUSG")
    with pytest.raises(MoomooUnavailable, match="FIRM"):
        MoomooStockTrading(replace(config, mode="real"), sdk=sdk)


def test_buy_service_persists_submission_and_blocks_duplicate(setup, tmp_path):
    client = trader(setup)
    store = IntentStore(tmp_path / "ledger.db")
    service = ExecutionService(client, intent_store=store)
    validators = []

    async def run():
        first = await service.execute_buy("AAPL", buy_amount=250, limit_price=100,
                                           intent=intent(), quote_validator=validators.append, strict_budget=True)
        second = await service.execute_buy("AAPL", buy_amount=250, limit_price=100, intent=intent())
        return first, second

    first, second = asyncio.run(run())
    assert first["intent_status"] == "SUBMITTED" and first["intent_broker"] == "MOOMOO"
    assert first["filled_quantity"] is None
    assert validators == [100, 100]
    places = [params for name, params in setup[2].calls if name == "place"]
    assert len(places) == 1
    assert places[0]["acc_id"] == 123 and places[0]["trd_env"] == "SIMULATE"
    assert places[0]["qty"] == 2 and places[0]["session"] == "RTH"
    assert first["total_amount"] <= 250 and not second["success"]
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT broker, status FROM broker_orders").fetchall() == [("MOOMOO", "SUBMITTED")]
    from observability.entry_quality import build_fill_provenance
    assert build_fill_provenance(first)["status"] == "SUBMITTED_ONLY"


@pytest.mark.parametrize("changes", [
    {"buy_amount": 99}, {"buy_amount": float("nan")}, {"limit_price": 100.001},
    {"strict_budget": True, "limit_price": None}, {"exact_quantity": 3},
    {"quote_validator": lambda value: (_ for _ in ()).throw(ValueError())},
])
def test_buy_rejections_never_submit(setup, changes):
    client = trader(setup)
    params = dict(ticker="AAPL", buy_amount=250, limit_price=100)
    params.update(changes)
    assert not asyncio.run(client.async_buy_stock(**params))["success"]
    assert not any(name == "place" for name, _ in setup[2].calls)


def test_wrong_intent_account_rejected_before_submission(setup, tmp_path):
    client = trader(setup)
    service = ExecutionService(client, intent_store=IntentStore(tmp_path / "orders.db"))
    with pytest.raises(ValueError, match="ACCOUNT_MISMATCH"):
        asyncio.run(service.execute_buy("AAPL", intent=intent(account="vps:old:01")))
    assert not setup[2].calls


def test_submission_timeout_is_unknown_and_not_retried(setup, tmp_path):
    client = trader(setup)
    setup[2].fail_place = True
    service = ExecutionService(client, intent_store=IntentStore(tmp_path / "orders.db"))

    async def run():
        result = await service.execute_buy("AAPL", limit_price=100, intent=intent())
        again = await service.execute_buy("AAPL", limit_price=100, intent=intent())
        return result, again

    result, again = asyncio.run(run())
    assert result["intent_status"] == "UNKNOWN" and result["intent_broker"] == "MOOMOO"
    assert "secret" not in str(result)
    assert not again["success"]
    assert sum(name == "place" for name, _ in setup[2].calls) == 1


def test_partial_sell_preserves_quantity_and_prevents_short(setup):
    client = trader(setup)
    result = asyncio.run(client.async_sell_stock("AAPL", limit_price=100, quantity=2))
    assert result["success"] and result["quantity"] == 2
    assert not asyncio.run(client.async_sell_stock("AAPL", limit_price=100, quantity=4))["success"]
    assert sum(name == "place" for name, _ in setup[2].calls) == 1


@pytest.mark.parametrize("qty", [None, "", float("nan"), 1.5, -1])
def test_bad_position_is_unknown_not_zero(setup, qty):
    client = trader(setup)
    setup[2].positions[0]["qty"] = qty
    with pytest.raises(MoomooUnavailable):
        client.get_holding_quantity("AAPL")
    assert not asyncio.run(client.async_sell_stock("AAPL", limit_price=100))["success"]


def test_failed_position_query_never_sells(setup):
    client = trader(setup)
    setup[2].fail_positions = True
    with pytest.raises(MoomooUnavailable):
        client.get_portfolio()
    assert not asyncio.run(client.async_sell_stock("AAPL", limit_price=100))["success"]
    assert not any(name == "place" for name, _ in setup[2].calls)


def test_moo_is_not_silently_changed_to_market(setup):
    client = trader(setup)
    result = asyncio.run(client.async_sell_stock("AAPL", use_moo=True))
    assert result["message"] == "MOOMOO_MOO_NOT_SUPPORTED" and not setup[2].calls


@pytest.mark.parametrize("state,filled,status,remaining", [
    ("SUBMITTED", 0, "PENDING", 2), ("FILLED_PART", 1, "PARTIAL", 1),
    ("FILLED_ALL", 2, "FILLED", 0), ("CANCELLED_PART", 1, "CANCELLED", 0),
    ("CANCELLED_ALL", 0, "CANCELLED", 0), ("FAILED", 0, "REJECTED", 0),
])
def test_native_reconciliation(setup, state, filled, status, remaining):
    from prism_core.moomoo_evidence import reconcile
    client = trader(setup)
    setup[2].orders = [order(order_status=state, dealt_qty=filled, dealt_avg_price=99)]
    result = reconcile(client, intent(), "9001", "20261113")
    assert result["status"] == status
    assert result["filled_qty"] == filled and result["remaining_qty"] == remaining
    assert result["fees"] is None


@pytest.mark.parametrize("changes", [
    {"code": "US.MSFT"}, {"qty": 3}, {"currency": "HKD"}, {"trd_side": "SELL"},
    {"dealt_qty": None}, {"dealt_qty": 3}, {"order_status": "FILLED_ALL", "dealt_qty": 1},
])
def test_reconcile_mismatch_keeps_unknown(setup, changes):
    from prism_core.moomoo_evidence import reconcile
    client = trader(setup)
    setup[2].orders = [order(**changes)]
    result = reconcile(client, intent(), "9001", "20261113")
    assert result["status"] == "UNKNOWN" and result["filled_qty"] is None


def test_cancel_is_scoped_to_remaining_and_account(setup):
    client = trader(setup)
    setup[2].orders = [order(order_status="FILLED_PART", dealt_qty=1, dealt_avg_price=100)]
    assert not client.cancel_order("MSFT", "9001", 1)["success"]
    assert not client.cancel_order("AAPL", "9001", 2)["success"]
    assert client.cancel_order("AAPL", "9001", 1)["success"]
    mutation = [p for name, p in setup[2].calls if name == "modify"]
    assert len(mutation) == 1
    assert mutation[0]["qty"] == 2 and mutation[0]["acc_id"] == 123


def test_daily_reconcile_requires_exact_id_and_terminal_state(setup):
    from prism_core.moomoo_evidence import normalized_daily_rows
    from observability.fill_reconciliation import reconcile_target, build_attributes
    client = trader(setup)
    target = dict(id="x", symbol="AAPL", quantity=2, submitted_quantity=2, order_no="9001", broker="MOOMOO")
    rows = normalized_daily_rows(client, "20261113", "20261113")
    assert reconcile_target(target, rows, market="US")["fill_status"] == "MISSING"
    setup[2].orders = [order(order_status="FILLED_ALL", dealt_qty=2, dealt_avg_price=100)]
    rows = normalized_daily_rows(client, "20261113", "20261113")
    assert reconcile_target(dict(target, order_no="other"), rows, market="US")["fill_status"] == "MISSING"
    result = reconcile_target(target, rows, market="US")
    assert result["fill_status"] == "CONFIRMED" and result["filled_qty"] == 2
    attrs = build_attributes(target, result, market="US", execution_profile_ref="masked")
    assert attrs["fill_provenance"]["broker"] == "MOOMOO"


def test_sdk_contract_matches_installed_optional_dependency(setup):
    sdk = pytest.importorskip("moomoo")
    client = trader(setup)
    client._buy("AAPL", limit_price=100)
    client.get_portfolio()
    client.get_account_summary()
    client.order_rows()
    client.order_rows("2026-11-13", "2026-11-13")
    client.cancel_order("AAPL", "9001", 2)
    methods = {"place": "place_order", "positions": "position_list_query", "orders": "order_list_query",
               "history": "history_order_list_query", "funds": "accinfo_query", "modify": "modify_order"}
    for name, kwargs in setup[2].calls:
        inspect.signature(getattr(sdk.OpenSecTradeContext, methods[name])).bind(None, **kwargs)
    assert sdk.SecurityFirm.FUTUINC == "FUTUINC" and sdk.Session.RTH == "RTH"


@pytest.mark.parametrize("timeout", [0, -1, None, float("nan")])
def test_invalid_timeout_never_starts_worker(setup, timeout):
    result = asyncio.run(trader(setup).async_buy_stock("AAPL", timeout=timeout))
    assert result["message"] == "MOOMOO_TIMEOUT_INVALID" and not setup[2].calls


def test_timeout_keeps_worker_alive_and_context_drains_it(setup, monkeypatch):
    from prism_core.moomoo_trading import AsyncMoomooTradingContext
    client = trader(setup)
    release = threading.Event()
    original = setup[2].place_order

    def slow_place(**kwargs):
        assert release.wait(2)
        return original(**kwargs)

    monkeypatch.setattr(setup[2], "place_order", slow_place)

    async def run():
        context = AsyncMoomooTradingContext()
        context.trader = client
        result = await client.async_buy_stock("AAPL", limit_price=100, timeout=.01)
        assert result["outcome_unknown"] and not setup[2].closed
        assert client._pending
        release.set()
        await context.__aexit__()
        assert setup[2].closed and setup[3].closed

    asyncio.run(run())
    assert sum(name == "place" for name, _ in setup[2].calls) == 1


def test_unfilled_orders_preserve_sell_side_and_price(setup):
    setup[2].orders = [order(trd_side="SELL", order_status="FILLED_PART", dealt_qty=1, dealt_avg_price=100)]
    row = trader(setup).get_unfilled_orders()[0]
    assert row["sll_buy_dvsn_cd"] == "01" and row["ord_unpr"] == 100
    assert row["nccs_qty"] == 1


def test_reserved_order_forwards_validator(setup):
    def reject(price):
        raise ValueError("gate rejected")
    result = trader(setup).buy_reserved_order("AAPL", 100, 250, quote_validator=reject)
    assert not result["success"] and not any(n == "place" for n, _ in setup[2].calls)


def test_mismatched_modify_ack_remains_unknown(setup, monkeypatch):
    monkeypatch.setattr(setup[2], "modify_order", lambda **kwargs: frame([dict(order_id="other")]))
    result = trader(setup).cancel_order("AAPL", "9001", 2)
    assert result["outcome_unknown"]


def test_read_only_connection_check_never_submits(setup, monkeypatch, capsys):
    import prism_core.moomoo_trading as module
    from tools.check_moomoo_connection import main
    client = trader(setup)
    monkeypatch.setattr(module, "MoomooStockTrading", lambda: client)
    assert main([]) == 0
    output = capsys.readouterr().out
    assert '"orders_submitted": 0' in output and "123" not in output
    assert not any(n in {"place", "modify"} for n, _ in setup[2].calls)
    assert setup[2].closed


def test_oneil_and_daily_reconciler_use_native_evidence(setup, tmp_path):
    from prism_core.oneil_broker import OneilBroker
    from tools.reconcile_fills import _account_rows
    client = trader(setup)
    setup[2].orders = [order(order_status="FILLED_ALL", dealt_qty=2, dealt_avg_price=100)]
    store = IntentStore(tmp_path / "orders.db")
    service = ExecutionService(client, intent_store=store)
    broker = OneilBroker("moomoo-us", store, service_factory=lambda **kwargs: service)
    request = intent(execution_mode="live")

    async def run():
        held = await broker.holdings("AAPL")
        assert held["quantity"] == 5 and held["open_orders_count"] == 0
        assert await broker.discover_order_date(request, "9001") == "20261113"
        result = await broker.reconcile(request, "9001", "20261113")
        assert result["filled_qty"] == 2 and result["status"] == "FILLED"
        ok, rows, reason = await _account_rows(
            "US", client.account_config, "20261113", service_factory=lambda *args: service)
        assert ok and reason is None and rows[0]["filled_qty"] == 2

    asyncio.run(run())
