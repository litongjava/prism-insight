from pathlib import Path
import pytest
from trading.moomoo_paper import MoomooError, OutcomeUnknown
from trading.moomoo_paper_manager import PaperManager


class Broker:
    def __init__(self):
        self.orders, self.positions, self.calls = [], [], []
        self.unknown = False

    async def select_account(self, account=None):
        return account or '123'

    async def history_orders(self, account, since):
        return []

    async def snapshot(self, account):
        return {'positions': self.positions, 'orders': self.orders}

    async def submit_limit(self, account, symbol, side, qty, price, **kwargs):
        self.calls.append((symbol, side, qty, price, kwargs))
        if self.unknown:
            raise OutcomeUnknown('unknown')
        return {'order_id': '999'}

    async def cancel(self, account, order):
        self.calls.append(('cancel', order))
        return {'order_id': order}


def evidence(**kwargs):
    return dict(order_id='999', symbol='AAPL', market=2, side=1, order_type=1,
                qty='10', price='100', cum_qty='0', avg_fill_price='0', status=2,
                text='PRISM:test', **kwargs)


@pytest.mark.asyncio
async def test_ack_partial_fill_cancel_and_positions(tmp_path):
    b = Broker(); m = PaperManager(b, tmp_path)
    ack = await m.submit('123', 'test', 'AAPL', 'BUY', 10, '100')
    assert ack['state'] == 'ACKNOWLEDGED' and ack['filled'] is None
    b.orders = [evidence()]
    b.orders[0].update(status=3, cum_qty='4', avg_fill_price='99')
    b.positions = [dict(symbol='AAPL', market=2, pstn_type=0, qty='4', qty_avbl='4')]
    r = await m.reconcile('123')
    assert r['intents'][0]['filled'] == '4' and r['positions'][0]['quantity'] == 4
    await m.cancel('123', 'test')
    assert m.rows('123')[0]['state'] == 'CANCEL_REQUESTED'
    b.orders[0].update(status=7)
    r = await m.reconcile('123')
    assert r['intents'][0]['state'] == 'PARTIAL_CANCELLED'
    assert r['intents'][0]['filled'] == '4'


@pytest.mark.asyncio
async def test_restart_never_resends_same_intent(tmp_path):
    b = Broker(); m = PaperManager(b, tmp_path)
    await m.submit('123', 'test', 'AAPL', 'BUY', 10, '100')
    m = PaperManager(b, tmp_path)
    await m.submit('123', 'test', 'AAPL', 'BUY', 10, '100.00')
    assert len(b.calls) == 1
    with pytest.raises(MoomooError):
        await m.submit('123', 'test', 'AAPL', 'BUY', 11, '100')


@pytest.mark.asyncio
async def test_unknown_blocks_other_orders_and_resolves_by_exact_remark(tmp_path):
    b = Broker(); b.unknown = True; m = PaperManager(b, tmp_path)
    with pytest.raises(OutcomeUnknown):
        await m.submit('123', 'test', 'AAPL', 'BUY', 10, '100')
    with pytest.raises(MoomooError):
        await m.submit('123', 'other', 'SPY', 'BUY', 1, '100')
    assert len(b.calls) == 1
    b.orders = [evidence()]
    r = await m.reconcile('123')
    assert r['intents'][0]['order_id'] == '999'
    assert r['intents'][0]['state'] == 'OPEN'


@pytest.mark.asyncio
async def test_cap_and_no_short_and_incomplete_evidence(tmp_path):
    b = Broker(); m = PaperManager(b, tmp_path)
    with pytest.raises(MoomooError):
        await m.submit('123', 'over', 'AAPL', 'BUY', 51, '100')
    with pytest.raises(MoomooError):
        await m.submit('123', 'short', 'AAPL', 'SELL', 1, '100')
    assert not b.calls
    await m.submit('123', 'test', 'AAPL', 'BUY', 10, '100')
    b.orders = [evidence()]
    b.orders[0].update(status=4, cum_qty=None)
    with pytest.raises(MoomooError):
        await m.reconcile('123')
    assert m.rows('123')[0]['filled'] is None


@pytest.mark.asyncio
async def test_old_orders_missing_today_stay_unresolved(tmp_path):
    b = Broker(); m = PaperManager(b, tmp_path)
    await m.submit('123', 'test', 'AAPL', 'BUY', 10, '100')
    await m.reconcile('123')
    with pytest.raises(MoomooError):
        await m.submit('123', 'second', 'AAPL', 'BUY', 1, '100')
    assert len(b.calls) == 1


@pytest.mark.asyncio
async def test_confirmed_sell_and_fill_regression(tmp_path):
    b = Broker(); m = PaperManager(b, tmp_path)
    b.positions = [dict(symbol='AAPL', market=2, pstn_type=0, qty='10', qty_avbl='10')]
    await m.submit('123', 'exit', 'AAPL', 'SELL', 10, '100')
    b.orders = [evidence()]; b.orders[0].update(side=2, status=4, cum_qty='10', avg_fill_price='100')
    r = await m.reconcile('123')
    assert r['intents'][0]['state'] == 'FILLED'
    b.orders[0].update(cum_qty='9')
    with pytest.raises(MoomooError):
        await m.reconcile('123')
    assert m.rows('123')[0]['filled'] == '10'

@pytest.mark.asyncio
async def test_cancel_not_replayed_while_broker_still_open(tmp_path):
    b = Broker(); m = PaperManager(b, tmp_path)
    await m.submit('123', 'test', 'AAPL', 'BUY', 10, '100')
    b.orders = [evidence()]
    await m.cancel('123', 'test')
    await m.cancel('123', 'test')
    assert len(b.calls) == 2
    assert m.rows('123')[0]['state'] == 'CANCEL_REQUESTED'


@pytest.mark.asyncio
async def test_external_pending_order_blocks_duplicate_symbol(tmp_path):
    b = Broker(); m = PaperManager(b, tmp_path)
    b.orders = [evidence()]
    with pytest.raises(MoomooError):
        await m.submit('123', 'other', 'AAPL', 'BUY', 10, '100')
    assert b.calls == []

@pytest.mark.asyncio
async def test_historical_fill_resolves_prior_day(tmp_path):
    b = Broker(); m = PaperManager(b, tmp_path)
    await m.submit('123', 'test', 'AAPL', 'BUY', 10, '100')
    async def history(account, since):
        row = evidence(); row.update(status=4, cum_qty='10', avg_fill_price='100')
        return [row]
    b.history_orders = history
    r = await m.reconcile('123')
    assert r['intents'][0]['state'] == 'FILLED'
