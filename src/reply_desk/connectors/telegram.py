"""Telegram Bot API: approval cards with inline buttons, button taps, and edit replies.

Env:
  TELEGRAM_BOT_TOKEN  the bot's token from @BotFather
  TELEGRAM_CHAT_ID    the chat the cards go to (for a private chat with the bot, your user ID)

Calls (POST https://api.telegram.org/bot<token>/<method>): sendMessage, editMessageText,
answerCallbackQuery, getUpdates and getMe. Taps arrive by long polling (getUpdates), so no public
webhook URL is needed. The token is part of the URL, so errors name only the method.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from ..http import Http, HttpError, Transport

API = "https://api.telegram.org"


def recent_chats(token: str, transport: Transport | None = None, base: str = API) -> list[tuple[str, str]]:
    """Chats that messaged the bot lately, to find TELEGRAM_CHAT_ID during setup. Reads only."""
    data = Http(transport=transport).request("POST", "%s/bot%s/getUpdates" % (base.rstrip("/"), token),
                                             label="Telegram getUpdates", json_body={"timeout": 0})
    chats: dict[str, str] = {}
    for update in (data.get("result") if isinstance(data, dict) else None) or []:
        msg = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            chats[str(chat["id"])] = str(chat.get("username") or chat.get("title") or chat.get("first_name") or "")
    return list(chats.items())


def keyboard(buttons: Sequence[tuple[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}


class TelegramClient:
    def __init__(self, token: str | None = None, chat_id: str | None = None, transport: Transport | None = None,
                 base: str = API):
        token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            raise RuntimeError("set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        self.chat_id = str(chat_id)
        self._url = "%s/bot%s/" % (base.rstrip("/"), token)
        self.http = Http(transport=transport, timeout=60)

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        data = self.http.request("POST", self._url + method, label="Telegram " + method, json_body=payload)
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise HttpError("Telegram %s was not accepted" % method)
        return data.get("result")

    def _message(self, text: str) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True}

    def send_card(self, text: str, buttons: Sequence[tuple[str, str]]) -> str:
        return str(self._call("sendMessage", {**self._message(text), "reply_markup": keyboard(buttons)})["message_id"])

    def update_card(self, message_id: str, text: str, buttons: Sequence[tuple[str, str]] = ()) -> None:
        """Rewrite a card. Without buttons the inline keyboard is removed."""
        payload = {**self._message(text), "message_id": int(message_id)}
        if buttons:
            payload["reply_markup"] = keyboard(buttons)
        self._call("editMessageText", payload)

    def send_text(self, text: str, force_reply: bool = False) -> str:
        payload = self._message(text)
        if force_reply:
            payload["reply_markup"] = {"force_reply": True, "input_field_placeholder": "The new email text"}
        return str(self._call("sendMessage", payload)["message_id"])

    def answer(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        result = self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                           "allowed_updates": ["callback_query", "message"]})
        return [u for u in result or [] if isinstance(u, dict)]

    def ping(self) -> str:
        me = self._call("getMe", {}) or {}
        return "Telegram bot token works (@%s)" % me.get("username", "?")
