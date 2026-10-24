"""The desk end to end with fakes: cards, taps, page gates, restarts, follow-ups."""

import datetime as dt

import pytest
from conftest import NOW, OURS, Crash

from reply_desk.desk import Desk

PAT = "pat@example.com"


def interested(rig, text="Sounds interesting, tell me more.", **kw):
    rig.inbox.add_inbound(PAT, text, **kw)
    rig.desk.tick()
    return rig.card(PAT)


def tap(rig, card, action="send", **kw):
    rig.tg.tap(card["id"], action, **kw)
    rig.desk.tick(poll=False)
    return rig.store.card(card["id"])


def test_an_interested_reply_gets_a_live_page_before_its_card(rig):
    card = interested(rig)
    reply = rig.store.reply(card["reply_id"])
    assert reply["label"] == "interested" and reply["page_url"] in card["draft"]
    page = rig.cfg.site_dir / reply["page_url"].rstrip("/").rsplit("/", 1)[-1] / "index.html"
    assert reply["page_marker"] in page.read_text()
    assert len(rig.tg.cards) == 1 and "Pat" in card["draft"]


def test_the_card_waits_until_the_page_is_live(rig):
    rig.desk.fetch = lambda url: (404, "")
    assert interested(rig) is None and rig.store.replies("waiting_page")
    rig.desk.fetch = rig.fetch
    rig.desk.tick(poll=False)
    assert rig.card(PAT) is not None


def test_a_page_that_never_goes_live_gets_an_alert_and_no_card(make_rig):
    rig = make_rig(pages={"check_attempts": 2})
    rig.desk.fetch = lambda url: (200, "<html>an old copy</html>")
    interested(rig)
    rig.desk.tick(poll=False)
    assert rig.card(PAT) is None and rig.store.replies("failed")
    assert "never went live" in rig.tg.texts[-1]


def test_send_checks_the_page_again_and_sends_nothing_if_it_is_down(rig):
    card = interested(rig)
    rig.desk.fetch = lambda url: (503, "")
    assert tap(rig, card)["state"] == "pending"
    assert rig.inbox.sent == [] and rig.store.sends("intent", "accepted", "confirmed", "locked") == []


def test_a_double_tap_sends_once(rig):
    card = interested(rig)
    rig.tg.tap(card["id"])
    rig.tg.tap(card["id"])
    rig.desk.tick(poll=False)
    assert len(rig.inbox.sent) == 1
    assert rig.store.card(card["id"])["state"] == "sent"
    assert "Already handled. Nothing was sent again." in rig.tg.answers


def test_a_tap_delivered_again_after_a_restart_sends_nothing(rig):
    card = interested(rig)
    tap(rig, card)
    rig.restart()
    rig.store.put("telegram.offset", "0")  # Telegram redelivers what was never acknowledged
    tap(rig, card)
    assert len(rig.inbox.sent) == 1


def test_a_restart_mid_send_settles_the_card_without_sending_again(rig):
    card = interested(rig)
    rig.inbox.script = ["crash"]
    rig.tg.tap(card["id"])
    with pytest.raises(Crash):
        rig.desk.tick(poll=False)
    assert rig.store.card(card["id"])["state"] == "sending"
    rig.restart().tick(poll=False)  # recovery, then the unacknowledged tap arrives again
    assert rig.store.card(card["id"])["state"] == "sent"
    assert len(rig.inbox.sent) == 1 and rig.tg.answers[-1] == "Already handled. Nothing was sent again."


def test_a_restart_before_the_intent_puts_the_card_back(rig):
    card = interested(rig)
    rig.store.claim_card(card["id"])  # the process died right after the tap was claimed
    rig.restart().tick(poll=False)
    assert rig.store.card(card["id"])["state"] == "pending" and rig.inbox.sent == []


def test_a_locked_send_locks_the_card_and_a_person_settles_it(rig):
    card = interested(rig)
    rig.inbox.script = ["never"]
    assert tap(rig, card)["state"] == "locked"
    [locked] = rig.store.sends("locked")
    assert "reply-desk resolve %d" % locked["id"] in rig.tg.cards[card["message_id"]][0]
    assert rig.desk.resolve(locked["id"], sent=True) == "send %d marked sent" % locked["id"]
    assert rig.store.card(card["id"])["state"] == "sent" and rig.store.followups("scheduled")
    assert "not locked" in rig.desk.resolve(locked["id"], sent=False)
    assert len(rig.inbox.sent) == 1


def test_recheck_confirms_a_send_whose_answer_was_lost(rig):
    card = interested(rig)
    rig.inbox.script = ["timeout"]
    tap(rig, card)
    [locked] = rig.store.sends("locked")
    assert "recheck matched" in rig.desk.recheck(locked["id"])
    assert rig.store.card(card["id"])["state"] == "sent" and len(rig.inbox.sent) == 1


def test_a_card_goes_stale_when_they_write_again(rig):
    card = interested(rig, minutes_ago=60)
    rig.inbox.add_inbound(PAT, "Actually, call me instead.", minutes_ago=5)  # not polled yet
    assert tap(rig, card)["state"] == "stale" and rig.inbox.sent == []


def test_dry_run_runs_every_check_and_sends_nothing(make_rig):
    rig = make_rig(sending={"mode": "dry-run"})
    card = interested(rig)
    assert tap(rig, card)["state"] == "dry_run"
    assert rig.inbox.sent == [] and rig.store.sends("intent", "locked", "confirmed") == []


def test_dry_run_cannot_reach_the_send_call_at_all(make_rig):
    rig = make_rig(sending={"mode": "dry-run"})
    target = rig.inbox.add_inbound(PAT, "hi")
    r = rig.desk.sender.send("k", {"reply_to_id": target["id"], "sender": OURS, "recipient": PAT,
                                   "subject": "Re: Hello", "body": "hello"})
    assert r.state == "locked" and "dry-run" in r.detail and rig.inbox.sent == []


def test_an_unsubscribe_is_suppressed_with_no_card(rig):
    rig.inbox.add_inbound("riley@example.org", "Please remove me from your list.")
    rig.desk.tick()
    assert rig.card("riley@example.org") is None and rig.store.is_suppressed("riley@example.org")
    assert rig.tg.texts[-1].startswith("Unsubscribed: riley@example.org")
    rig.inbox.add_inbound("riley@example.org", "Hello?", thread="th-2", minutes_ago=1)
    rig.desk.tick()
    assert rig.card("riley@example.org") is None


def test_nothing_is_sent_to_an_address_that_unsubscribed(rig):
    card = interested(rig)
    rig.store.suppress(PAT, "asked elsewhere")
    assert tap(rig, card)["state"] == "closed" and rig.inbox.sent == []


@pytest.mark.parametrize("mode,blocked", [("live", PAT), ("dry-run", None)])
def test_the_block_list_is_only_touched_in_live_mode(make_rig, mode, blocked):
    rig = make_rig(sending={"mode": mode}, inbox={"blocklist_unsubscribes": True})
    rig.inbox.add_inbound(PAT, "Unsubscribe")
    rig.desk.tick()
    assert getattr(rig.inbox, "blocked", None) == blocked


def test_taps_from_another_chat_or_user_are_refused(make_rig):
    rig = make_rig(telegram={"allowed_user_ids": [7]})
    card = interested(rig)
    assert tap(rig, card, chat="another-chat", user=7)["state"] == "pending"
    assert tap(rig, card, user=8)["state"] == "pending"
    assert rig.tg.answers == ["Not allowed.", "Not allowed."] and rig.inbox.sent == []
    assert tap(rig, card, user=7)["state"] == "sent"


def test_edit_then_send_uses_the_new_text(rig):
    rig.inbox.add_inbound("alex@example.org", "Do you work with businesses outside the US?")
    rig.desk.tick()
    card = rig.card("alex@example.org")
    assert "[[ write your answer here ]]" in card["draft"]
    assert tap(rig, card)["state"] == "pending" and rig.inbox.sent == []  # a part is still to write
    tap(rig, card, "edit")
    prompt = rig.store.card(card["id"])["edit_prompt_id"]
    rig.tg.reply_to(prompt, "Hi Alex,\n\nYes, anywhere.\n\nSam")
    assert tap(rig, card)["state"] == "sent"
    assert rig.inbox.sent[0]["body"] == "Hi Alex,\n\nYes, anywhere.\n\nSam"


def test_skip_sends_nothing(rig):
    card = interested(rig)
    assert tap(rig, card, "skip")["state"] == "skipped"
    assert tap(rig, card)["state"] == "skipped" and rig.inbox.sent == []


def test_follow_ups_are_timed_to_what_they_wrote_and_stop_when_they_reply(rig):
    card = interested(rig, "Sounds interesting. I'll look it over this weekend.")
    tap(rig, card)
    [f] = rig.store.followups("scheduled")
    assert f["due_at"] == "2026-10-26T09:00:00+00:00" and '"this weekend"' in f["why"]  # the Monday after
    rig.clock.t = dt.datetime(2026, 10, 26, 9, 0, tzinfo=dt.UTC)
    rig.desk.tick(poll=False)
    follow = rig.card(PAT)
    assert follow["kind"] == "followup" and "Hope the weekend went well" in follow["draft"]
    assert tap(rig, follow)["state"] == "sent"
    [nxt] = rig.store.followups("scheduled")
    assert nxt["step"] == 2 and nxt["due_at"] == "2026-10-30T09:00:00+00:00"
    rig.inbox.add_inbound(PAT, "Thanks, let's talk next month.", minutes_ago=-60 * 24 * 5)
    rig.desk.tick()
    assert rig.store.followups("scheduled") == [] and len(rig.inbox.sent) == 2


def test_a_due_follow_up_is_dropped_if_they_already_wrote(rig):
    tap(rig, interested(rig))
    rig.inbox.add_inbound(PAT, "One more thing", minutes_ago=-60)  # in the thread, not yet polled
    rig.clock.t = NOW + dt.timedelta(days=5)
    rig.desk.tick(poll=False)
    assert rig.store.followups("cancelled") and rig.card(PAT)["kind"] == "reply"


def test_a_plain_no_gets_a_polite_close_and_no_follow_ups(rig):
    rig.inbox.add_inbound("morgan@example.org", "No thanks, we're all set.")
    rig.desk.tick()
    card = rig.card("morgan@example.org")
    assert "I won't follow up" in card["draft"]
    assert tap(rig, card)["state"] == "sent" and rig.store.followups("scheduled") == []


def test_the_same_text_twice_to_one_address_goes_back_for_an_edit(rig):
    rig.inbox.add_inbound("morgan@example.org", "No thanks.", thread="th-1", minutes_ago=50)
    rig.inbox.add_inbound("morgan@example.org", "No thanks, really.", thread="th-2", minutes_ago=40)
    rig.desk.tick()
    first, second = [c for c in rig.store.cards("pending")]
    assert first["draft"] == second["draft"]
    assert tap(rig, first)["state"] == "sent"
    assert tap(rig, second)["state"] == "pending" and len(rig.inbox.sent) == 1
    assert "edit the text" in rig.tg.cards[second["message_id"]][0]


def test_a_closed_stdout_does_not_interrupt_a_send(rig):
    def broken(line):
        raise BrokenPipeError()

    rig.desk = Desk(rig.cfg, rig.store, rig.inbox, rig.tg, fetch=rig.fetch, clock=rig.clock.now,
                    sleep=rig.clock.sleep, say=broken)
    assert tap(rig, interested(rig))["state"] == "sent"


def test_auto_replies_get_no_card_and_each_reply_is_stored_once(rig):
    rig.inbox.add_inbound("ooo@example.com", "I am out of the office until Monday.", thread="th-9", auto=True)
    rig.desk.tick()
    rig.desk.tick()
    assert rig.card("ooo@example.com") is None
    assert rig.store.counts()["replies"] == {"ignored": 1}
