"""Lark Python SDK delivery restricted to the configured PRISM group."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

from telegram.error import TelegramError
from messaging.telegram_delivery import TelegramDeliveryUnknown


def enabled():
    return os.getenv("PRISM_NOTIFICATION_PROVIDER", "telegram").lower() == "lark"


class LarkBot:
    """Keep SDK authentication and HTTP work off the application's event loop.

    An ambiguous message response is never automatically resent. A caller can
    supply a stable UUID for an explicitly reconciled outbox retry.
    """

    def __init__(self, *, client=None):
        import lark_oapi as lark
        self.chat_id = os.getenv("LARK_CHAT_ID", "")
        app_id = os.getenv("LARK_APP_ID", "")
        secret = os.getenv("LARK_APP_SECRET", "")
        if not all((app_id, secret, self.chat_id)):
            raise ValueError("Lark app credentials and target group are required")
        self.client = client or (
            lark.Client.builder().app_id(app_id).app_secret(secret)
            .domain(lark.LARK_DOMAIN).timeout(60).log_level(lark.LogLevel.CRITICAL).build()
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    def _destination(self, chat_id):
        if chat_id != self.chat_id:
            raise TelegramError("Lark destination does not match configured group")

    async def _send(self, msg_type, content, message_uuid=None):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        request = (
            CreateMessageRequest.builder().receive_id_type("chat_id")
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(self.chat_id).msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .uuid(message_uuid or str(uuid.uuid4())).build()).build()
        )
        try:
            response = await asyncio.to_thread(self.client.im.v1.message.create, request)
        except Exception:
            # Do not include SDK exception bodies: they may contain private data.
            raise TelegramDeliveryUnknown() from None
        if not response.success():
            raise TelegramError("Lark message rejected")
        mid = getattr(getattr(response, "data", None), "message_id", None)
        if not isinstance(mid, str) or not mid:
            raise TelegramDeliveryUnknown()
        return SimpleNamespace(message_id=mid)

    async def send_message(self, chat_id, text, *, message_uuid=None, **kwargs):
        self._destination(chat_id)
        return await self._send("text", {"text": text}, message_uuid)

    async def send_document(self, chat_id, document, caption=None, *, message_uuid=None, **kwargs):
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        self._destination(chat_id)
        filename = Path(getattr(document, "name", "report.pdf")).name
        position = document.tell()
        document.seek(0, 2)
        size = document.tell() - position
        document.seek(position)
        if size >= 30 * 1024 * 1024:
            raise TelegramError("Lark attachment must be smaller than 30 MB")
        request = CreateFileRequest.builder().request_body(
            CreateFileRequestBody.builder()
            .file_type("pdf" if filename.lower().endswith(".pdf") else "stream")
            .file_name(filename).file(document).build()
        ).build()
        try:
            response = await asyncio.to_thread(self.client.im.v1.file.create, request)
        except Exception:
            raise TelegramError("Lark file upload failed") from None
        key = getattr(getattr(response, "data", None), "file_key", None)
        if not response.success() or not key:
            raise TelegramError("Lark file upload failed")
        return await self._send("file", {"file_key": key}, message_uuid)


def make_bot(token=None, **kwargs):
    if enabled():
        return LarkBot()
    from telegram import Bot
    return Bot(token=token, **kwargs)
