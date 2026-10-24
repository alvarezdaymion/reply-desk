"""Send each approved email at most once, and prove that it went out.

The problem: an email API can accept a send and then fail to confirm it (a timeout, a dropped
connection, a 5xx after the message was already queued). A blind retry emails the person a second
time. SafeSender never retries a send. Instead:

1. Before the provider is called, the exact email (reply target, sender, recipient, subject, body)
   is written to the ledger as an `intent` under a unique key, one key per card or follow-up step.
   A key that is already in the ledger is never sent again, and neither is the same text to the
   same address under any key.
2. The provider's message ID is saved the moment it comes back (`accepted`).
3. That exact message is read back by ID at 3, 10 and 20 seconds after acceptance (configurable).
   A readback that matches sender, recipient, subject and body marks the send `confirmed`.
4. Everything else is `locked`: an error or timeout on the send call, a response without a message
   ID, or no matching readback by the last check. A locked send is never retried. It waits for a
   person (`reply-desk resolve`).

After a restart, `recover` settles whatever was in flight without sending anything: an `accepted`
send is read back by its ID; an `intent` with no ID (the process stopped during the send call) is
looked for in the thread on the same 3, 10, 20 second schedule, confirmed if the exact message is
there and locked if it is not.

`SafeSender.send` is the only code in this project that calls the provider's send method.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .store import Store

# A message found in the thread must be newer than its intent, give or take this much clock skew.
THREAD_SKEW = dt.timedelta(minutes=10)


class Provider(Protocol):
    def send_reply(self, *, reply_to_id: str, sender: str, subject: str, body: str) -> dict[str, Any]: ...
    def get_email(self, email_id: str) -> dict[str, Any]: ...
    def thread(self, thread_id: str, lead_email: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class SendResult:
    state: str           # confirmed, locked, refused, or the state an existing ledger row is in
    send_id: int | None
    called: bool         # True only if this call reached the provider's send method
    detail: str
    readbacks: int = 0


def normalize(text: str) -> str:
    """Visible text only: HTML head and tags dropped, entities decoded, whitespace collapsed."""
    text = re.sub(r"<head\b.*?</head\s*>", " ", text or "", flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>|</p\s*>|</div\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def addresses(value: Any) -> set[str]:
    parts = value if isinstance(value, list) else str(value or "").split(",")
    out = set()
    for p in parts:
        if isinstance(p, dict):
            p = p.get("address") or p.get("email") or ""
        m = re.search(r"[^\s<>,;\"']+@[^\s<>,;\"']+", str(p))
        if m:
            out.add(m.group(0).lower())
    return out


def mismatch(msg: dict[str, Any], email: dict[str, Any], provider_id: str) -> str:
    """Why `msg` is not the email in the ledger, or "" when every field matches."""
    if str(msg.get("id") or "") != provider_id:
        return "a different message came back"
    sender = str(msg.get("from_address_email") or msg.get("eaccount") or "").strip().lower()
    if sender != str(email["sender"]).strip().lower():
        return "sender differs"
    if addresses(msg.get("to_address_email_list")) != {str(email["recipient"]).lower()}:
        return "recipient differs"
    if str(msg.get("subject") or "").strip() != str(email["subject"]).strip():
        return "subject differs"
    body = msg.get("body")
    variants = [(k, body[k]) for k in ("text", "html") if body.get(k)] if isinstance(body, dict) else []
    if not variants:
        return "the readback has no body"
    expected = " ".join(str(email["body"]).split())
    if any((normalize(str(v)) if k == "html" else " ".join(str(v).split())) != expected for k, v in variants):
        return "body differs"
    return ""


def parse_ts(value: Any) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def find_in_thread(thread: Sequence[dict[str, Any]], email: dict[str, Any], not_before: dt.datetime) -> str | None:
    """The ID of an outgoing message in `thread` identical to `email` and newer than `not_before`."""
    for msg in thread:
        mid = str(msg.get("id") or "")
        if msg.get("ue_type") == 2 or not mid or mismatch(msg, email, mid):
            continue
        created = parse_ts(msg.get("timestamp_created"))
        if created is not None and created >= not_before:
            return mid
    return None


def short(e: Exception) -> str:
    return (str(e) or type(e).__name__)[:160]


def secs(at: float) -> str:
    return "%g s" % at


class SafeSender:
    def __init__(self, store: Store, provider: Provider, readback_seconds: Sequence[float] = (3, 10, 20),
                 sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] | None = None):
        self.store = store
        self.provider = provider
        self.schedule = [float(s) for s in readback_seconds]
        self.sleep = sleep
        self.say = say or (lambda _: None)

    def send(self, key: str, email: dict[str, str], card_id: str | None = None) -> SendResult:
        existing = self.store.send_by_key(key)
        if existing:
            return SendResult(existing["state"], existing["id"], False, "already in the ledger as send %d (%s);"
                              " not sent again" % (existing["id"], existing["state"]))
        same = self.store.same_email(email["recipient"], email["body"])
        if same:
            return SendResult("refused", same["id"], False,
                              "the same text to %s is already in the ledger as send %d; not sent again"
                              % (email["recipient"], same["id"]))
        send_id = self.store.create_intent(key, card_id, email)
        if send_id is None:  # another writer got there between the checks and the insert
            row = self.store.send_by_key(key) or self.store.same_email(email["recipient"], email["body"])
            return SendResult(row["state"] if row else "refused", row["id"] if row else None, False,
                              "already in the ledger; not sent again")
        self.say("send %d: intent saved as %s before calling the provider" % (send_id, key))
        try:
            resp = self.provider.send_reply(reply_to_id=email["reply_to_id"], sender=email["sender"],
                                            subject=email["subject"], body=email["body"])
        except Exception as e:
            return self._lock(send_id, ("intent",), True,
                              "the send call failed and its outcome is unknown (%s); not retried" % short(e))
        pid = str(resp.get("id") or "") if isinstance(resp, dict) else ""
        if not pid:
            return self._lock(send_id, ("intent",), True,
                              "the provider answered without a message ID, so the outcome is unknown; not retried")
        if not self.store.move_send(send_id, "accepted", ("intent",), "accepted as %s" % pid, provider_id=pid):
            row = self.store.send(send_id) or {}
            return SendResult(str(row.get("state")), send_id, True,
                              "accepted as %s, but the ledger row had already moved; not retried" % pid)
        self.say("send %d: accepted as %s and saved; reading it back at %s"
                 % (send_id, pid, ", ".join(secs(a) for a in self.schedule)))
        return self.confirm(send_id, called=True)

    def confirm(self, send_id: int, called: bool = False) -> SendResult:
        """Read an accepted send back by its ID on the schedule; confirm it or lock it. Never sends."""
        row = self.store.send(send_id)
        if row is None or row["state"] != "accepted":
            return SendResult(row["state"] if row else "missing", send_id, called, "only an accepted send is read back")
        pid, waited, last, n = row["provider_id"], 0.0, "", 0
        for n, at in enumerate(self.schedule, 1):
            self.sleep(at - waited)
            waited = at
            try:
                problem = mismatch(self.provider.get_email(pid), row, pid)
            except Exception as e:
                problem = short(e)
            if not problem:
                note = "readback %d at %s matched" % (n, secs(at))
                self.say("send %d: %s" % (send_id, note))
                if self.store.move_send(send_id, "confirmed", ("accepted",), note, readbacks=n):
                    return SendResult("confirmed", send_id, called, note, n)
                now_row = self.store.send(send_id) or {}
                return SendResult(str(now_row.get("state")), send_id, called,
                                  note + ", but the ledger row had already moved", n)
            last = "readback %d at %s: %s" % (n, secs(at), problem)
            self.store.note_readback(send_id, n, last)
            self.say("send %d: %s" % (send_id, last))
        return self._lock(send_id, ("accepted",), called, "accepted as %s, but no readback matched by %s (last: %s);"
                          " not retried" % (pid, secs(self.schedule[-1]), last.split(": ", 1)[-1]), n)

    def recover(self) -> list[SendResult]:
        """Settle sends a stopped process left in flight. Reads only; never sends."""
        results = [self.confirm(row["id"]) for row in self.store.sends("accepted")]
        for row in self.store.sends("intent"):
            found, why = self._search_thread(row, self.schedule)
            if found and self.store.move_send(row["id"], "accepted", ("intent",),
                                              "found in the thread after a restart as %s" % found, provider_id=found):
                self.say("send %d: the process had stopped mid-send; found it in the thread as %s" % (row["id"], found))
                results.append(self.confirm(row["id"]))
                continue
            results.append(self._lock(row["id"], ("intent",), False,
                                      "the process stopped during the send call and %s, so the outcome is unknown;"
                                      " not retried" % why))
        return results

    def recheck(self, send_id: int) -> SendResult:
        """One more look at a locked send, by its message ID or in the thread. Reads only; never sends."""
        row = self.store.send(send_id)
        if row is None or row["state"] != "locked":
            return SendResult(row["state"] if row else "missing", send_id, False, "only a locked send can be rechecked")
        if row["provider_id"]:
            try:
                problem = mismatch(self.provider.get_email(row["provider_id"]), row, row["provider_id"])
            except Exception as e:
                problem = short(e)
            found = row["provider_id"] if not problem else None
        else:
            found, problem = self._search_thread(row, [0.0])
        if found and self.store.move_send(send_id, "confirmed", ("locked",), "recheck matched %s" % found,
                                          provider_id=found):
            return SendResult("confirmed", send_id, False, "recheck matched %s" % found)
        return SendResult("locked", send_id, False, "still not confirmed: %s" % problem)

    def _search_thread(self, row: dict[str, Any], schedule: Sequence[float]) -> tuple[str | None, str]:
        created = parse_ts(row["created_at"]) or dt.datetime.now(dt.UTC)
        waited, why = 0.0, "the thread was not checked"
        for n, at in enumerate(schedule, 1):
            self.sleep(at - waited)
            waited = at
            try:
                thread = self.provider.thread(row["thread_id"], row["recipient"])
            except Exception as e:
                why = "the thread could not be read (%s)" % short(e)
                continue
            found = find_in_thread(thread, row, created - THREAD_SKEW)
            if found:
                return found, ""
            why = "no matching message was in the thread after %d check%s" % (n, "" if n == 1 else "s")
        return None, why

    def _lock(self, send_id: int, from_: tuple[str, ...], called: bool, reason: str, readbacks: int = 0) -> SendResult:
        if self.store.move_send(send_id, "locked", from_, reason):
            self.say("send %d: locked: %s" % (send_id, reason))
            return SendResult("locked", send_id, called, reason, readbacks)
        row = self.store.send(send_id) or {}
        return SendResult(str(row.get("state")), send_id, called, reason, readbacks)
