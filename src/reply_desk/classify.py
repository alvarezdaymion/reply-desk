"""Sort a reply into interested, question, not_now or unsubscribe with plain, readable rules.

Rules run in that order of strictness: unsubscribe first (it always wins, the optional model never
overrides it), then not_now (so "not interested" never reads as "interested"), then interested,
then question. A reply with no clear signal is a question, so a person looks at it. Only the newly
written part of the email is read; quoted history below "On ... wrote:" is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LABELS = ("interested", "question", "not_now", "unsubscribe")


@dataclass(frozen=True)
class Verdict:
    label: str
    reason: str
    source: str = "rules"
    draft: str | None = None  # only the optional model proposes a draft here


def _rx(*patterns: str) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I) for p in patterns]


RULES: list[tuple[str, list[re.Pattern[str]]]] = [
    ("unsubscribe", _rx(
        r"\bunsubscribe\b", r"\bremove me\b", r"\btake me off\b", r"\bopt(?:[ -])?out\b",
        r"\bstop (?:emailing|contacting|sending|messaging)\b", r"\b(?:do not|don'?t) (?:contact|email)\b",
        r"\bno more emails\b", r"\bremove (?:this|my) (?:email|address)\b", r"\boff your (?:mailing )?list\b",
    )),
    ("not_now", _rx(
        r"\bnot interested\b", r"\bno,? thanks?\b", r"\bno thank you\b", r"\bnot (?:right )?now\b",
        r"\bnot a good time\b", r"\bnot at (?:this|the moment)\b", r"\bmaybe later\b",
        r"\b(?:check|circle) back\b", r"\breach (?:back )?out (?:again |later|next|in|after)",
        r"\b(?:too|really|very|super|pretty) busy\b", r"\bswamped\b", r"\bnext (?:quarter|year)\b",
        r"\bafter the (?:holidays|new year)\b", r"\b(?:we'?re|we are|i'?m|i am) all set\b",
        r"\balready (?:have|work with|use)\b", r"\bnot (?:a )?(?:good )?fit\b", r"\bnot for us\b",
        r"\bno need\b", r"\bnot looking\b",
    )),
    ("interested", _rx(
        r"\binterested\b", r"\bsounds (?:good|great|interesting)\b", r"\btell me more\b",
        r"\bsend (?:me |over )?(?:more|details|info|information)\b",
        r"\b(?:let'?s|happy to|love to|glad to) (?:talk|chat|connect|meet|hop on)\b",
        r"\b(?:set up|book|schedule) (?:a )?(?:call|time|meeting|chat)\b", r"\bnext steps?\b",
        r"\b(?:i'?ll|i will|will) (?:take a look|look at|look it over|review|check it out|check out)\b",
        r"^\s*(?:yes|yeah|sure)\b",
    )),
]

_QUOTED = re.compile(r"^\s*On\b[^\n]{0,300}?\bwrote:|^\s*>|^-{2,}\s*Original Message|^\s*From:\s|^_{8,}",
                     re.I | re.M)
_URL = re.compile(r"https?://\S+", re.I)
_QUESTION_START = re.compile(r"(?:^|[.!\n]\s*)(?:how|what|when|where|why|who|which|can|could|do|does|is|are|"
                             r"will|would)\b[^.!\n]*$", re.I | re.M)


def authored_text(text: str) -> str:
    """The newly written part of an email, without the quoted history below it."""
    text = text or ""
    m = _QUOTED.search(text)
    return (text[:m.start()] if m else text).strip()


def classify(text: str) -> Verdict:
    body = authored_text(text)
    for label, patterns in RULES:
        for rx in patterns:
            m = rx.search(body)
            if m:
                return Verdict(label, 'matched "%s"' % m.group(0).strip())
    plain = _URL.sub(" ", body)
    if "?" in plain or _QUESTION_START.search(plain):
        return Verdict("question", "asks a question")
    return Verdict("question", "no clear signal; needs a person to read it")
