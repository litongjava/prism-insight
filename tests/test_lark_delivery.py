import io
import json
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from telegram.error import TelegramError
from messaging.lark_delivery import LarkBot, make_bot
from messaging.telegram_delivery import TelegramDeliveryUnknown


@pytest.fixture
def setup(monkeypatch):
    for key, value in {'PRISM_NOTIFICATION_PROVIDER':'lark', 'LARK_APP_ID':'test',
                       'LARK_APP_SECRET':'secret', 'LARK_CHAT_ID':'oc_target'}.items():
        monkeypatch.setenv(key, value)
    message = Mock(return_value=NS(success=lambda: True, data=NS(message_id='om_test')))
    upload = Mock(return_value=NS(success=lambda: True, data=NS(file_key='file_test')))
    client = NS(im=NS(v1=NS(message=NS(create=message), file=NS(create=upload))))
    return LarkBot(client=client), message, upload


@pytest.mark.asyncio
async def test_sdk_text_is_scoped_idempotent_and_off_loop(setup):
    bot, message, _ = setup
    main_thread = threading.get_ident()
    def send(request):
        assert threading.get_ident() != main_thread
        assert request.body.receive_id == 'oc_target'
        assert request.body.uuid == 'stable'
        assert json.loads(request.body.content) == {'text':'模拟订单已受理'}
        return NS(success=lambda: True, data=NS(message_id='om_test'))
    message.side_effect = send
    with pytest.raises(TelegramError):
        await bot.send_message('other', 'private')
    message.assert_not_called()
    assert (await bot.send_message('oc_target', '模拟订单已受理', message_uuid='stable')).message_id == 'om_test'
    assert isinstance(make_bot(), LarkBot)


@pytest.mark.asyncio
async def test_sdk_upload_then_send_file(setup):
    bot, message, upload = setup
    document = io.BytesIO(b'%PDF-test')
    document.name = 'report.pdf'
    await bot.send_document('oc_target', document, message_uuid='file-event')
    request = upload.call_args.args[0]
    assert request.body.file is document
    assert request.body.file_type == 'pdf'
    sent = message.call_args.args[0].body
    assert sent.msg_type == 'file' and sent.uuid == 'file-event'
    assert json.loads(sent.content) == {'file_key':'file_test'}


@pytest.mark.asyncio
async def test_ambiguous_send_never_retries_or_exposes_errors(setup):
    bot, message, _ = setup
    message.side_effect = RuntimeError('private token')
    with pytest.raises(TelegramDeliveryUnknown) as error:
        await bot.send_message('oc_target', 'test')
    assert 'private' not in str(error.value)
    assert message.call_count == 1


@pytest.mark.asyncio
async def test_upload_failure_does_not_send_message(setup):
    bot, message, upload = setup
    upload.return_value = NS(success=lambda: False, data=None)
    with pytest.raises(TelegramError):
        await bot.send_document('oc_target', io.BytesIO(b'test'))
    message.assert_not_called()
