"""The optional model drafter against a fake client: checked answers, rules on anything odd."""

import json
from types import SimpleNamespace

import pytest
from conftest import make_cfg

from reply_desk.classify import Verdict, classify
from reply_desk.llm import ModelDrafter

REPLY = {"first_name": "Pat", "company": "Acme", "subject": "Re: Hi", "body": "Tell me more",
         "lead_email": "pat@example.com"}
GOOD = {"label": "interested", "reason": "asks for more",
        "draft": "Hi Pat,\n\nHere is the overview: {page_url}\n\n{signature}"}


class FakeMessages:
    def __init__(self, answer, stop="end_turn"):
        self.answer, self.stop, self.calls = answer, stop, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return SimpleNamespace(stop_reason=self.stop, content=[SimpleNamespace(type="text", text=text)])


def drafter(tmp_path, answer, stop="end_turn", fallbacks=False):
    messages = FakeMessages(answer, stop)
    client = SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))
    return ModelDrafter(make_cfg(tmp_path, model={"enabled": True, "fallbacks": fallbacks}), client), messages


def test_a_good_answer_is_used(tmp_path):
    d, messages = drafter(tmp_path, GOOD)
    v = d.classify(REPLY, classify(REPLY["body"]))
    assert v == Verdict("interested", "asks for more", "model", GOOD["draft"])
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["output_config"]["format"]["type"] == "json_schema"
    assert "<email>" in call["messages"][0]["content"] and "Tell me more" in call["messages"][0]["content"]


def test_fallbacks_use_the_beta_header(tmp_path):
    d, messages = drafter(tmp_path, GOOD, fallbacks=True)
    d.classify(REPLY, classify(REPLY["body"]))
    assert messages.calls[0]["betas"] == ["server-side-fallback-2026-07-01"]
    assert messages.calls[0]["fallbacks"] == "default"


@pytest.mark.parametrize("answer,stop", [
    ({"label": "maybe", "reason": "x", "draft": ""}, "end_turn"),
    ("not json", "end_turn"),
    (GOOD, "refusal"),
    (RuntimeError("overloaded"), "end_turn"),
])
def test_anything_odd_falls_back_to_the_rules(tmp_path, answer, stop):
    d, _ = drafter(tmp_path, answer, stop)
    rules = classify(REPLY["body"])
    v = d.classify(REPLY, rules)
    assert (v.label, v.draft) == (rules.label, None) and v.source.startswith("rules")


@pytest.mark.parametrize("draft,why", [
    ("Hi, see https://example.com/x\n\n{signature}", "link"),
    ("Hi {first_name}\n\n{signature}", "unknown placeholders"),
    ("Hi Pat, here: {page_url}", "no {signature}"),
    ("Hi Pat, thanks!\n\n{signature}", "no {page_url}"),
])
def test_a_bad_draft_keeps_the_label_but_not_the_draft(tmp_path, draft, why):
    d, _ = drafter(tmp_path, {**GOOD, "draft": draft})
    v = d.classify(REPLY, classify(REPLY["body"]))
    assert v.label == "interested" and v.draft is None and why in v.source


def test_the_model_is_never_asked_about_an_unsubscribe(tmp_path):
    d, messages = drafter(tmp_path, GOOD)
    rules = classify("Unsubscribe me")
    assert d.classify(REPLY, rules) is rules and messages.calls == []


def test_follow_up_drafts_are_checked_too(tmp_path):
    good, _ = drafter(tmp_path, {"draft": "Hi Pat, any thoughts?\n\n{signature}"})
    assert good.followup(REPLY, [], 1, 3, "they wrote \"this weekend\"") == "Hi Pat, any thoughts?\n\n{signature}"
    bad, _ = drafter(tmp_path, {"draft": "Hi Pat, any thoughts? www.example.com\n\n{signature}"})
    assert bad.followup(REPLY, [], 1, 3, "two days") is None


@pytest.mark.parametrize("token", ["{signature:invalid}", "{signature!r}", "{signature:{page_url}}"])
def test_model_formatting_is_rejected_before_rendering(tmp_path, token):
    d, _ = drafter(tmp_path, {**GOOD, "draft": "Overview: {page_url}\n" + token})
    verdict = d.classify(REPLY, classify(REPLY["body"]))
    assert verdict.draft is None
    d, _ = drafter(tmp_path, {"draft": "Checking in.\n" + token})
    assert d.followup(REPLY, [], 1, 3, "due") is None


def test_model_tokens_must_use_the_exact_supported_spelling(tmp_path):
    d, _ = drafter(tmp_path, {**GOOD, "draft": "Overview: {page_url:}\n{signature}"})
    assert d.classify(REPLY, classify(REPLY["body"])).draft is None


def test_model_client_has_no_automatic_request_retries(tmp_path, monkeypatch):
    import sys

    kwargs = []
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda **options: kwargs.append(options)))
    ModelDrafter(make_cfg(tmp_path))
    assert kwargs == [{"max_retries": 0}]


@pytest.mark.parametrize("stage", ["classify", "followup"])
def test_model_rate_limits_defer_work_without_repeating_the_request(tmp_path, stage):
    from reply_desk.http import HttpError

    d, messages = drafter(tmp_path, HttpError("model rate limited", 429, 12))
    with pytest.raises(HttpError) as error:
        if stage == "classify":
            d.classify(REPLY, classify(REPLY["body"]))
        else:
            d.followup(REPLY, [], 1, 3, "due")
    assert error.value.retry_after == 12 and len(messages.calls) == 1


@pytest.mark.parametrize("link", ["{page_url}details", "{page_url}?extra=1", "{page_url}#extra",
                                  "prefix{page_url}", "{page_url}.example.com", "{booking_url}/extra"])
def test_model_link_tokens_cannot_be_extended(tmp_path, link):
    d, _ = drafter(tmp_path, {**GOOD, "draft": "Overview: {page_url}\nDetails: " + link + "\n{signature}"})
    assert d.classify(REPLY, classify(REPLY["body"])).draft is None
