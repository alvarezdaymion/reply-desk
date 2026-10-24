"""The guardrail: each email is sent at most once, confirmed by reading it back, or locked for a person."""

import pytest
from conftest import OURS, Clock, Crash, FakeInbox

from reply_desk.http import HttpError
from reply_desk.sender import SafeSender, addresses, mismatch, normalize
from reply_desk.store import Store

LEAD = "lead@example.com"


def setup(tmp_path, *script):
    inbox, clock = FakeInbox(), Clock()
    inbox.script = list(script)
    target = inbox.add_inbound(LEAD, "Tell me more")
    store = Store(tmp_path / "desk.db", clock=clock.now)
    email = {"reply_to_id": target["id"], "thread_id": "th-1", "sender": OURS, "recipient": LEAD,
             "subject": "Re: Hello", "body": "Hi,\n\nHere is more.\n\nSam"}
    return SafeSender(store, inbox, (3, 10, 20), clock.sleep), store, inbox, clock, email


def restarted(store, inbox, clock):
    """A new process: a fresh connection to the same ledger file."""
    store.close()
    fresh = Store(store.path, clock=clock.now)
    return SafeSender(fresh, inbox, (3, 10, 20), clock.sleep), fresh


def test_confirmed_by_the_first_readback(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "ok")
    r = sender.send("reply:1", email)
    assert (r.state, r.called, r.readbacks) == ("confirmed", True, 1)
    assert len(inbox.sent) == 1 and clock.sleeps == [3]
    assert store.send(r.send_id)["provider_id"] == inbox.reads[0]


def test_accepted_but_unconfirmed_is_confirmed_on_the_second_readback(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "late:1")
    r = sender.send("reply:1", email)
    assert (r.state, r.readbacks) == ("confirmed", 2)
    assert clock.sleeps == [3, 7]  # read back at 3 s and 10 s after acceptance
    assert len(inbox.sent) == 1 and len(inbox.reads) == 2


def test_never_confirmed_is_locked_after_3_10_and_20_seconds_and_not_retried(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "never")
    r = sender.send("reply:1", email)
    assert r.state == "locked" and "no readback matched by 20 s" in r.detail
    assert clock.sleeps == [3, 7, 10]
    assert len(inbox.sent) == 1 and len(inbox.reads) == 3
    assert store.send(r.send_id)["state"] == "locked"


@pytest.mark.parametrize("outcome,why", [("timeout", "outcome is unknown"), ("reject", "outcome is unknown"),
                                         ("no_id", "without a message ID")])
def test_a_send_without_a_clear_answer_is_locked_with_no_retry(tmp_path, outcome, why):
    sender, store, inbox, clock, email = setup(tmp_path, outcome)
    r = sender.send("reply:1", email)
    assert r.state == "locked" and why in r.detail and r.called
    assert len(inbox.sent) == 1 and inbox.reads == [] and clock.sleeps == []


def test_a_readback_with_a_different_body_is_not_a_confirmation(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "wrong_body")
    r = sender.send("reply:1", email)
    assert r.state == "locked" and "body differs" in r.detail and len(inbox.sent) == 1


def test_the_same_key_is_never_sent_twice(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "ok", "ok")
    sender.send("reply:1", email)
    again = sender.send("reply:1", {**email, "body": "A different text"})
    assert (again.state, again.called) == ("confirmed", False)
    assert len(inbox.sent) == 1


def test_the_same_text_to_the_same_address_is_refused_under_any_key(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "ok", "ok")
    sender.send("reply:1", email)
    other = sender.send("followup:9", {**email, "body": email["body"].replace("\n\n", "\n")})  # same words
    assert (other.state, other.called) == ("refused", False)
    assert len(inbox.sent) == 1


def test_a_locked_send_stays_locked_across_a_restart(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "timeout", "ok")
    sender.send("reply:1", email)
    sender, store = restarted(store, inbox, clock)
    assert sender.recover() == []  # locked is not in flight: nothing to settle
    r = sender.send("reply:1", email)
    assert (r.state, r.called) == ("locked", False)
    assert len(inbox.sent) == 1


def test_restart_mid_send_finds_the_message_in_the_thread_and_never_resends(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "crash")
    with pytest.raises(Crash):  # the provider took it, then the process died before saving the ID
        sender.send("reply:1", email)
    assert store.send_by_key("reply:1")["state"] == "intent"
    sender, store = restarted(store, inbox, clock)
    [r] = sender.recover()
    assert r.state == "confirmed" and not r.called
    assert len(inbox.sent) == 1


def test_restart_mid_send_with_nothing_in_the_thread_locks(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path)
    store.create_intent("reply:1", None, email)  # the process died after saving the intent
    sender, store = restarted(store, inbox, clock)
    [r] = sender.recover()
    assert r.state == "locked" and "outcome is unknown" in r.detail
    assert clock.sleeps == [3, 7, 10]  # the thread was searched on the same schedule
    assert inbox.sent == []


def test_restart_mid_send_locks_when_the_thread_cannot_be_read(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path)
    store.create_intent("reply:1", None, email)
    inbox.thread_error = HttpError("Instantly read thread returned HTTP 503", 503)
    sender, store = restarted(store, inbox, clock)
    [r] = sender.recover()
    assert r.state == "locked" and "could not be read" in r.detail and inbox.sent == []


def test_restart_after_acceptance_reads_back_by_id(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path)
    send_id = store.create_intent("reply:1", None, email)
    pid = inbox.send_reply(reply_to_id=email["reply_to_id"], sender=OURS, subject=email["subject"],
                           body=email["body"])["id"]
    store.move_send(send_id, "accepted", ("intent",), "accepted", provider_id=pid)
    sender, store = restarted(store, inbox, clock)
    [r] = sender.recover()
    assert r.state == "confirmed" and inbox.reads == [pid]
    assert len(inbox.sent) == 1  # the one call made before the restart


def test_recheck_confirms_a_locked_send_by_looking_it_up_and_never_sends(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "timeout")
    r = sender.send("reply:1", email)
    again = sender.recheck(r.send_id)
    assert again.state == "confirmed" and store.send(r.send_id)["provider_id"]
    assert len(inbox.sent) == 1


def test_recheck_leaves_an_unconfirmed_send_locked(tmp_path):
    sender, store, inbox, clock, email = setup(tmp_path, "never")
    r = sender.send("reply:1", email)
    assert sender.recheck(r.send_id).state == "locked"
    assert sender.recheck(999).state == "missing"
    assert len(inbox.sent) == 1


def test_readback_matching_ignores_markup_but_not_words():
    email = {"sender": OURS, "recipient": LEAD, "subject": "Re: Hello", "body": "Hi Pat,\n\nSee you & thanks."}
    html = "<head><title>x</title></head>Hi Pat,<br/><br/>See you &amp; thanks."
    msg = {"id": "m1", "from_address_email": OURS.upper(), "to_address_email_list": "Pat <%s>" % LEAD,
           "subject": "Re: Hello", "body": {"text": "Hi Pat,\nSee you & thanks.", "html": html}}
    assert mismatch(msg, email, "m1") == ""
    two = {**msg, "to_address_email_list": "%s, other@example.com" % LEAD}
    assert mismatch(two, email, "m1") == "recipient differs"
    assert mismatch(msg, email, "m2") == "a different message came back"
    assert normalize("<p>a</p><p>b</p>") == "a b"
    assert addresses([{"address": "X@example.com"}]) == {"x@example.com"}
