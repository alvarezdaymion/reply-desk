"""`reply-desk demo`: a synthetic inbox run end to end, with no keys and no network.

Only the outside world is faked here: the inbox (a stand-in for Instantly), Telegram (cards print
to the console and the operator's taps are scripted), the clock (waits are simulated, so the 3, 10
and 20 second readbacks take no time) and the page host (the live check reads the page from the
local site folder). The desk, the send ledger, the classifier, the timing parser and the page
builder are the real code, writing real state to a SQLite file in the demo folder.
"""

from __future__ import annotations

import datetime as dt
import html
import shutil
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config as config_mod
from .cards import plain
from .desk import Desk
from .http import HttpError
from .pages import shown
from .status import status_lines
from .store import Store, zulu

START = dt.datetime(2026, 10, 21, 10, 0, tzinfo=dt.UTC)  # a Wednesday, fixed so every run prints the same
MARK = ".reply-desk-demo"

LEADS = {
    "pat@example.com": ("Pat", "Acme Plumbing"),
    "jordan@example.com": ("Jordan", "Sample Street Cafe"),
    "alex@example.org": ("Alex", "Example Landscaping"),
    "casey@example.com": ("Casey", "Corner Bike Shop"),
    "morgan@example.org": ("Morgan", "Test Kitchen Supply"),
}

# minutes before START, lead, receiving inbox, reply text, what the fake provider does on a send
REPLIES = [
    (170, "pat@example.com", "sam@example.org",
     "Sounds interesting. I'll look it over this weekend and let you know.\n\nPat\n\n"
     "On Tue, Oct 20, 2026 at 9:00 AM Sam <sam@example.org> wrote:\n> Hi Pat, quick question about your books.",
     "flaky"),
    (140, "jordan@example.com", "sam.k@example.org", "How much does it cost for a cafe our size?", "lost"),
    (115, "alex@example.org", "hello@example.org", "Do you work with businesses outside the US?", "ok"),
    (90, "casey@example.com", "sam@example.org",
     "We're swamped until after the holidays. Can you check back then?", "crash"),
    (60, "morgan@example.org", "sam.k@example.org", "No thanks, we already work with a bookkeeper.", "ok"),
    (45, "riley@example.org", "hello@example.org", "Please remove me from your list.", "ok"),
]

ANSWERS = [{"keywords": ["cost", "price", "pricing", "how much"],
            "text": "It's a flat monthly fee based on how many accounts we look after. "
                    "I can give you an exact number after a 15-minute call."}]


class DemoCrash(BaseException):
    """Stands in for the process dying mid-send (a BaseException, so nothing in the desk catches it)."""


class SimClock:
    def __init__(self, start: dt.datetime):
        self.t = start

    def now(self) -> dt.datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += dt.timedelta(seconds=seconds)


class DemoInbox:
    """A fake Instantly: threads in memory, scripted send outcomes, the same method names as the client."""

    def __init__(self, clock: SimClock):
        self.clock = clock
        self.emails: dict[str, dict[str, Any]] = {}
        self.outcome: dict[str, str] = {}
        self.sent: list[tuple[str, str]] = []
        self.next_id = 100
        for i, (minutes, lead, inbox, text, outcome) in enumerate(REPLIES, 1):
            company = LEADS.get(lead, ("", ""))[1]
            subject = "Bookkeeping for %s" % company if company else "A quick question"
            start = START - dt.timedelta(days=1)
            self._add({"id": "em-%03d" % i, "thread_id": "th-%d" % i, "ue_type": 1, "from_address_email": inbox,
                       "eaccount": inbox, "to_address_email_list": lead, "subject": subject,
                       "body": {"text": "Hi, I run Demo Bookkeeping. Could we help with your books?"},
                       "timestamp_created": zulu(start)})
            self._add({"id": "em-%03d" % (i + 50), "thread_id": "th-%d" % i, "ue_type": 2,
                       "from_address_email": lead, "lead": lead, "eaccount": inbox, "to_address_email_list": inbox,
                       "subject": "Re: " + subject, "body": {"text": text}, "campaign_id": "demo-campaign",
                       "timestamp_created": zulu(START - dt.timedelta(minutes=minutes))})
            self.outcome[lead] = outcome

    def _add(self, email: dict[str, Any]) -> None:
        self.emails[email["id"]] = email

    @staticmethod
    def _public(email: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in email.items() if not k.startswith("_")}

    def list_replies(self, since: str) -> list[dict[str, Any]]:
        return [self._public(e) for e in self.emails.values()
                if e["ue_type"] == 2 and e["timestamp_created"] >= since]

    def thread(self, thread_id: str, lead_email: str) -> list[dict[str, Any]]:
        return [self._public(e) for e in self.emails.values()
                if e["thread_id"] == thread_id and not e.get("_hidden")]

    def get_email(self, email_id: str) -> dict[str, Any]:
        email = self.emails.get(email_id)
        if email is None or email.get("_hidden"):
            if email is not None and email["_hidden"] < 99:
                email["_hidden"] -= 1  # visible from the next read on
            raise HttpError("Instantly get email returned HTTP 404: not found yet", 404)
        return self._public(email)

    def lead(self, email: str) -> dict[str, str]:
        first, company = LEADS.get(email, ("", ""))
        return {"first_name": first, "company": company} if first else {}

    def send_reply(self, *, reply_to_id: str, sender: str, subject: str, body: str) -> dict[str, Any]:
        target = self.emails[reply_to_id]
        to = target["from_address_email"]
        self.sent.append((to, body))
        self.next_id += 1
        mid = "em-%d" % self.next_id
        outcome = self.outcome.get(to, "ok")
        self._add({"id": mid, "thread_id": target["thread_id"], "ue_type": 3, "from_address_email": sender,
                   "eaccount": sender, "to_address_email_list": to, "subject": subject,
                   "body": {"text": body, "html": html.escape(body, quote=False).replace("\n", "<br/>")},
                   "timestamp_created": zulu(self.clock.now()),
                   "_hidden": {"flaky": 1, "lost": 99}.get(outcome, 0)})
        if outcome == "crash":  # the provider took the email, then the process died before saving the ID
            raise DemoCrash()
        return {"id": mid}

    def block(self, email: str) -> None:
        pass


class ConsoleNotifier:
    """Telegram stand-in: prints cards, keeps unacknowledged updates the way getUpdates does."""

    chat_id = "demo-chat"

    def __init__(self, out: Callable[[str], None]):
        self.out = out
        self.queue: list[dict[str, Any]] = []
        self.next_update, self.next_message, self.full_cards = 1, 500, 1

    def _id(self) -> str:
        self.next_message += 1
        return str(self.next_message)

    def send_card(self, text: str, buttons: Any) -> str:
        lines = [x for x in plain(text).split("\n") if x.strip()]
        if self.full_cards > 0:  # the first card in full, as Telegram shows it
            self.full_cards -= 1
        else:  # the rest shortened: title, why, and the start of the draft
            start = lines.index("Draft") if "Draft" in lines else len(lines)
            lines = [lines[0], *[x for x in lines if x.startswith("Why:")], *lines[start:start + 3], "..."]
        self.out("  +-- Telegram card " + "-" * 50)
        for line in lines:
            self.out("  | " + line)
        self.out("  | [ Send ]  [ Edit ]  [ Skip ]")
        self.out("  +" + "-" * 67)
        return self._id()

    def update_card(self, message_id: str, text: str, buttons: Any = ()) -> None:
        pass  # the desk's own lines already say what changed

    def send_text(self, text: str, force_reply: bool = False) -> str:
        self.out("  [Telegram] " + plain(text))
        return self._id()

    def answer(self, callback_id: str, text: str) -> None:
        self.out('  [Telegram shows] "%s"' % text)

    def updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        self.queue = [u for u in self.queue if u["update_id"] >= offset]
        return list(self.queue)

    def _push(self, update: dict[str, Any]) -> None:
        update["update_id"] = self.next_update
        self.next_update += 1
        self.queue.append(update)

    def tap(self, card_id: str, action: str) -> None:
        self._push({"callback_query": {"id": "cb-%d" % self.next_update, "data": "rd:%s:%s" % (action, card_id),
                                       "from": {"id": 1}, "message": {"chat": {"id": self.chat_id}}}})

    def reply(self, to_message: str, text: str) -> None:
        self._push({"message": {"text": text, "chat": {"id": self.chat_id}, "from": {"id": 1},
                                "reply_to_message": {"message_id": int(to_message)}}})


def demo_config(root: Path) -> config_mod.Config:
    return config_mod.from_dict({
        "business": {"name": "Demo Bookkeeping", "signature": "Sam\nDemo Bookkeeping",
                     "booking_url": "https://example.com/book"},
        "sending": {"mode": "live"},  # live against the fake inbox above; nothing leaves this process
        "pages": {"base_url": "https://pages.example.com",
                  "points": ["Monthly books closed by the 5th", "One flat fee, no hourly billing"]},
        "followups": {"timezone": "UTC"},
        "answers": ANSWERS,
    }, root)


def site_fetch(cfg: config_mod.Config) -> Callable[[str], tuple[int, str]]:
    """The live check's GET, answered from the local site folder instead of a web host."""
    base = cfg["pages"]["base_url"].rstrip("/")

    def fetch(url: str) -> tuple[int, str]:
        path = urllib.parse.urlsplit(url).path.strip("/")
        page = cfg.site_dir / path / "index.html"
        if not url.startswith(base) or not path or not page.is_file():
            return 404, ""
        return 200, page.read_text(encoding="utf-8")

    return fetch


def prepare(root: Path) -> Path:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        if not (root / MARK).exists():
            raise SystemExit("%s is not empty and is not a demo folder; pick another --dir" % root)
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARK).write_text("Created by reply-desk demo. Safe to delete.\n")
    return root


def run(root: Path, out: Callable[[str], None] = print) -> int:
    root = prepare(root)
    cfg = demo_config(root)
    clock = SimClock(START)
    inbox, tg = DemoInbox(clock), ConsoleNotifier(out)
    fetch = site_fetch(cfg)

    def new_desk() -> Desk:
        store = Store(cfg.db_path, clock=clock.now)
        return Desk(cfg, store, inbox, tg, fetch=fetch, clock=clock.now, sleep=clock.sleep,
                    say=lambda line: out("  " + line))

    def card_for(desk: Desk, lead: str) -> str:
        row = desk.store.db.execute("SELECT c.id FROM cards c JOIN replies r ON r.id = c.reply_id"
                                    " WHERE r.lead_email = ? ORDER BY c.created_at DESC LIMIT 1", (lead,)).fetchone()
        return str(row["id"])

    def act(desk: Desk, title: str, *taps: tuple[str, str]) -> None:
        out("\n> " + title)
        for lead, action in taps:
            tg.tap(card_for(desk, lead), action)
        desk.tick(poll=False)

    out("Reply Desk demo for \"Demo Bookkeeping\": synthetic replies, no keys, no network. Nothing is sent anywhere.")
    out("Clock: Wednesday 21 October 2026, 10:00 UTC (fixed). Telegram cards print here.\n")
    out("1. Six replies arrive across three sending inboxes. Each is sorted, and each one that needs an answer")
    out("   gets a card. The interested lead gets a one-page site first, and its card waits until the page is live.")
    desk = new_desk()
    desk.tick()

    out("\n2. The operator taps buttons in Telegram (scripted here).")
    act(desk, "Send on Pat's card, then Send again by accident", ("pat@example.com", "send"),
        ("pat@example.com", "send"))
    act(desk, "Send on Jordan's card (this time Instantly accepts the email but it never shows up)",
        ("jordan@example.com", "send"))
    act(desk, "Send on Alex's card (its draft still has a part to write)", ("alex@example.org", "send"))
    act(desk, "Edit on Alex's card", ("alex@example.org", "edit"))
    prompt = desk.store.card(card_for(desk, "alex@example.org"))["edit_prompt_id"]
    tg.reply(prompt, "Hi Alex,\n\nYes. We work fully online, so location doesn't matter.\n\nSam\nDemo Bookkeeping")
    act(desk, "The operator replies to the bot with the answer, then taps Send on Alex's card",
        ("alex@example.org", "send"))
    act(desk, "Skip on Morgan's card", ("morgan@example.org", "skip"))
    try:
        act(desk, "Send on Casey's card (the process dies right after Instantly accepts it)",
            ("casey@example.com", "send"))
    except DemoCrash:
        out("  !! the process stopped before the message ID was saved (simulated)")
    desk.store.close()

    out("\n3. The desk restarts. Telegram delivers Casey's unacknowledged tap again.")
    desk = new_desk()
    desk.tick(poll=False)

    clock.t = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.UTC)
    out("\n4. Friday 23 October, 09:00: Alex has not written back, so the planned follow-up becomes a card.")
    desk.tick(poll=False)

    repeats = len(inbox.sent) - len(set(inbox.sent))
    out("\nSummary")
    for line in status_lines(desk.store, cfg.tz()):
        out("  " + line if line else "")
    out("\n  Instantly send calls: %d, one per approved card. Repeats: %d." % (len(inbox.sent), repeats))
    out("  State: %s   Pages: %s" % (shown(cfg.db_path), shown(cfg.site_dir)))
    desk.store.close()
    return 0
