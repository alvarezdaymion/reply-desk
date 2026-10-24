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
    rig.desk.tick(poll=False)
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
    assert f["due_at"] == "2026-10-26T09:00:00.000000+00:00" and '"this weekend"' in f["why"]  # the Monday after
    rig.clock.t = dt.datetime(2026, 10, 26, 9, 0, tzinfo=dt.UTC)
    rig.desk.tick(poll=False)
    follow = rig.card(PAT)
    assert follow["kind"] == "followup" and "Hope the weekend went well" in follow["draft"]
    assert tap(rig, follow)["state"] == "sent"
    [nxt] = rig.store.followups("scheduled")
    assert nxt["step"] == 2 and nxt["due_at"] == "2026-10-30T09:00:00.000000+00:00"
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
    rig.desk.tick()
    first = rig.card("morgan@example.org")
    assert tap(rig, first)["state"] == "sent"
    rig.inbox.add_inbound("morgan@example.org", "No thanks, really.", thread="th-2", minutes_ago=40)
    rig.desk.tick()
    second = rig.card("morgan@example.org")
    assert first["draft"] == second["draft"]
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


def test_truncated_reply_is_retried_without_drafting_until_complete(rig, monkeypatch):
    from reply_desk.http import HttpError

    full = rig.inbox.add_inbound(PAT, "Sounds interesting. Please remove me from your list.")
    excerpt = {**full, "body": {"text": "Sounds interesting."}, "body_fallback_reason": "too_large"}
    monkeypatch.setattr(rig.inbox, "list_replies", lambda since: [excerpt])
    original = rig.inbox.get_email

    def unavailable(mid):
        raise HttpError("read failed")

    monkeypatch.setattr(rig.inbox, "get_email", unavailable)
    for _ in range(4):
        rig.desk.tick()
    assert rig.card(PAT) is None
    [waiting] = rig.store.replies("new")
    assert not waiting["body_complete"]
    monkeypatch.setattr(rig.inbox, "get_email", original)
    rig.store.close()
    rig.restart().tick(poll=False)
    assert rig.store.is_suppressed(PAT) and rig.inbox.sent == []


@pytest.mark.parametrize("response", [{}, {"id": "wrong", "body": {"text": "Yes"}},
                                      {"id": "in-1", "content_preview": "Yes"},
                                      {"id": "in-1", "body": {"text": "Yes"}, "body_fallback_reason": "large"}])
def test_incomplete_body_fetch_cannot_release_a_reply(rig, monkeypatch, response):
    full = rig.inbox.add_inbound(PAT, "Sounds interesting.")
    monkeypatch.setattr(rig.inbox, "list_replies", lambda since: [{**full, "body_fallback_reason": "large"}])
    monkeypatch.setattr(rig.inbox, "get_email", lambda mid: response)
    rig.desk.tick()
    assert rig.card(PAT) is None and rig.inbox.sent == []


def test_later_thread_page_blocks_a_stale_approval(rig, monkeypatch):
    import json

    from reply_desk.connectors.instantly import InstantlyClient

    card = interested(rig)
    target = rig.inbox.emails["in-1"]
    latest = {**target, "id": "in-2", "timestamp_created": "2026-10-21T13:59:00Z",
              "body": {"text": "Unsubscribe"}}
    responses = iter([{"items": [target], "next_starting_after": "c1"}, {"items": [latest]}])
    client = InstantlyClient(api_key="test-key", transport=lambda *args: (200, json.dumps(next(responses)).encode()))
    monkeypatch.setattr(rig.inbox, "thread", client.thread)
    assert tap(rig, card)["state"] == "stale"
    assert rig.inbox.sent == []


def test_poll_resumes_a_backlog_across_restart(rig, monkeypatch):
    import json
    from urllib.parse import parse_qs, urlsplit

    from reply_desk.connectors.instantly import InstantlyClient

    target = rig.inbox.add_inbound(PAT, "Question?")
    items = [{**target, "id": "in-%d" % i} for i in range(401)]
    calls = []

    def transport(method, url, headers, body, timeout):
        query = parse_qs(urlsplit(url).query)
        offset = int(query.get("starting_after", ["0"])[0])
        calls.append((offset, query["min_timestamp_created"][0]))
        page = {"items": items[offset:offset + 100]}
        if offset + 100 < len(items):
            page["next_starting_after"] = str(offset + 100)
        return 200, json.dumps(page).encode()

    monkeypatch.setattr(rig.desk, "inbox", InstantlyClient(api_key="test-key", transport=transport))
    rig.desk.poll()
    assert len(calls) == 3 and not rig.store.has_reply("in-400")
    rig.store.close()
    rig.restart()
    rig.clock.t += dt.timedelta(days=1)
    monkeypatch.setattr(rig.desk, "inbox", InstantlyClient(api_key="test-key", transport=transport))
    rig.desk.poll()
    assert rig.store.has_reply("in-400")
    assert calls[3][0] == 300 and len({since for _, since in calls}) == 1


def test_uncertain_card_post_never_repeats_or_creates_a_second_card(rig, monkeypatch):
    from reply_desk.http import HttpError

    original = rig.tg.send_card
    calls = []

    def uncertain(text, buttons):
        calls.append(True)
        original(text, buttons)
        raise HttpError("response lost")

    monkeypatch.setattr(rig.tg, "send_card", uncertain)
    interested(rig)
    rig.store.close()
    rig.restart().tick()
    rig.desk.tick()
    [card] = rig.store.cards("pending")
    assert rig.store.reply(card["reply_id"])["status"] == "carded"
    assert len(calls) == 1 and card["post_state"] == "locked"
    assert tap(rig, card)["state"] == "pending" and not rig.inbox.sent


def test_restart_after_post_acceptance_never_reposts(rig, monkeypatch):
    original = rig.tg.send_card

    def crash(text, buttons):
        original(text, buttons)
        raise Crash()

    monkeypatch.setattr(rig.tg, "send_card", crash)
    with pytest.raises(Crash):
        interested(rig)
    monkeypatch.setattr(rig.tg, "send_card", original)
    rig.store.close()
    rig.restart().tick()
    assert len(rig.tg.cards) == 1
    assert rig.card(PAT)["post_state"] == "locked"


def test_clipped_draft_cannot_be_sent(make_rig):
    rig = make_rig(drafts={"interested": "x" * 2501})
    card = interested(rig)
    assert tap(rig, card)["state"] == "pending" and not rig.inbox.sent
    assert "2500" in rig.store.card(card["id"])["note"]


def test_unknown_placeholders_in_an_edit_block_sending(rig):
    card = interested(rig)
    rig.store.edit_draft(card["id"], "Hello {contact_name}, the amount is {price}.")
    assert tap(rig, card)["state"] == "pending" and not rig.inbox.sent


def test_recovery_schedules_followup_from_the_original_settlement_time(rig, monkeypatch):
    card = interested(rig)

    def crash(card):
        raise Crash()

    monkeypatch.setattr(rig.desk, "_arm", crash)
    with pytest.raises(Crash):
        tap(rig, card)
    assert rig.store.card(card["id"])["state"] == "sent"
    rig.store.close()
    rig.clock.t += dt.timedelta(days=1)
    rig.restart().recover()
    rig.desk.recover()
    [followup] = rig.store.followups("scheduled")
    assert followup["due_at"] == "2026-10-23T09:00:00.000000+00:00"
    assert len(rig.inbox.sent) == 1


def test_a_failed_edit_refresh_cannot_approve_the_unseen_text(rig, monkeypatch):
    from reply_desk.http import HttpError

    card = interested(rig)
    tap(rig, card, "edit")
    prompt = rig.store.card(card["id"])["edit_prompt_id"]

    def failed(*args):
        raise HttpError("update failed")

    monkeypatch.setattr(rig.tg, "update_card", failed)
    rig.tg.reply_to(prompt, "This replacement must be displayed before approval.")
    rig.desk.tick(poll=False)
    assert tap(rig, card)["state"] == "pending" and not rig.inbox.sent


def test_an_old_button_cannot_approve_a_successfully_displayed_edit(rig):
    card = interested(rig)
    rig.tg.tap(card["id"])
    old_tap = rig.tg.queue.pop()["callback_query"]
    tap(rig, card, "edit")
    prompt = rig.store.card(card["id"])["edit_prompt_id"]
    rig.tg.reply_to(prompt, "New terms for approval.")
    rig.desk.tick(poll=False)
    rig.desk.on_tap(old_tap)
    assert rig.inbox.sent == []
    assert tap(rig, card)["state"] == "sent"


@pytest.mark.parametrize("already_carded", [False, True])
def test_disabling_followups_cancels_existing_work_and_blocks_old_buttons(rig, already_carded):
    tap(rig, interested(rig))
    rig.clock.t += dt.timedelta(days=7)
    if already_carded:
        rig.desk.followups()
        old = rig.card(PAT)
        rig.tg.tap(old["id"])
    rig.cfg.data["followups"]["gaps_days"] = []
    rig.store.close()
    rig.restart().tick(poll=False)
    assert not rig.store.followups("scheduled", "carded")
    assert not rig.store.cards("pending") and len(rig.inbox.sent) == 1


@pytest.mark.parametrize("sent", [True, False])
def test_recovery_reconciles_a_resolution_committed_before_card_settlement(rig, monkeypatch, sent):
    card = interested(rig)
    rig.inbox.script = ["never"]
    tap(rig, card)
    [send] = rig.store.sends("locked")

    def crash(send_id):
        raise Crash()

    monkeypatch.setattr(rig.desk, "settle_send", crash)
    with pytest.raises(Crash):
        rig.desk.resolve(send["id"], sent=sent)
    rig.store.close()
    rig.restart().recover()
    assert rig.store.card(card["id"])["state"] == ("sent" if sent else "closed")
    assert len(rig.inbox.sent) == 1


@pytest.mark.parametrize("thread", [[], [{"id": "in-2", "ue_type": 2}], [{"id": "in-2"}]])
def test_an_uncertain_thread_blocks_approval(rig, monkeypatch, thread):
    card = interested(rig)
    monkeypatch.setattr(rig.inbox, "thread", lambda *args: thread)
    assert tap(rig, card)["state"] == "pending"
    assert rig.inbox.sent == []


def test_restart_does_not_repeat_an_uncertain_page_publish(rig):
    calls = []

    class Publisher:
        def publish(self, site_dir, slug):
            calls.append(slug)
            raise Crash()

    publisher = Publisher()
    rig.desk.publisher = publisher
    with pytest.raises(Crash):
        interested(rig)
    rig.store.close()
    rig.restart()
    rig.desk.publisher = publisher
    rig.desk.tick(poll=False)
    assert len(calls) == 1 and rig.card(PAT)


def test_restart_uses_rules_after_an_uncertain_model_call(rig):
    calls = []

    class Drafter:
        def classify(self, reply, verdict):
            calls.append(True)
            raise Crash()

    drafter = Drafter()
    rig.desk.drafter = drafter
    with pytest.raises(Crash):
        interested(rig)
    rig.store.close()
    rig.restart()
    rig.desk.drafter = drafter
    rig.desk.tick(poll=False)
    assert len(calls) == 1 and rig.card(PAT)


def test_restart_uses_the_saved_rule_followup_after_an_uncertain_model_call(rig):
    tap(rig, interested(rig))
    rig.clock.t += dt.timedelta(days=7)
    calls = []

    class Drafter:
        def followup(self, *args):
            calls.append(True)
            raise Crash()

    rig.desk.drafter = Drafter()
    with pytest.raises(Crash):
        rig.desk.followups()
    rig.store.close()
    rig.restart().tick(poll=False)
    card = rig.card(PAT)
    assert len(calls) == 1 and card["kind"] == "followup" and card["message_id"]


def test_a_redelivered_edit_tap_never_repeats_an_uncertain_prompt(rig, monkeypatch):
    card = interested(rig)
    original = rig.tg.send_text

    def crash(text, force_reply=False):
        original(text, force_reply)
        raise Crash()

    monkeypatch.setattr(rig.tg, "send_text", crash)
    rig.tg.tap(card["id"], "edit")
    with pytest.raises(Crash):
        rig.desk.tick(poll=False)
    monkeypatch.setattr(rig.tg, "send_text", original)
    rig.store.close()
    rig.restart().tick(poll=False)
    assert len(rig.tg.texts) == 1


@pytest.mark.parametrize("bad", [{"id": "in-2", "ue_type": 2},
                                  {"id": "in-2", "ue_type": "2", "timestamp_created": "2026-10-21T13:59:00Z"},
                                  {"id": "in-2", "ue_type": 99}])
def test_malformed_metadata_alongside_the_original_reply_blocks_sending(rig, monkeypatch, bad):
    card = interested(rig)
    target = rig.inbox.emails["in-1"]
    monkeypatch.setattr(rig.inbox, "thread", lambda *args: [target, bad])
    assert tap(rig, card)["state"] == "pending" and not rig.inbox.sent


def test_model_rendering_failure_uses_rules_without_repeating_the_call(rig):
    from reply_desk.classify import Verdict

    calls = []

    class Drafter:
        def classify(self, reply, verdict):
            calls.append(True)
            return Verdict("question", "question", "model", "Answer\n{signature:invalid}")

    rig.desk.drafter = Drafter()
    rig.inbox.add_inbound(PAT, "What does it cost?")
    rig.desk.tick()
    rig.desk.tick(poll=False)
    card = rig.card(PAT)
    assert "A flat monthly fee." in card["draft"] and len(calls) == 1


def test_followup_rendering_failure_keeps_the_saved_rule_draft(rig):
    tap(rig, interested(rig))
    rig.clock.t += dt.timedelta(days=7)

    class Drafter:
        def followup(self, *args):
            return "Checking in\n{signature:invalid}"

    rig.desk.drafter = Drafter()
    rig.desk.followups()
    card = rig.card(PAT)
    assert card["kind"] == "followup" and "Just checking this reached you" in card["draft"]
    assert "{signature" not in card["draft"]


def test_rate_limited_card_post_stops_the_tick_and_persists_its_cooldown(rig, monkeypatch):
    from reply_desk.http import HttpError

    rig.inbox.add_inbound(PAT, "Tell me more")
    rig.inbox.add_inbound("lead@example.org", "Tell me more", thread="th-2")
    original = rig.tg.send_card
    posts = []

    def limited_once(text, controls):
        posts.append(True)
        if len(posts) == 1:
            raise HttpError("Telegram sendMessage returned HTTP 429", 429, 30)
        return original(text, controls)

    monkeypatch.setattr(rig.tg, "send_card", limited_once)
    report = rig.desk.tick()
    assert len(posts) == 1 and report["errors"] and report["retry_after"] == 30
    rig.store.close()
    rig.restart()
    rig.clock.t += dt.timedelta(seconds=29)
    assert rig.desk.tick()["retry_after"] == 1 and len(posts) == 1
    rig.clock.t += dt.timedelta(seconds=1)
    rig.desk.tick()
    assert len(posts) == 2
    assert rig.card(PAT)["post_state"] == "locked"
    assert rig.card("lead@example.org")["post_state"] == "posted"


def test_rate_limited_hydration_defers_later_triage_and_taps(rig, monkeypatch):
    from reply_desk.http import HttpError

    first = rig.inbox.add_inbound(PAT, "Tell me more")
    rig.inbox.emails[first["id"]]["body_fallback_reason"] = "large"
    rig.inbox.add_inbound("lead@example.org", "Tell me more", thread="th-2")
    calls = []

    def limited(mid):
        calls.append(mid)
        raise HttpError("Instantly get email returned HTTP 429", 429, 20)

    monkeypatch.setattr(rig.inbox, "get_email", limited)
    monkeypatch.setattr(rig.tg, "updates", lambda *args: pytest.fail("taps ran during cooldown"))
    report = rig.desk.tick()
    assert calls == [first["id"]] and report["retry_after"] == 20
    assert not rig.tg.cards and report["errors"]


@pytest.mark.parametrize("outcome", ["confirmed", "resolved_sent"])
def test_recovery_uses_ledger_settlement_time_before_moving_the_card(rig, monkeypatch, outcome):
    card = interested(rig)
    if outcome == "resolved_sent":
        rig.inbox.script = ["never"]
        tap(rig, card)
        [send] = rig.store.sends("locked")
        method, args = "settle_send", (send["id"],)
    else:
        method, args = "_settle", ()

    def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(rig.desk, method, crash)
    with pytest.raises(Crash):
        if outcome == "resolved_sent":
            rig.desk.resolve(args[0], sent=True)
        else:
            tap(rig, card)
    [send] = rig.store.sends(outcome)
    settled_at = send["updated_at"]
    rig.store.close()
    rig.clock.t += dt.timedelta(days=5)
    rig.restart().recover()
    rig.desk.recover()
    assert rig.store.card(card["id"])["updated_at"] == settled_at
    assert rig.store.followups("scheduled")[0]["due_at"] == "2026-10-23T09:00:00.000000+00:00"
    assert len(rig.inbox.sent) == 1


@pytest.mark.parametrize("terminal,followup_state", [("skipped", "done"), ("closed", "done"),
                                                   ("dry_run", "done"), ("stale", "cancelled")])
def test_recovery_reconciles_terminal_followup_cards(rig, terminal, followup_state):
    tap(rig, interested(rig))
    rig.clock.t += dt.timedelta(days=7)
    rig.desk.followups()
    card = rig.card(PAT)
    assert rig.store.move_card(card["id"], terminal, ("pending",), "handled")
    rig.store.close()
    rig.restart().recover()
    rig.desk.recover()
    assert rig.store.followup(card["followup_id"])["state"] == followup_state
    assert not rig.store.followups("scheduled", "carded") and len(rig.inbox.sent) == 1


@pytest.mark.parametrize("carded", [False, True])
def test_cross_thread_reply_cancels_followups_before_an_interrupted_model_call(rig, carded):
    tap(rig, interested(rig))
    [followup] = rig.store.followups("scheduled")
    old_card = None
    if carded:
        rig.clock.t += dt.timedelta(days=3)
        rig.desk.followups()
        old_card = rig.card(PAT)
    fresh = rig.inbox.add_inbound(PAT, "What does it cost?", thread="th-new")
    fresh["timestamp_created"] = rig.clock.now().isoformat()

    class InterruptedDrafter:
        def classify(self, reply, verdict):
            raise Crash()

    rig.desk.drafter = InterruptedDrafter()
    with pytest.raises(Crash):
        rig.desk.tick()
    assert rig.store.followup(followup["id"])["state"] == "cancelled"
    if old_card:
        assert rig.store.card(old_card["id"])["state"] == "stale"
    rig.store.close()
    rig.clock.t += dt.timedelta(days=3)
    rig.restart().tick(poll=False)
    if old_card:
        tap(rig, old_card)
    assert len(rig.inbox.sent) == 1


def test_unpolled_reply_in_another_thread_blocks_a_followup(rig):
    tap(rig, interested(rig))
    rig.clock.t += dt.timedelta(days=3)
    rig.desk.followups()
    followup = rig.card(PAT)
    fresh = rig.inbox.add_inbound(PAT, "One more question", thread="th-new")
    fresh["timestamp_created"] = rig.clock.now().isoformat()
    assert not rig.store.has_reply(fresh["id"])
    assert tap(rig, followup)["state"] == "stale" and len(rig.inbox.sent) == 1


def test_a_stored_newer_reply_blocks_send_even_if_the_provider_read_is_stale(rig, monkeypatch):
    card = interested(rig)
    old_thread = rig.inbox.thread("th-1", PAT)
    fresh = rig.inbox.add_inbound(PAT, "Another question", thread="th-new", minutes_ago=1)
    rig.desk.poll()
    assert rig.store.has_reply(fresh["id"])
    monkeypatch.setattr(rig.inbox, "thread", lambda *args: old_thread)
    assert tap(rig, card)["state"] == "stale" and not rig.inbox.sent


def test_a_delayed_older_reply_does_not_cancel_a_newer_followup(rig):
    tap(rig, interested(rig))
    [followup] = rig.store.followups("scheduled")
    rig.inbox.add_inbound(PAT, "Earlier question", thread="th-old", minutes_ago=63)
    rig.desk.tick()
    assert rig.store.followup(followup["id"])["state"] == "scheduled"


def test_edited_page_links_check_the_complete_destination(rig):
    card = interested(rig)
    reply = rig.store.reply(card["reply_id"])
    extended = reply["page_url"] + "details"
    rig.store.edit_draft(card["id"], "The details are here: " + extended)
    rig.desk._refresh(card["id"], "Edited")
    assert tap(rig, card)["state"] == "pending" and not rig.inbox.sent


@pytest.mark.parametrize("paged", [False, True])
def test_the_first_poll_window_is_saved_before_processing_replies(rig, monkeypatch, paged):
    first = rig.inbox.add_inbound("first@example.com", "Question?", minutes_ago=23 * 60)
    second = rig.inbox.add_inbound("second@example.com", "Question?", minutes_ago=23 * 60)
    if paged:
        monkeypatch.setattr(rig.inbox, "list_reply_page", lambda since, after: (rig.inbox.list_replies(since), ""),
                            raising=False)
    original = rig.inbox.lead

    def interrupted(email):
        if email == "second@example.com":
            raise Crash()
        return original(email)

    monkeypatch.setattr(rig.inbox, "lead", interrupted)
    with pytest.raises(Crash):
        rig.desk.poll()
    assert rig.store.has_reply(first["id"]) and rig.store.has_reply(second["id"])
    monkeypatch.setattr(rig.inbox, "lead", original)
    rig.store.close()
    rig.clock.t += dt.timedelta(hours=2)
    rig.restart().poll()
    assert rig.store.has_reply(second["id"])


@pytest.mark.parametrize("raised", [False, True])
def test_a_page_rate_limit_defers_the_tick_without_using_a_page_attempt(rig, monkeypatch, raised):
    from reply_desk.http import HttpError

    rig.inbox.add_inbound(PAT, "Tell me more")
    rig.inbox.add_inbound("second@example.com", "What does it cost?", thread="th-2", minutes_ago=30)
    checks = []

    def limited(url):
        checks.append(url)
        if raised:
            raise HttpError("page GET returned HTTP 429", 429, 30)
        return 429, ""

    monkeypatch.setattr(rig.desk, "fetch", limited)
    report = rig.desk.tick()
    assert len(checks) == 1 and report["errors"] and report["retry_after"] > 0
    [reply] = rig.store.replies("waiting_page")
    assert reply["attempts"] == 0 and not rig.tg.cards
    assert rig.store.get("api.retry_until")
    monkeypatch.setattr(rig.desk, "fetch", rig.fetch)
    rig.clock.sleep(report["retry_after"])
    rig.desk.tick(poll=False)
    assert rig.card(PAT) and rig.card("second@example.com")
    assert rig.store.reply(reply["id"])["attempts"] == 0


def test_a_booking_link_on_the_page_host_does_not_need_the_page_marker(make_rig):
    rig = make_rig(pages={"base_url": "https://example.com"})
    card = interested(rig)
    assert rig.cfg["business"]["booking_url"] in card["draft"]
    assert tap(rig, card)["state"] == "sent"


@pytest.mark.parametrize("suffix", ["'details", '"details', "<details>", "/details", "?view=1", "#details",
                                    ".details", ";details", "%27details", ".'details"])
def test_page_url_extensions_are_refused_before_any_live_check(rig, monkeypatch, suffix):
    card = interested(rig)
    reply = rig.store.reply(card["reply_id"])
    checks = []

    def live(url):
        checks.append(url)
        return 200, reply["page_marker"]

    monkeypatch.setattr(rig.desk, "fetch", live)
    rig.store.edit_draft(card["id"], "Overview: " + reply["page_url"] + suffix)
    rig.desk._refresh(card["id"], "Edited")
    assert tap(rig, card)["state"] == "pending"
    assert not checks and not rig.inbox.sent and rig.store.send_by_key(rig.desk.key(card)) is None


@pytest.mark.parametrize("suffix", ["", "\nThanks.", ".", ", thanks.", "!", "?", "; next steps.", ":\nDetails."])
def test_page_urls_allow_only_whitespace_or_sentence_punctuation_after_them(rig, monkeypatch, suffix):
    card = interested(rig)
    reply = rig.store.reply(card["reply_id"])
    checks = []

    def live(url):
        checks.append(url)
        return 200, reply["page_marker"]

    monkeypatch.setattr(rig.desk, "fetch", live)
    rig.store.edit_draft(card["id"], "Overview: " + reply["page_url"] + suffix)
    rig.desk._refresh(card["id"], "Edited")
    assert tap(rig, card)["state"] == "sent"
    assert [url.split("?rd-check=", 1)[0] for url in checks] == [reply["page_url"]]


def test_fractional_reply_order_survives_polling_and_restart(make_rig, monkeypatch):
    rig = make_rig(pages={"enabled": False})
    older = rig.inbox.add_inbound(PAT, "What does it cost?", thread="th-old")
    latest = rig.inbox.add_inbound(PAT, "Tell me more", thread="th-new")
    older["timestamp_created"] = "2026-10-21T13:00:00.125Z"
    latest["timestamp_created"] = "2026-10-21T15:00:00.875+02:00"
    rig.desk.tick()
    [first, last] = sorted(rig.store.cards("pending"), key=lambda c: c["reply_id"])
    assert rig.store.get("inbox.cursor") == "2026-10-21T13:00:00.875000+00:00"
    rig.store.close()
    rig.restart()
    monkeypatch.setattr(rig.inbox, "thread", lambda *args: [older, latest])
    assert tap(rig, last)["state"] == "sent"
    assert tap(rig, first)["state"] == "stale"
    assert [email["reply_to_id"] for email in rig.inbox.sent] == [latest["id"]]


@pytest.mark.parametrize("paged", [False, True])
def test_a_fetched_batch_is_saved_before_any_lead_lookup(rig, monkeypatch, paged):
    from reply_desk.http import HttpError

    card = interested(rig)
    snapshot = rig.inbox.thread("", PAT)
    rig.inbox.add_inbound("other@example.org", "What does it cost?", thread="th-other", minutes_ago=2)
    unsubscribe = rig.inbox.add_inbound(PAT, "Please unsubscribe", thread="th-new", minutes_ago=1)
    if paged:
        monkeypatch.setattr(rig.inbox, "list_reply_page", lambda since, after: (rig.inbox.list_replies(since), ""),
                            raising=False)

    def limited(email):
        raise HttpError("lead lookup returned HTTP 429", 429, 3)

    monkeypatch.setattr(rig.inbox, "lead", limited)
    monkeypatch.setattr(rig.inbox, "thread", lambda *args: snapshot)
    rig.tg.tap(card["id"])
    assert rig.desk.tick()["retry_after"] == 3
    assert rig.store.has_reply(unsubscribe["id"])
    assert not rig.inbox.sent
    rig.store.close()
    rig.clock.sleep(3)
    rig.restart().tick(poll=False)
    assert rig.store.is_suppressed(PAT) and not rig.inbox.sent


@pytest.mark.parametrize("rate_limited", [False, True])
def test_an_interrupted_poll_resumes_before_queued_approvals(rig, monkeypatch, rate_limited):
    from reply_desk.http import HttpError

    card = interested(rig)
    snapshot = rig.inbox.thread("", PAT)
    fresh = rig.inbox.add_inbound(PAT, "Unsubscribe", thread="th-new", minutes_ago=1)
    reads = []

    def interrupted(since):
        reads.append(since)
        if len(reads) == 1:
            raise HttpError("list replies unavailable", 429 if rate_limited else 503, 3 if rate_limited else None)
        return [fresh]

    monkeypatch.setattr(rig.inbox, "list_replies", interrupted)
    monkeypatch.setattr(rig.inbox, "thread", lambda *args: snapshot)
    rig.tg.tap(card["id"])
    assert rig.desk.tick()["errors"]
    assert not rig.inbox.sent and rig.store.card(card["id"])["state"] == "pending"
    rig.clock.sleep(3)
    rig.store.close()
    rig.restart().tick(poll=False)
    assert len(reads) == 2 and reads[0] == reads[1]
    assert rig.store.is_suppressed(PAT) and not rig.inbox.sent


def test_a_poll_backlog_finishes_before_queued_approvals(rig, monkeypatch):
    card = interested(rig)
    snapshot = rig.inbox.thread("", PAT)
    fresh = rig.inbox.add_inbound(PAT, "Unsubscribe", thread="th-new", minutes_ago=1)
    reads = []

    def page(since, after):
        reads.append(after)
        if after == "3":
            return [fresh], ""
        return [], str(int(after or "0") + 1)

    monkeypatch.setattr(rig.inbox, "list_reply_page", page, raising=False)
    monkeypatch.setattr(rig.inbox, "thread", lambda *args: snapshot)
    rig.tg.tap(card["id"])
    rig.desk.tick()
    assert reads == ["", "1", "2"] and not rig.inbox.sent
    rig.desk.tick(poll=False)
    assert reads == ["", "1", "2", "3"]
    assert rig.store.is_suppressed(PAT) and not rig.inbox.sent
