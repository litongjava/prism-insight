from datetime import datetime
import pytest
from tools import local_paper_automation as runner


def at(value):
    return datetime.fromisoformat(value)


def test_nyse_dst_holiday_early_close_and_no_catchup():
    assert runner.due(at('2026-11-17T14:45:00+00:00')) == ['morning']
    assert runner.due(at('2026-07-06T13:45:00+00:00')) == ['morning']
    assert runner.due(at('2026-11-17T15:01:00+00:00')) == []
    assert runner.due(at('2026-11-26T14:45:00+00:00')) == []
    assert runner.due(at('2026-11-27T16:30:00+00:00')) == ['afternoon']


def test_batch_claim_survives_unknown_process_result(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, 'STATE', tmp_path)
    assert runner.claim('day:morning')
    assert not runner.claim('day:morning')
    runner.finish('day:morning', 'ERROR', 124)
    assert not runner.claim('day:morning')


@pytest.mark.asyncio
async def test_ambiguous_notice_is_persisted_and_not_replayed(monkeypatch, tmp_path):
    from messaging import lark_delivery
    monkeypatch.setattr(runner, 'STATE', tmp_path)
    monkeypatch.setenv('LARK_CHAT_ID', 'oc_target')
    calls = []
    class Bot:
        async def send_message(self, *args, **kwargs):
            calls.append(kwargs)
            raise RuntimeError('connection lost after write')
    monkeypatch.setattr(lark_delivery, 'make_bot', Bot)
    await runner.notice('same-event', 'fill')
    await runner.notice('same-event', 'fill')
    assert len(calls) == 1 and calls[0]['message_uuid']
    with runner.database() as db:
        assert db.execute('SELECT state FROM notices').fetchone()[0] == 'UNKNOWN'


@pytest.mark.asyncio
async def test_outside_session_monitor_has_no_broker_or_order_calls(monkeypatch):
    async def forbidden():
        pytest.fail('must not contact broker')
    monkeypatch.setattr(runner, 'reconcile', forbidden)
    await runner.tick('monitor', at('2026-11-26T15:00:00+00:00'))
