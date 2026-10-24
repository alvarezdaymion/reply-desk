"""Shared fakes. Nothing here touches the network: the inbox, Telegram and the clock are in memory."""

import datetime as dt

import pytest

from reply_desk.http import HttpError
from reply_desk.store import Store, zulu

NOW = dt.datetime(2026, 10, 21, 14, 0, tzinfo=dt.UTC)  # a Wednesday
OURS = "sam@example.org"


class Crash(BaseException):
    """The process dying: not an Exception, so no handler in the desk catches it."""


class FakeInbox:
    """Stands in for InstantlyClient. Each send_reply call takes the next scripted outcome:
    ok | late:N (hidden from the next N reads) | never | timeout | reject | no_id | wrong_body | crash"""

    def __init__(self):
        self.emails, self.sent, self.reads, self.script = {}, [], [], []
        self.thread_error = None
        self.n = 0

    def add_inbound(self, lead, text, thread="th-1", minutes_ago=60, eid=None, auto=False):
        self.n += 1
        email = {"id": eid or "in-%d" % self.n, "thread_id": thread, "ue_type": 2, "from_address_email": lead,
                 "lead": lead, "eaccount": OURS, "to_address_email_list": OURS, "subject": "Re: Hello",
                 "body": {"text": text}, "campaign_id": "c-1", "is_auto_reply": 1 if auto else 0,
                 "timestamp_created": zulu(NOW - dt.timedelta(minutes=minutes_ago))}
        self.emails[email["id"]] = email
        return email

    def list_replies(self, since):
        return [dict(e) for e in self.emails.values() if e["ue_type"] == 2 and e["timestamp_created"] >= since]

    def thread(self, thread_id, lead_email):
        if self.thread_error:
            raise self.thread_error
        return [dict(e) for e in self.emails.values() if e["thread_id"] == thread_id and not e.get("hidden")]

    def get_email(self, email_id):
        self.reads.append(email_id)
        email = self.emails.get(email_id)
        if email is None or email.get("hidden"):
            if email is not None and email["hidden"] != "forever":
                email["hidden"] -= 1
            raise HttpError("Instantly get email returned HTTP 404", 404)
        return {k: v for k, v in email.items() if k != "hidden"}

    def lead(self, email):
        return {"first_name": "Pat", "company": "Acme Plumbing"} if email.startswith("pat@") else {}

    def block(self, email):
        self.blocked = email

    def send_reply(self, *, reply_to_id, sender, subject, body):
        self.sent.append({"reply_to_id": reply_to_id, "sender": sender, "subject": subject, "body": body})
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "reject":
            raise HttpError("Instantly send reply returned HTTP 400", 400)
        target = self.emails[reply_to_id]
        self.n += 1
        mid = "out-%d" % self.n
        hidden = {"never": "forever"}.get(outcome, int(outcome.split(":")[1]) if outcome.startswith("late:") else 0)
        stored = body + (" (changed)" if outcome == "wrong_body" else "")
        self.emails[mid] = {"id": mid, "thread_id": target["thread_id"], "ue_type": 3, "from_address_email": sender,
                            "eaccount": sender, "to_address_email_list": target["from_address_email"],
                            "subject": subject, "body": {"text": stored}, "timestamp_created": zulu(NOW),
                            "hidden": hidden}
        if outcome == "timeout":
            raise TimeoutError("timed out")
        if outcome == "crash":
            raise Crash()
        return {} if outcome == "no_id" else {"id": mid}


class FakeTelegram:
    chat_id = "chat-1"

    def __init__(self):
        self.cards, self.texts, self.answers, self.queue = {}, [], [], []
        self.next_update, self.next_message = 1, 100

    def _mid(self):
        self.next_message += 1
        return str(self.next_message)

    def send_card(self, text, buttons):
        mid = self._mid()
        self.cards[mid] = (text, list(buttons))
        return mid

    def update_card(self, message_id, text, buttons=()):
        self.cards[message_id] = (text, list(buttons))

    def send_text(self, text, force_reply=False):
        self.texts.append(text)
        return self._mid()

    def answer(self, callback_id, text):
        self.answers.append(text)

    def updates(self, offset, timeout):
        self.queue = [u for u in self.queue if u["update_id"] >= offset]
        return list(self.queue)

    def push(self, update):
        update["update_id"] = self.next_update
        self.next_update += 1
        self.queue.append(update)

    def tap(self, card_id, action="send", chat=None, user=1):
        prefix = "rd:%s:%s" % (action, card_id)
        data = next((data for _, buttons in self.cards.values() for _, data in buttons
                     if data.startswith(prefix + ":")), prefix)
        self.push({"callback_query": {"id": "cb%d" % self.next_update, "data": data,
                                      "from": {"id": user}, "message": {"chat": {"id": chat or self.chat_id}}}})

    def reply_to(self, message_id, text):
        self.push({"message": {"text": text, "chat": {"id": self.chat_id}, "from": {"id": 1},
                               "reply_to_message": {"message_id": int(message_id)}}})


class Clock:
    def __init__(self, start=NOW):
        self.t, self.sleeps = start, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += dt.timedelta(seconds=seconds)


def make_cfg(root, **over):
    from reply_desk import config

    data = {"business": {"name": "Demo Bookkeeping", "signature": "Sam", "booking_url": "https://example.com/book"},
            "sending": {"mode": "live"}, "followups": {"timezone": "UTC"},
            "answers": [{"keywords": ["cost"], "text": "A flat monthly fee."}]}
    for section, values in over.items():
        data[section] = {**data.get(section, {}), **values} if isinstance(values, dict) else values
    return config.from_dict(data, root)


class Rig:
    """A desk wired to fakes, plus helpers to drive it."""

    def __init__(self, root, **over):
        from reply_desk.demo import site_fetch

        self.cfg = make_cfg(root, **over)
        self.inbox, self.tg, self.clock = FakeInbox(), FakeTelegram(), Clock()
        self.fetch = site_fetch(self.cfg)
        self.restart()

    def restart(self):
        from reply_desk.desk import Desk

        self.store = Store(self.cfg.db_path, clock=self.clock.now)
        self.desk = Desk(self.cfg, self.store, self.inbox, self.tg, fetch=self.fetch, clock=self.clock.now,
                         sleep=self.clock.sleep)
        return self.desk

    def card(self, lead):
        row = self.store.db.execute("SELECT c.* FROM cards c JOIN replies r ON r.id = c.reply_id WHERE r.lead_email = ?"
                                    " ORDER BY c.created_at DESC, c.rowid DESC LIMIT 1", (lead,)).fetchone()
        return dict(row) if row else None


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def make_rig(tmp_path):
    return lambda **over: Rig(tmp_path, **over)
