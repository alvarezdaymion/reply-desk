"""One tick of the reply desk, and the handlers for the buttons on a card.

tick() runs five steps. A step that fails is reported and the others still run.

1. recover     settle sends and cards that a stopped process left in flight (reads only)
2. poll        store new replies from the inbox, once each (keyed by the provider's message ID)
3. triage      classify new replies. Unsubscribes are suppressed and get no card. An interested
               reply gets a one-page site, and its card is posted only once the page is live.
4. taps        read Telegram updates: Send, Edit, Skip, and the text of an edit
5. follow-ups  turn due follow-ups into cards, unless the person has written since

Send runs these checks before anything is written to the ledger: the draft has nothing left to
fill in, the address has not unsubscribed, a linked page is still live, and the person has not
written again since the card was made. Then SafeSender sends once and reads the message back.
In dry-run mode the checks run and nothing is sent.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from . import drafts, pages, timing
from .cards import buttons, card_text, revision
from .classify import authored_text, classify
from .connectors.instantly import reply_from_item
from .http import raise_if_rate_limited, retry_delay
from .sender import SafeSender, parse_ts, short
from .store import Store, iso, zulu

POLL_OVERLAP = dt.timedelta(minutes=5)


class NoSend:
    """Wraps the inbox in dry-run mode so a send is impossible, not just skipped."""

    def __init__(self, inner: Any):
        self.inner = inner

    def send_reply(self, **_: Any) -> dict[str, Any]:
        raise PermissionError("dry-run mode: sending is disabled")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else "Re: " + subject


def newer_inbound(thread: list[dict[str, Any]], reply: dict[str, Any]) -> dict[str, Any] | None:
    """A message the person sent after `reply` (auto-replies don't count)."""
    received = parse_ts(reply["received_at"])
    if not received or not any(m.get("id") == reply["provider_id"] for m in thread):
        raise ValueError("the thread is missing the original reply")
    for m in thread:
        if not m.get("id") or type(m.get("ue_type")) is not int or m["ue_type"] not in (1, 2, 3):
            raise ValueError("the thread has incomplete message metadata")
        if m.get("ue_type") != 2:
            continue
        created = parse_ts(m.get("timestamp_created"))
        if not created:
            raise ValueError("the thread has an invalid received timestamp")
        if str(m["id"]) == reply["provider_id"] or m.get("is_auto_reply") in (1, True):
            continue
        if created >= received:
            return m
    return None


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _never_raises(say: Callable[[str], None] | None) -> Callable[[str], None]:
    """Progress lines are for people. A closed stdout must not interrupt a send or a readback."""

    def safe(line: str) -> None:
        try:
            if say:
                say(line)
        except Exception:
            pass

    return safe


class Desk:
    def __init__(self, cfg: Any, store: Store, inbox: Any, notifier: Any, *, publisher: Any = None,
                 fetch: pages.Fetch = pages.urllib_fetch, drafter: Any = None,
                 clock: Callable[[], dt.datetime] = _utcnow, sleep: Callable[[float], None] = time.sleep,
                 say: Callable[[str], None] | None = None):
        self.cfg, self.store, self.inbox, self.notifier = cfg, store, inbox, notifier
        self.publisher = publisher or pages.publisher_for(cfg)
        self.fetch, self.drafter, self.clock = fetch, drafter, clock
        self.say = _never_raises(say)
        self.sender = SafeSender(store, inbox if cfg.live else NoSend(inbox), cfg["sending"]["readback_seconds"], sleep,
                                 self.say)
        self.tz = cfg.tz()
        self.chat_id = str(getattr(notifier, "chat_id", ""))
        self.allowed = {str(u) for u in cfg["telegram"]["allowed_user_ids"]}
        self.stats: Counter[str] = Counter()

    # ---- the tick ------------------------------------------------------------------------------

    def tick(self, updates_timeout: int = 0, poll: bool = True) -> dict[str, Any]:
        self.stats = Counter()
        errors = []
        retry_after = 0.0
        until = parse_ts(self.store.get("api.retry_until"))
        if until and until > self.clock():
            return {"stats": {}, "errors": ["API work deferred during cooldown"],
                    "retry_after": (until - self.clock()).total_seconds()}
        steps: list[tuple[str, Callable[[], None]]] = [("recover", self.recover), ("triage", self.triage),
                                                       ("taps", lambda: self.taps(updates_timeout)),
                                                       ("follow-ups", self.followups)]
        if poll:
            steps.insert(1, ("poll", self.poll))
        for name, step in steps:
            try:
                step()
            except Exception as e:
                errors.append("%s: %s" % (name, short(e)))
                self.store.log("tick.error", name, short(e))
                retry_after = retry_delay(e)
                if retry_after:
                    until = self.clock() + dt.timedelta(seconds=retry_after)
                    self.store.put("api.retry_until", until.isoformat())
                    break
        return {"stats": dict(self.stats), "errors": errors, "retry_after": retry_after}

    def recover(self) -> None:
        if not self.cfg["followups"]["gaps_days"]:
            self.store.disable_followups()
        settled = len(self.sender.recover())
        if settled:
            self.stats["recovered"] += settled
        for card in self.store.cards("sending", "locked"):
            row = self.store.send_by_key(self.key(card))
            if row is None:
                if self.store.move_card(card["id"], "pending", ("sending",), "restarted before anything was sent"):
                    self._refresh(card["id"], "The desk restarted before sending. Nothing was sent; tap Send again.")
            elif card["state"] == "sending" or row["state"] != "locked":
                self._settle(card, row["state"], row["note"] or "", row["id"])
        for card in self.store.cards("sent"):
            self._arm(card)
        for card in self.store.cards("skipped", "stale", "closed", "dry_run"):
            self._finish_followup(card)
        for card in self.store.cards("pending"):
            if card["post_state"] == "posting":
                self.store.finish_card_post(card["id"], None, card["draft"], "card post outcome unclear; check by hand")
            elif card["post_state"] == "unposted":
                self._post(card)

    def poll(self) -> None:
        cursor = parse_ts(self.store.get("inbox.cursor") or "")
        lookback = dt.timedelta(hours=float(self.cfg["inbox"]["lookback_hours"]))
        since = (cursor - POLL_OVERLAP) if cursor else self.clock() - lookback
        campaigns = {str(c) for c in self.cfg["inbox"]["campaign_ids"]}
        if not hasattr(self.inbox, "list_reply_page"):
            newest = self._store_replies(self.inbox.list_replies(zulu(since)), campaigns, cursor)
            if newest:
                self.store.put("inbox.cursor", iso(newest))
            return
        progress = json.loads(self.store.get("inbox.pagination") or "null") or {
            "since": zulu(since), "after": "", "newest": iso(cursor) if cursor else ""}
        seen = {progress["after"]}
        for _ in range(3):
            items, nxt = self.inbox.list_reply_page(progress["since"], progress["after"])
            if nxt and nxt in seen:
                raise ValueError("Instantly list replies returned a repeated cursor")
            newest = self._store_replies(items, campaigns, parse_ts(progress["newest"]))
            progress.update(after=nxt, newest=iso(newest) if newest else "")
            if not nxt:
                if newest:
                    self.store.put("inbox.cursor", iso(newest))
                self.store.put("inbox.pagination", "null")
                return
            seen.add(nxt)
            self.store.put("inbox.pagination", json.dumps(progress))

    def _store_replies(self, items: list[dict[str, Any]], campaigns: set[str],
                       newest: dt.datetime | None) -> dt.datetime | None:
        for item in items:
            if item.get("ue_type") not in (None, 2):  # only what people sent us
                continue
            r = reply_from_item(item)
            created = parse_ts(r["received_at"])
            if not (r["provider_id"] and r["lead_email"] and created):
                continue
            newest = max(newest or created, created)
            if (campaigns and r["campaign_id"] not in campaigns) or self.store.has_reply(r["provider_id"]):
                continue
            try:
                r.update(self.inbox.lead(r["lead_email"]))
            except Exception as e:
                raise_if_rate_limited(e)
                self.store.log("lead.lookup_failed", r["lead_email"], short(e))
            if self.store.add_reply({**r, "received_at": iso(created)}):
                self.stats["replies"] += 1
        return newest

    # ---- triage --------------------------------------------------------------------------------

    def triage(self) -> None:
        for reply in self.store.replies("new", "waiting_page"):
            try:
                self._triage(reply)
            except Exception as e:
                raise_if_rate_limited(e)
                attempts = reply["attempts"] + 1
                failed = attempts >= 3
                fields = {"status": "failed"} if failed else {}
                moved = self.store.update_reply(reply["id"], attempts=attempts, note=short(e), **fields)
                self.store.log("triage.error", reply["id"], short(e))
                if failed and moved:
                    self._notify("No card for the reply from %s: %s. Answer it by hand."
                                 % (reply["lead_email"], short(e)))

    def _triage(self, reply: dict[str, Any]) -> None:
        lead = reply["lead_email"]
        if not reply["body_complete"]:
            try:
                full = reply_from_item(self.inbox.get_email(reply["provider_id"]))
                if full["provider_id"] != reply["provider_id"] or full["truncated"] or not full["body"].strip():
                    raise ValueError("the full reply body is still unavailable")
            except Exception as e:
                raise_if_rate_limited(e)
                self.store.log("reply.body_fetch_failed", reply["provider_id"], short(e))
                return
            self.store.update_reply(reply["id"], body=full["body"], body_complete=1)
            reply = self.store.reply(reply["id"]) or reply
        if reply["status"] == "new":
            if self.store.is_suppressed(lead) or reply["auto_reply"]:
                why = "this address unsubscribed" if self.store.is_suppressed(lead) else "auto-reply"
                self.store.update_reply(reply["id"], status="ignored", note=why)
                self.stats["ignored"] += 1
                return
            verdict = classify(reply["body"])
            self.store.update_reply(reply["id"], label=verdict.label, draft=None, status="waiting_page",
                                    reason="%s (%s)" % (verdict.reason, verdict.source))
            if self.drafter is not None:
                verdict = self.drafter.classify(reply, verdict)
            self.store.update_reply(reply["id"], label=verdict.label, draft=verdict.draft,
                                    reason="%s (%s)" % (verdict.reason, verdict.source))
            if self.store.cancel_followups(lead, "they replied"):
                self.stats["followups_cancelled"] += 1
            reply = self.store.reply(reply["id"]) or reply
        if reply["label"] == "unsubscribe":
            self._unsubscribe(reply)
            return
        if reply["label"] == "interested" and self.cfg["pages"]["enabled"] and not self._page_ready(reply):
            return
        card = self.store.create_card(reply["id"], "reply", self._reply_draft(self.store.reply(reply["id"]) or reply))
        self._post(card)

    def _page_ready(self, reply: dict[str, Any]) -> bool:
        """Build and publish the page once, then check it is live. False holds the card back."""
        if not reply["page_url"]:
            page = pages.prepare(self.cfg, reply)
            self.store.update_reply(reply["id"], page_url=page.url, page_marker=page.marker)
            reply = {**reply, "page_url": page.url, "page_marker": page.marker}
            note = self.publisher.publish(self.cfg.site_dir, page.slug)
            self.say("page for %s: %s (%s)" % (reply["lead_email"], page.url, note))
        ok, why = pages.check_live(reply["page_url"], reply["page_marker"], self.fetch)
        if ok:
            self.say("page for %s is live: %s" % (reply["lead_email"], why))
            return True
        attempts = reply["attempts"] + 1
        if attempts >= int(self.cfg["pages"]["check_attempts"]):
            self.store.update_reply(reply["id"], status="failed", attempts=attempts, note="page not live: " + why)
            self._notify("No card for %s: their page never went live (%s). Check the publisher, then answer by hand."
                         % (reply["lead_email"], why))
        else:
            self.store.update_reply(reply["id"], status="waiting_page", attempts=attempts, note="page not live: " + why)
            self.say("page for %s not live yet (%s); checking again next tick" % (reply["lead_email"], why))
        return False

    def _reply_draft(self, reply: dict[str, Any]) -> str:
        values = drafts.values_for(self.cfg, reply)
        if reply["draft"]:
            try:
                return drafts.fill(reply["draft"], values)
            except (ValueError, KeyError, IndexError):
                self.store.update_reply(reply["id"], draft=None)
        text = authored_text(reply["body"])
        return drafts.reply_draft(self.cfg, reply, reply["label"], self._hint(reply), text) or ""

    def _hint(self, reply: dict[str, Any]) -> timing.Hint | None:
        """When the person said to come back, read relative to when they wrote it."""
        fu = self.cfg["followups"]
        written = parse_ts(reply["received_at"]) or self.clock()
        return timing.parse_when(authored_text(reply["body"]), written.astimezone(self.tz),
                                 send_hour=int(fu["send_hour"]), max_days_out=int(fu["max_days_out"]))

    def _unsubscribe(self, reply: dict[str, Any]) -> None:
        lead = reply["lead_email"]
        self.store.suppress(lead, "asked to unsubscribe")
        self.store.update_reply(reply["id"], status="suppressed")
        self.stats["unsubscribed"] += 1
        extra = ""
        if self.cfg["inbox"]["blocklist_unsubscribes"]:
            if not self.cfg.live:
                extra = " Dry run: not added to the Instantly block list."
            else:
                try:
                    self.inbox.block(lead)
                    extra = " Added to the Instantly block list."
                except Exception as e:
                    raise_if_rate_limited(e)
                    extra = " The block list update failed (%s); add it by hand." % short(e)
        self._notify("Unsubscribed: %s asked not to be emailed. No card, and the desk will never send to this "
                     "address.%s" % (lead, extra))

    # ---- taps ----------------------------------------------------------------------------------

    def taps(self, timeout: int = 0) -> None:
        offset = int(self.store.get("telegram.offset") or 0)
        for update in self.notifier.updates(offset, timeout):
            try:
                if "callback_query" in update:
                    self.on_tap(update["callback_query"])
                elif "message" in update:
                    self.on_message(update["message"])
            except Exception as e:  # a bad update must not block the queue
                self.store.log("update.error", update.get("update_id", ""), short(e))
                self.store.put("telegram.offset", str(int(update["update_id"]) + 1))
                raise_if_rate_limited(e)
                continue
            self.store.put("telegram.offset", str(int(update["update_id"]) + 1))

    def _trusted(self, chat: Any, user: Any) -> bool:
        return str(chat) == self.chat_id and (not self.allowed or str(user) in self.allowed)

    def on_tap(self, cq: dict[str, Any]) -> None:
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        if not self._trusted(chat, (cq.get("from") or {}).get("id")):
            self.store.log("tap.refused", (cq.get("from") or {}).get("id", ""), "chat %s" % chat)
            return self._answer(cq, "Not allowed.")
        parts = str(cq.get("data") or "").split(":")
        card = self.store.card(parts[2]) if len(parts) in (3, 4) and parts[0] == "rd" else None
        if card is None or parts[1] not in ("send", "edit", "skip"):
            return self._answer(cq, "Unknown button.")
        if parts[1] == "send" and card["state"] == "pending":
            if len(parts) != 4 or parts[3] != revision(card["draft"]):
                self._refresh(card["id"], "Review the current draft, then tap its Send button.")
                return self._answer(cq, "The draft changed. Review the current card before sending.")
        self.stats["taps"] += 1
        {"send": self.on_send, "edit": self.on_edit, "skip": self.on_skip}[parts[1]](card, cq)

    def on_send(self, card: dict[str, Any], cq: dict[str, Any]) -> None:
        if not self.store.claim_card(card["id"], expected_draft=card["draft"]):
            current = self.store.card(card["id"]) or card
            self.stats["repeat_taps"] += 1
            self.store.log("tap.repeat", card["id"], "card was %s" % current["state"])
            self.say("%s: Send tapped again while the card was %s; nothing was sent again"
                     % (self._who(card), current["state"]))
            return self._answer(cq, "Already handled. Nothing was sent again.")
        self._answer(cq, "Checking, then sending" if self.cfg.live else "Dry run: checking")
        self._deliver(self.store.card(card["id"]) or card)

    def _deliver(self, card: dict[str, Any]) -> None:
        reply = self.store.reply(card["reply_id"])
        assert reply is not None
        if card["kind"] == "followup" and not self.cfg["followups"]["gaps_days"]:
            return self._settle(card, "closed", "follow-ups are disabled; nothing was sent")
        problem = drafts.check(card["draft"])
        if problem:
            return self._back_to_pending(card, problem)
        if not reply["body_complete"]:
            return self._back_to_pending(card, "the full reply body is unavailable; nothing was sent")
        if card["post_state"] != "posted" or card["reviewed_draft"] != card["draft"]:
            return self._back_to_pending(card, "this draft has not been displayed in full; nothing was sent")
        if self.store.is_suppressed(reply["lead_email"]):
            return self._settle(card, "closed", "this address unsubscribed; nothing was sent")
        if reply["page_url"] and reply["page_url"] in card["draft"]:
            ok, why = pages.check_live(reply["page_url"], reply["page_marker"], self.fetch)
            if not ok:
                return self._back_to_pending(card, "the page is not live (%s); nothing was sent" % why)
        try:
            thread = self.inbox.thread(reply["thread_id"], reply["lead_email"])
            newer = newer_inbound(thread, reply)
        except Exception as e:
            raise_if_rate_limited(e)
            return self._back_to_pending(card, "the thread could not be read (%s); nothing was sent" % short(e))
        if newer:
            return self._settle(card, "stale", "they wrote again, so this draft is out of date; nothing was sent")
        if not self.cfg.live:
            return self._settle(card, "dry_run", "dry run: every check passed and nothing was sent")
        email = {"reply_to_id": reply["provider_id"], "thread_id": reply["thread_id"], "sender": reply["inbox"],
                 "recipient": reply["lead_email"], "subject": reply_subject(reply["subject"]), "body": card["draft"]}
        result = self.sender.send(self.key(card), email, card["id"])
        if result.state == "refused":  # nothing was written or sent, so the card can be edited and sent
            return self._back_to_pending(card, result.detail + "; edit the text to send something new")
        self._settle(card, result.state, result.detail, result.send_id)

    def on_skip(self, card: dict[str, Any], cq: dict[str, Any]) -> None:
        if not self.store.move_card(card["id"], "skipped", ("pending",), "skipped"):
            return self._answer(cq, "Already handled.")
        if card["kind"] == "followup":
            self.store.move_followup(card["followup_id"], "done", ("carded",), "skipped; no more follow-ups")
        self._answer(cq, "Skipped. Nothing was sent.")
        self._refresh(card["id"], "Skipped. Nothing was sent.")
        self.say("%s: skipped" % self._who(card))

    def on_edit(self, card: dict[str, Any], cq: dict[str, Any]) -> None:
        if card["state"] != "pending":
            return self._answer(cq, "This card is closed.")
        callback_id = str(cq.get("id") or "")
        if not callback_id or not self.store.claim_edit_callback(callback_id):
            return self._answer(cq, "This request was already handled. Tap Edit again if no prompt arrived.")
        reply = self.store.reply(card["reply_id"]) or {}
        prompt = self.notifier.send_text("Reply to this message with the new email text for %s."
                                         % html.escape(reply.get("lead_email", "this lead")), force_reply=True)
        self.store.set_card(card["id"], edit_prompt_id=prompt)
        self._answer(cq, "Reply to my message with the new text.")

    def on_message(self, msg: dict[str, Any]) -> None:
        if not self._trusted((msg.get("chat") or {}).get("id"), (msg.get("from") or {}).get("id")):
            return
        prompt = (msg.get("reply_to_message") or {}).get("message_id")
        card = self.store.card_for_prompt(str(prompt)) if prompt is not None else None
        if card is None:
            return
        text = str(msg.get("text") or "").strip()
        if not text or len(text) > drafts.MAX_CHARS:
            self._notify("That edit was empty or too long, so the draft did not change.")
        elif self.store.edit_draft(card["id"], text):
            self._refresh(card["id"], "Edited. Tap Send to send this version.")
            self.say("%s: draft edited" % self._who(card))
        else:
            self._notify("That card is already closed, so the edit was not applied.")

    # ---- locked sends: a person decides --------------------------------------------------------

    def resolve(self, send_id: int, sent: bool, note: str = "") -> str:
        """Record what a person found after checking a locked send. Never sends."""
        to = "resolved_sent" if sent else "resolved_not_sent"
        why = "a person checked: it was %s%s" % ("sent" if sent else "not sent", ": " + note if note else "")
        if not self.store.move_send(send_id, to, ("locked",), why):
            row = self.store.send(send_id)
            return "send %d is %s, not locked; nothing changed" % (send_id, row["state"] if row else "missing")
        self.settle_send(send_id)
        return "send %d marked %s" % (send_id, "sent" if sent else "not sent")

    def recheck(self, send_id: int) -> str:
        """Read a locked send back once more. Confirms it if it matches. Never sends."""
        result = self.sender.recheck(send_id)
        if result.state == "confirmed":
            self.settle_send(send_id)
        return "send %d: %s" % (send_id, result.detail)

    def settle_send(self, send_id: int) -> None:
        row = self.store.send(send_id)
        card = self.store.card(row["card_id"]) if row and row["card_id"] else None
        if row and card:
            self._settle(card, row["state"], row["note"] or "", send_id)

    # ---- outcomes ------------------------------------------------------------------------------

    def key(self, card: dict[str, Any]) -> str:
        """One ledger key per reply and per follow-up step."""
        if card["kind"] == "followup":
            return "followup:%d" % card["followup_id"]
        return "reply:%d" % card["reply_id"]

    def _back_to_pending(self, card: dict[str, Any], why: str) -> None:
        self.store.move_card(card["id"], "pending", ("sending",), why)
        self._refresh(card["id"], "Not sent: %s." % why)
        self.say("%s: not sent: %s" % (self._who(card), why))

    def _settle(self, card: dict[str, Any], state: str, detail: str, send_id: int | None = None) -> None:
        """Move a card to the outcome of its send (or of a check that stopped it)."""
        to = {"confirmed": "sent", "resolved_sent": "sent", "locked": "locked", "resolved_not_sent": "closed",
              "stale": "stale", "dry_run": "dry_run", "closed": "closed"}.get(state)
        row = self.store.send(send_id) if send_id is not None else None
        at = row["updated_at"] if row and to == "sent" else None
        if to is None or not self.store.move_card(card["id"], to, ("sending", "locked"), detail, at=at):
            return
        self.stats[{"sent": "confirmed"}.get(to, to)] += 1
        status = {
            "sent": "Sent once and confirmed: %s." % detail,
            "locked": "LOCKED: %s. Stop the service, check the sent folder, then run: "
                      "reply-desk resolve %s --sent (or --not-sent), then restart the service" % (detail, send_id),
        }.get(to, detail[:1].upper() + detail[1:] + ".")
        self._refresh(card["id"], status)
        self.say("%s: %s" % (self._who(card), status))
        self._finish_followup(self.store.card(card["id"]) or card)
        if to == "sent":
            self._arm(self.store.card(card["id"]) or card)

    def _finish_followup(self, card: dict[str, Any]) -> None:
        if card["kind"] == "followup" and card["state"] in ("skipped", "stale", "closed", "dry_run"):
            self.store.move_followup(card["followup_id"], "cancelled" if card["state"] == "stale" else "done",
                                     ("carded",), card["note"] or card["state"])

    def _arm(self, card: dict[str, Any]) -> None:
        """Schedule the next follow-up after a confirmed send, timed to what the person wrote."""
        gaps = [int(g) for g in self.cfg["followups"]["gaps_days"]]
        reply = self.store.reply(card["reply_id"])
        if not gaps or reply is None:
            return
        now = (parse_ts(card["updated_at"]) or self.clock()).astimezone(self.tz)
        if card["kind"] == "reply":
            hint = self._hint(reply)
            if reply["label"] == "not_now" and hint is None:
                return  # a plain no: no follow-ups
            step = 1
            if hint:  # never sooner than a day after this send, even if the card waited a while
                due, why, kind = max(hint.due, self._days_from(now, 1)), 'they wrote "%s"' % hint.phrase, hint.kind
            else:
                due, why, kind = self._days_from(now, gaps[0]), "%d days after your reply" % gaps[0], ""
        else:
            followup = self.store.followup(card["followup_id"])
            assert followup is not None
            self.store.move_followup(followup["id"], "done", ("carded",), "sent")
            step = followup["step"] + 1
            if step > len(gaps):
                return
            gap = gaps[step - 1]
            due, why, kind = self._days_from(now, gap), "%d days after the last follow-up" % gap, ""
        if self.store.schedule_followup(reply["id"], step, due, why, kind):
            self.say("follow-up %d for %s planned for %s %s: %s"
                     % (step, reply["lead_email"], timing.human_day(due), due.strftime("%H:%M"), why))

    def _days_from(self, now: dt.datetime, days: int) -> dt.datetime:
        day = timing.next_business_day((now + dt.timedelta(days=days)).date())
        return dt.datetime.combine(day, dt.time(int(self.cfg["followups"]["send_hour"])), tzinfo=now.tzinfo)

    # ---- follow-ups ----------------------------------------------------------------------------

    def followups(self) -> None:
        total = len(self.cfg["followups"]["gaps_days"])
        if not total:
            self.store.disable_followups()
            return
        for f in self.store.due_followups(self.clock()):
            reply = self.store.reply(f["reply_id"])
            assert reply is not None
            if self.store.is_suppressed(reply["lead_email"]):
                self.store.move_followup(f["id"], "cancelled", ("scheduled",), "unsubscribed")
                continue
            try:
                thread = self.inbox.thread(reply["thread_id"], reply["lead_email"])
                newer = newer_inbound(thread, reply)
            except Exception as e:
                raise_if_rate_limited(e)
                self.store.log("followup.thread_failed", f["id"], short(e))
                continue  # try again next tick
            if newer:
                self.store.move_followup(f["id"], "cancelled", ("scheduled",), "they replied")
                self.stats["followups_cancelled"] += 1
                continue
            fallback = drafts.followup_draft(self.cfg, reply, f["step"], total, f["hint"])
            card = self.store.create_card(reply["id"], "followup", fallback, followup_id=f["id"])
            draft = self.drafter.followup(reply, thread, f["step"], total, f["why"]) if self.drafter else None
            values = drafts.values_for(self.cfg, reply)
            try:
                text = drafts.fill(draft, values) if draft else ""
            except (ValueError, KeyError, IndexError):
                text = ""
            if text:
                self.store.edit_draft(card["id"], text)
            self._post(self.store.card(card["id"]) or card)
            self.stats["followups_carded"] += 1

    # ---- cards and messages --------------------------------------------------------------------

    def _render(self, card: dict[str, Any], status: str = "") -> str:
        reply = self.store.reply(card["reply_id"])
        assert reply is not None
        followup = self.store.followup(card["followup_id"]) if card["followup_id"] else None
        return card_text(card, reply, followup, len(self.cfg["followups"]["gaps_days"]), status)

    def _post(self, card: dict[str, Any]) -> None:
        text = self._render(card)
        if not self.store.claim_card_post(card["id"]):
            return
        try:
            message_id = self.notifier.send_card(text, buttons(card["id"], card["draft"]))
            if not isinstance(message_id, str) or not message_id.strip():
                raise ValueError("Telegram returned no message ID")
        except Exception as e:
            self.store.finish_card_post(card["id"], None, card["draft"], "card post outcome unclear: " + short(e))
            raise
        self.store.finish_card_post(card["id"], message_id, card["draft"])
        self.stats["cards"] += 1

    def _refresh(self, card_id: str, status: str) -> None:
        card = self.store.card(card_id)
        if not card or not card["message_id"]:
            return
        try:
            self.notifier.update_card(card["message_id"], self._render(card, status),
                                      buttons(card_id, card["draft"]) if card["state"] == "pending" else ())
            self.store.mark_card_reviewed(card_id, card["draft"])
        except Exception as e:
            raise_if_rate_limited(e)
            self.store.log("card.refresh_failed", card_id, short(e))

    def _who(self, card: dict[str, Any]) -> str:
        reply = self.store.reply(card["reply_id"]) or {}
        name = reply.get("first_name") or reply.get("lead_email") or "?"
        if reply.get("company"):
            name += " (%s)" % reply["company"]
        return ("follow-up for " if card["kind"] == "followup" else "") + name

    def _answer(self, cq: dict[str, Any], text: str) -> None:
        try:
            self.notifier.answer(str(cq.get("id", "")), text)
        except Exception as e:  # an old tap can't be answered; that is fine
            raise_if_rate_limited(e)
            self.store.log("tap.answer_failed", cq.get("id", ""), short(e))

    def _notify(self, text: str) -> None:
        try:
            self.notifier.send_text(html.escape(text))
        except Exception as e:
            raise_if_rate_limited(e)
            self.store.log("notify.failed", "", short(e))
