"""Real US consumer imports; run separately from the root test process."""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "prism-us"))


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setenv("PRISM_US_BROKER", "moomoo")
    monkeypatch.setenv("MOOMOO_ACCOUNT_ID", "987654321")
    monkeypatch.setenv("MOOMOO_BUY_AMOUNT_USD", "250")
    monkeypatch.setenv("MOOMOO_TRADING_MODE", "demo")
    monkeypatch.setenv("MOOMOO_ACCOUNT_NAME", "moomoo-us")


def test_us_tracking_and_reconcile_share_account_without_kis(environment, monkeypatch):
    # Use the suite's existing isolated loader for the overlapping KR/US packages.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    from test_us_stock_tracking_agent_process_reports import us_agent_module as module
    from prism_core.oneil_service import configured_accounts
    from tools.reconcile_fills import _accounts

    def forbidden():
        pytest.fail("Moomoo account discovery must not load KIS credentials")

    monkeypatch.setattr(module, "_get_kis_auth", forbidden)
    agent = object.__new__(module.USStockTrackingAgent)
    accounts = agent._get_trading_accounts()
    assert len(accounts) == 1
    assert accounts[0]["account_key"] == "moomoo:demo:987654321"
    assert accounts == configured_accounts()
    assert _accounts("US")[accounts[0]["account_key"]] == accounts[0]
    assert "987654321" not in agent._safe_account_log_label(accounts[0])


def test_moomoo_does_not_process_old_kis_pending_queue(environment, monkeypatch):
    import us_pending_order_batch as module

    def forbidden(*args, **kwargs):
        pytest.fail("Moomoo must not open the KIS pending queue")

    monkeypatch.setattr(module.sqlite3, "connect", forbidden)
    module.process_pending_orders(dry_run=False)


def test_fill_chaser_consumes_moomoo_order_shape(environment):
    from tools.fill_chaser import _inquire_open_orders

    class Client:
        def get_unfilled_orders(self):
            return [dict(order_no="9001", ticker="AAPL", sll_buy_dvsn_cd="01",
                         ord_qty=2, ccld_qty=1, nccs_qty=1, ord_unpr=100, exchange=None)]

    rows = asyncio.run(_inquire_open_orders(Client(), "US"))
    assert rows[0]["side"] == "SELL"
    assert rows[0]["unfilled_qty"] == 1 and rows[0]["ord_unpr"] == 100
