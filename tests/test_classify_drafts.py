import datetime as dt

import pytest
from conftest import make_cfg

from reply_desk import drafts
from reply_desk.classify import authored_text, classify
from reply_desk.timing import parse_when


@pytest.mark.parametrize("text,label", [
    ("Sounds interesting, tell me more.", "interested"),
    ("Yes please, send over the details", "interested"),
    ("Happy to chat next week.", "interested"),
    ("How much does it cost?", "question"),
    ("Do you work with businesses outside the US", "question"),
    ("Thanks.", "question"),  # no clear signal: a person reads it
    ("Not interested, thanks.", "not_now"),
    ("No thanks, we already work with a bookkeeper.", "not_now"),
    ("We're swamped until after the holidays.", "not_now"),
    ("Please remove me from your list.", "unsubscribe"),
    ("Not interested. Unsubscribe me.", "unsubscribe"),  # unsubscribe always wins
    ("STOP EMAILING ME", "unsubscribe"),
])
def test_labels(text, label):
    assert classify(text).label == label


@pytest.mark.parametrize("text,label,reason", [
    ("Interested, but please don’t email about the cost.", "unsubscribe", "don't email"),
    ("Please donʼt contact me.", "unsubscribe", "don't contact"),
    ("Please don‘t contact me.", "unsubscribe", "don't contact"),
    ("“ＤＯＮ＇Ｔ ＥＭＡＩＬ” me about the cost.", "unsubscribe", "don't email"),
    ("ＵＮＳＵＢＳＣＲＩＢＥ, thanks.", "unsubscribe", "unsubscribe"),
    ("I’M ALL SET. Tell me more?", "not_now", "i'm all set"),
    ("ＷＥ＇ＲＥ　ＡＬＬ　ＳＥＴ", "not_now", "we're all set"),
    ("LET’S TALK", "interested", "let's talk"),
    ("I’ll review it.", "interested", "i'll review"),
    ("ＳＯＵＮＤＳ　ＧＯＯＤ", "interested", "sounds good"),
])
def test_all_label_rules_use_normalized_unicode(text, label, reason):
    result = classify(text)
    assert result.label == label and result.reason == 'matched "%s"' % reason


@pytest.mark.parametrize("text", ["ＨＯＷ ＭＵＣＨ", "A price list？"])
def test_question_detection_uses_normalized_unicode(text):
    assert classify(text).reason == "asks a question"


def test_unicode_quoted_history_is_excluded_before_label_rules():
    text = "Thanks.\n\nＯｎ Tuesday a sender ｗｒｏｔｅ：\nPlease unsubscribe me."
    assert classify(text).reason == "no clear signal; needs a person to read it"


def test_quoted_history_is_not_read():
    text = "Thanks.\n\nOn Tue, Oct 20, 2026 at 9:00 AM Sam <sam@example.org> wrote:\n> Interested? Tell me more!"
    assert authored_text(text) == "Thanks."
    assert classify(text).label == "question"


def test_the_reason_says_what_matched():
    assert classify("Sounds great!").reason == 'matched "sounds great"'


def test_fill_leaves_out_lines_with_empty_placeholders():
    text = drafts.fill("Hi {first_name},\n\nPage: {page_url}\n\nBook: {booking_url}\n\n{signature}",
                       {"first_name": "Pat", "page_url": "", "booking_url": "https://example.com/b",
                        "signature": "Sam"})
    assert text == "Hi Pat,\n\nBook: https://example.com/b\n\nSam"


@pytest.mark.parametrize("text,ok", [
    ("Hi Pat,\n\nThanks.\n\nSam", True),
    ("Hi Pat,\n\n[[ write your answer here ]]\n\nSam", False),
    ("Hi {first_name}", False),
    ("", False),
    ("x" * 5001, False),
])
def test_draft_check(text, ok):
    assert (drafts.check(text) is None) == ok


def test_rule_drafts(tmp_path):
    cfg = make_cfg(tmp_path)
    reply = {"first_name": "Jordan", "company": "Sample Street Cafe", "lead_email": "jordan@example.com",
             "page_url": ""}
    assert "A flat monthly fee." in drafts.reply_draft(cfg, reply, "question", None, "How much does it cost?")
    assert drafts.ANSWER_MARKER in drafts.reply_draft(cfg, reply, "question", None, "Are you local?")
    hint = parse_when("after the holidays", dt.datetime(2026, 10, 21, tzinfo=dt.UTC))
    assert "I'll check back on Monday 4 January." in drafts.reply_draft(cfg, reply, "not_now", hint, "")
    assert "I won't follow up" in drafts.reply_draft(cfg, reply, "not_now", None, "")
    assert drafts.reply_draft(cfg, reply, "unsubscribe", None, "") is None
    nameless = drafts.reply_draft(cfg, {**reply, "first_name": ""}, "interested", None, "")
    assert nameless.startswith("Hi there,") and "pick a time here: https://example.com/book" in nameless
    assert "overview" not in nameless  # no page, so the line that would link it is left out


def test_follow_up_drafts_by_step(tmp_path):
    cfg = make_cfg(tmp_path)
    reply = {"first_name": "Pat", "company": "Acme", "lead_email": "pat@example.com",
             "page_url": "https://pages.example.com/acme-1/"}
    first = drafts.followup_draft(cfg, reply, 1, 3, "weekend")
    assert "Hope the weekend went well" in first and reply["page_url"] in first
    assert "One more nudge" in drafts.followup_draft(cfg, reply, 2, 3, "")
    assert "Last note from me" in drafts.followup_draft(cfg, reply, 3, 3, "")
