"""Instantly and Telegram clients against fake transports. No request leaves the process."""

import json
import urllib.parse

import pytest

from reply_desk.connectors.instantly import InstantlyClient, reply_from_item
from reply_desk.connectors.telegram import TelegramClient, recent_chats
from reply_desk.http import HttpError

KEY = "test-key"
TOKEN = "123:test-token"


class FakeTransport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "json": json.loads(body) if body else None})
        status, payload = self.responses.pop(0) if self.responses else (200, {})
        return status, json.dumps(payload).encode()

    def query(self, i=0):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.calls[i]["url"]).query))


def instantly(*responses):
    t = FakeTransport(*responses)
    return InstantlyClient(api_key=KEY, transport=t), t


def test_list_replies_reads_received_emails_oldest_first_and_pages():
    page1 = {"items": [{"id": "e%d" % i} for i in range(100)], "next_starting_after": "cursor-1"}
    client, t = instantly((200, page1), (200, {"items": [{"id": "e100"}]}))
    items = client.list_replies("2026-10-21T00:00:00Z")
    assert len(items) == 101
    q = t.query(0)
    assert (q["email_type"], q["sort_order"], q["min_timestamp_created"]) == ("received", "asc", "2026-10-21T00:00:00Z")
    assert t.query(1)["starting_after"] == "cursor-1"
    assert t.calls[0]["headers"]["Authorization"] == "Bearer " + KEY
    assert all(c["method"] == "GET" for c in t.calls)


def test_thread_searches_by_thread_and_lead():
    client, t = instantly((200, {"items": []}))
    client.thread("th-1", "pat@example.com")
    assert t.query()["search"] == "thread:th-1" and t.query()["lead"] == "pat@example.com"


def test_send_reply_posts_one_reply_in_the_thread():
    client, t = instantly((200, {"id": "out-1"}))
    assert client.send_reply(reply_to_id="in-1", sender="sam@example.org", subject="Re: Hi",
                             body="Hi Pat,\n\n<b>Thanks</b> & bye")["id"] == "out-1"
    [call] = t.calls
    assert call["method"] == "POST" and call["url"].endswith("/api/v2/emails/reply")
    assert call["json"] == {"reply_to_uuid": "in-1", "eaccount": "sam@example.org", "subject": "Re: Hi",
                            "body": {"text": "Hi Pat,\n\n<b>Thanks</b> & bye",
                                     "html": "Hi Pat,<br/><br/>&lt;b&gt;Thanks&lt;/b&gt; &amp; bye"}}


def test_get_email_quotes_the_id_and_lead_lookup_is_a_query():
    client, t = instantly((200, {"id": "a/b"}), (200, {"items": [{"email": "PAT@example.com", "first_name": "Pat",
                                                                   "company_name": "Acme"}]}))
    client.get_email("a/b")
    assert t.calls[0]["url"].endswith("/api/v2/emails/a%2Fb")
    assert client.lead("pat@example.com") == {"first_name": "Pat", "company": "Acme"}
    assert t.calls[1]["json"] == {"contacts": ["pat@example.com"], "limit": 1}


def test_errors_name_the_call_but_never_the_key():
    client, _ = instantly((401, {"message": "Invalid API key"}))
    with pytest.raises(HttpError) as e:
        client.list_replies("2026-10-21T00:00:00Z")
    assert "HTTP 401" in str(e.value) and KEY not in str(e.value) and e.value.status == 401


def test_network_failures_become_http_errors():
    def down(*args):
        raise TimeoutError("timed out")

    client = InstantlyClient(api_key=KEY, transport=down)
    with pytest.raises(HttpError, match="Instantly send reply failed: TimeoutError"):
        client.send_reply(reply_to_id="x", sender="a@example.org", subject="s", body="b")


def test_reply_from_item_keeps_what_the_desk_needs():
    r = reply_from_item({"id": "e1", "thread_id": "t1", "from_address_email": "Pat@Example.com",
                         "eaccount": "sam@example.org",
                         "subject": "Re: Hi", "body": {"html": "Yes<br/>please &amp; thanks"}, "is_auto_reply": 1,
                         "timestamp_created": "2026-10-21T10:00:00.000Z", "body_fallback_reason": "too_large"})
    assert (r["lead_email"], r["body"], r["auto_reply"], r["truncated"]) == ("pat@example.com", "Yes\nplease & thanks",
                                                                             True, True)


def test_missing_keys_fail_clearly(monkeypatch):
    for var in ("INSTANTLY_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="INSTANTLY_API_KEY"):
        InstantlyClient()
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        TelegramClient()


def telegram(*responses):
    t = FakeTransport(*responses)
    return TelegramClient(token=TOKEN, chat_id="42", transport=t), t


def test_send_card_has_one_row_of_callback_buttons():
    client, t = telegram((200, {"ok": True, "result": {"message_id": 7}}))
    assert client.send_card("<b>Card</b>", [("Send", "rd:send:c1"), ("Skip", "rd:skip:c1")]) == "7"
    [call] = t.calls
    assert call["url"].endswith("/bot%s/sendMessage" % TOKEN)
    assert call["json"]["parse_mode"] == "HTML" and call["json"]["chat_id"] == "42"
    assert call["json"]["reply_markup"] == {"inline_keyboard": [[{"text": "Send", "callback_data": "rd:send:c1"},
                                                                 {"text": "Skip", "callback_data": "rd:skip:c1"}]]}


def test_update_card_without_buttons_removes_them_and_updates_uses_the_offset():
    client, t = telegram((200, {"ok": True, "result": {}}), (200, {"ok": True, "result": [{"update_id": 5}]}))
    client.update_card("7", "done")
    assert "reply_markup" not in t.calls[0]["json"] and t.calls[0]["json"]["message_id"] == 7
    assert client.updates(5, 25) == [{"update_id": 5}]
    assert t.calls[1]["json"] == {"offset": 5, "timeout": 25, "allowed_updates": ["callback_query", "message"]}


def test_telegram_errors_never_contain_the_token():
    client, _ = telegram((400, {"ok": False, "description": "Bad Request: chat not found"}))
    with pytest.raises(HttpError) as e:
        client.send_text("hi")
    assert "Telegram sendMessage returned HTTP 400" == str(e.value)
    assert TOKEN not in str(e.value)


def test_telegram_not_ok_is_an_error():
    client, _ = telegram((200, {"ok": False}))
    with pytest.raises(HttpError, match="not accepted"):
        client.answer("cb1", "ok")


def test_recent_chats_lists_who_messaged_the_bot():
    t = FakeTransport((200, {"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 42, "first_name": "Sam"}}},
        {"update_id": 2, "callback_query": {"message": {"chat": {"id": 42, "first_name": "Sam"}}}}]}))
    assert recent_chats(TOKEN, transport=t) == [("42", "Sam")]
    assert t.calls[0]["url"].endswith("/getUpdates") and t.calls[0]["json"] == {"timeout": 0}


def test_thread_follows_even_a_short_page_continuation():
    client, t = instantly((200, {"items": [{"id": "e1"}], "next_starting_after": "c1"}),
                          (200, {"items": [{"id": "e2"}]}))
    assert [m["id"] for m in client.thread("t1", "lead@example.com")] == ["e1", "e2"]
    assert t.query(1)["starting_after"] == "c1"


@pytest.mark.parametrize("page", [
    {"items": [None]}, {"items": [], "next_starting_after": {"bad": "cursor"}},
])
def test_thread_rejects_incomplete_page_shapes(page):
    client, _ = instantly((200, page))
    with pytest.raises(HttpError):
        client.thread("t1", "lead@example.com")


def test_thread_rejects_a_repeated_cursor():
    page = {"items": [{"id": "e1"}], "next_starting_after": "c1"}
    client, _ = instantly((200, page), (200, page))
    with pytest.raises(HttpError, match="cursor"):
        client.thread("t1", "lead@example.com")


def test_error_bodies_cannot_echo_credentials_or_urls():
    secret = "synthetic-credential"
    client, _ = instantly((401, {"message": "Bearer " + secret + " https://example.com/" + secret}))
    with pytest.raises(HttpError) as error:
        client.thread("t1", "lead@example.com")
    assert str(error.value) == "Instantly read thread returned HTTP 401"


def test_telegram_refuses_to_silently_truncate_a_card():
    client, transport = telegram()
    with pytest.raises(ValueError, match="long"):
        client.send_card("x" * 4097, [("Send", "rd:send:c1")])
    assert transport.calls == []


@pytest.mark.parametrize("delay,expected", [(9, 9), (-1, None), ("bad", None)])
def test_rate_limit_retains_only_a_valid_numeric_retry_delay(delay, expected):
    client, _ = telegram((429, {"description": "private text", "parameters": {"retry_after": delay}}))
    with pytest.raises(HttpError) as error:
        client.updates(0, 25)
    assert error.value.retry_after == expected
    assert "private text" not in str(error.value)


def test_list_replies_never_silently_returns_a_capped_result():
    pages = [(200, {"items": [{"id": "e%d" % i}], "next_starting_after": "c%d" % i}) for i in range(4)]
    client, _ = instantly(*pages, (200, {"items": [{"id": "last"}]}))
    assert client.list_replies("2026-10-21T00:00:00Z")[-1]["id"] == "last"


def test_telegram_preserves_entities_within_the_visible_text_limit():
    client, transport = telegram((200, {"ok": True, "result": {"message_id": 7}}))
    text = "<b>Draft</b>\n" + "&amp;" * 1000
    client.send_card(text, [("Send", "rd:send:c1")])
    assert transport.calls[0]["json"]["text"] == text


@pytest.mark.parametrize("result", [{}, {"message_id": None}, {"message_id": "invalid"}, {"message_id": False}])
def test_telegram_does_not_invent_a_message_id(result):
    client, transport = telegram((200, {"ok": True, "result": result}))
    with pytest.raises(HttpError, match="message ID"):
        client.send_card("Draft", [("Send", "rd:send:c1")])
    assert len(transport.calls) == 1
