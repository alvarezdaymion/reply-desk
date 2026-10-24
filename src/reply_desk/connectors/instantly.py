"""Instantly API v2: read replies and threads, send a reply in a thread, read a message back.

Env:
  INSTANTLY_API_KEY   an API key with the emails scopes (read and create). The name lookup needs
                      leads:read, and inbox.blocklist_unsubscribes needs block list access.

Calls (base https://api.instantly.ai/api/v2):
  GET  /emails?email_type=received&min_timestamp_created=..  new replies, oldest first, paged
  GET  /emails?search=thread:<id>&lead=<email>               one thread: restart recovery
  GET  /emails?lead=<email>                                all threads: pre-send and follow-up freshness
  GET  /emails/<id>                                          read one message back by its ID
  POST /leads/list {"contacts": [email]}                     a lead's first name and company (a query)
  POST /emails/reply                                         the only send; only SafeSender calls it
  POST /block-lists-entries {"bl_value": email}              only with inbox.blocklist_unsubscribes
                                                             in live mode

Instantly sends a reply to the From address of the message it replies to, in the same thread.
The list endpoint allows 20 requests a minute, so keep inbox.poll_seconds at 60 or more.
"""

from __future__ import annotations

import html
import os
import re
import urllib.parse
from typing import Any

from ..http import Http, HttpError, Transport

BASE = "https://api.instantly.ai/api/v2"
PAGE = 100


def html_to_text(value: Any) -> str:
    text = re.sub(r"<br\s*/?>|</p\s*>|</div\s*>", "\n", str(value or ""), flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def reply_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """The fields Reply Desk keeps from an Instantly email object."""
    body = item.get("body") if isinstance(item.get("body"), dict) else {}
    text = body.get("text") or html_to_text(body.get("html")) or str(item.get("content_preview") or "")
    return {
        "provider_id": str(item.get("id") or ""),
        "thread_id": str(item.get("thread_id") or ""),
        "lead_email": str(item.get("from_address_email") or item.get("lead") or "").strip().lower(),
        "inbox": str(item.get("eaccount") or ""),
        "subject": str(item.get("subject") or ""),
        "body": text,
        "received_at": str(item.get("timestamp_created") or ""),
        "auto_reply": item.get("is_auto_reply") in (1, True, "1", "true"),
        "campaign_id": str(item.get("campaign_id") or ""),
        "truncated": bool(item.get("body_fallback_reason")) or not bool(body.get("text") or body.get("html")),
    }


class InstantlyClient:
    def __init__(self, api_key: str | None = None, transport: Transport | None = None, base: str = BASE):
        key = api_key or os.environ.get("INSTANTLY_API_KEY", "")
        if not key:
            raise RuntimeError("set INSTANTLY_API_KEY")
        self.base = base.rstrip("/")
        self.http = Http({"Authorization": "Bearer " + key}, transport)

    def _items(self, params: dict[str, Any], label: str) -> tuple[list[dict[str, Any]], str]:
        data = self.http.request("GET", self.base + "/emails", label=label, params=params)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise HttpError("%s returned an unexpected shape" % label)
        cursor = data.get("next_starting_after")
        if any(not isinstance(i, dict) for i in data["items"]) or (cursor is not None and not isinstance(cursor, str)):
            raise HttpError("%s returned an unexpected shape" % label)
        return data["items"], cursor or ""

    def list_reply_page(self, since: str, starting_after: str = "") -> tuple[list[dict[str, Any]], str]:
        params: dict[str, Any] = {"email_type": "received", "sort_order": "asc", "limit": PAGE,
                                  "min_timestamp_created": since, "preview_only": "false"}
        if starting_after:
            params["starting_after"] = starting_after
        return self._items(params, "Instantly list replies")

    def list_replies(self, since: str) -> list[dict[str, Any]]:
        """Received emails created at or after `since` (ISO 8601), oldest first."""
        cursor = ""
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        while True:
            page, nxt = self.list_reply_page(since, cursor)
            items.extend(page)
            if not nxt:
                return items
            if nxt in seen:
                raise HttpError("Instantly list replies returned a repeated cursor")
            seen.add(nxt)
            cursor = nxt

    def thread(self, thread_id: str, lead_email: str) -> list[dict[str, Any]]:
        """All messages for a lead; a nonempty thread_id narrows the read for send recovery."""
        params: dict[str, Any] = {"lead": lead_email, "sort_order": "asc", "limit": PAGE, "preview_only": "false"}
        if thread_id:
            params["search"] = "thread:" + thread_id
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            page, cursor = self._items(params, "Instantly read thread")
            items.extend(page)
            if not cursor:
                return items
            if cursor in seen:
                raise HttpError("Instantly read thread returned a repeated cursor")
            seen.add(cursor)
            params["starting_after"] = cursor

    def get_email(self, email_id: str) -> dict[str, Any]:
        data = self.http.request("GET", self.base + "/emails/" + urllib.parse.quote(email_id, safe=""),
                                 label="Instantly get email")
        if not isinstance(data, dict):
            raise HttpError("Instantly get email returned an unexpected shape")
        return data

    def lead(self, email: str) -> dict[str, str]:
        data = self.http.request("POST", self.base + "/leads/list", label="Instantly lead lookup",
                                 json_body={"contacts": [email], "limit": 1})
        for item in (data.get("items") if isinstance(data, dict) else None) or []:
            if isinstance(item, dict) and str(item.get("email") or "").lower() == email.lower():
                return {"first_name": str(item.get("first_name") or ""), "company": str(item.get("company_name") or "")}
        return {}

    def send_reply(self, *, reply_to_id: str, sender: str, subject: str, body: str) -> dict[str, Any]:
        """POST /emails/reply. Only SafeSender calls this, after the intent is in the ledger."""
        text = body.replace("\r\n", "\n").replace("\r", "\n")
        payload = {
            "reply_to_uuid": reply_to_id,
            "eaccount": sender,
            "subject": subject,
            # Escape first so text can never become markup, then keep every line break.
            "body": {"text": text, "html": html.escape(text, quote=False).replace("\n", "<br/>")},
        }
        data = self.http.request("POST", self.base + "/emails/reply", label="Instantly send reply", json_body=payload)
        return data if isinstance(data, dict) else {}

    def block(self, email: str) -> None:
        self.http.request("POST", self.base + "/block-lists-entries", label="Instantly block list",
                          json_body={"bl_value": email.strip().lower()})

    def ping(self) -> str:
        self._items({"limit": 1}, "Instantly list emails")
        return "Instantly API key works (read one page of emails)"
