"""Draft text from the templates in [drafts], and the checks every draft passes before it can send.

Templates use {placeholders}. A line whose placeholder has no value is left out, so an empty
booking_url drops the "pick a time" line instead of sending a broken sentence.

The rules never invent an answer: a question with no matching [[answers]] entry gets the marker
below, and a draft that still contains it (or an unfilled {placeholder}) is refused at send time
until someone taps Edit.
"""

from __future__ import annotations

import re
from typing import Any

from .config import template_fields
from .timing import Hint, human_day

ANSWER_MARKER = "[[ write your answer here ]]"
MAX_CHARS = 2500

OPENERS = {
    "weekend": "Hope the weekend went well. Did you get a chance to look at this?",
    "holidays": "Hope you had a good break. Checking back in, as you suggested.",
    "other": "Checking back in, as you suggested. Did you get a chance to look at this?",
    "": "Just checking this reached you. Happy to answer any questions.",
}

_MARKER = re.compile(r"\[\[.*?\]\]", re.S)
_UNFILLED = re.compile(r"\{[A-Za-z_]\w*(?:[^{}]*)\}")


def fill(template: str, values: dict[str, Any]) -> str:
    lines = []
    for line in template.split("\n"):
        fields = template_fields(line)
        if any(not str(values.get(f) or "").strip() for f in fields):
            continue
        lines.append(line.format(**{f: str(values[f]).strip() for f in fields}))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def check(text: str | None) -> str | None:
    """Why this draft must not be sent yet, or None if it may."""
    if not text or not text.strip():
        return "the draft is empty"
    if len(text) > MAX_CHARS:
        return "the draft is over %d characters" % MAX_CHARS
    if _MARKER.search(text):
        return "the draft still has a [[ ... ]] part to write; tap Edit first"
    m = _UNFILLED.search(text)
    if m:
        return "the draft has an unfilled %s" % m.group(0)
    return None


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else email


def values_for(cfg: Any, reply: dict[str, Any]) -> dict[str, str]:
    biz = cfg["business"]
    return {
        "first_name": reply.get("first_name") or "there",
        "company": reply.get("company") or domain_of(reply.get("lead_email", "")),
        "business": biz["name"],
        "signature": biz["signature"],
        "booking_url": biz["booking_url"],
        "page_url": reply.get("page_url") or "",
    }


def canned_answer(text: str, answers: list[dict[str, Any]]) -> str | None:
    for a in answers:
        for k in a["keywords"]:
            if re.search(r"\b%s\b" % re.escape(k), text, re.I):
                return a["text"]
    return None


def reply_draft(cfg: Any, reply: dict[str, Any], label: str, hint: Hint | None, text: str) -> str | None:
    """The rule-based draft for a classified reply. None for unsubscribe: nothing is drafted."""
    drafts, values = cfg["drafts"], values_for(cfg, reply)
    if label == "interested":
        return fill(drafts["interested"], values)
    if label == "question":
        return fill(drafts["question"], {**values, "answer": canned_answer(text, cfg["answers"]) or ANSWER_MARKER})
    if label == "not_now":
        if hint is None:
            return fill(drafts["not_now_close"], values)
        return fill(drafts["not_now_later"], {**values, "when": "on " + human_day(hint.due)})
    return None


def followup_draft(cfg: Any, reply: dict[str, Any], step: int, total: int, hint_kind: str) -> str:
    drafts, values = cfg["drafts"], values_for(cfg, reply)
    if step == 1:
        return fill(drafts["followup"], {**values, "opener": OPENERS.get(hint_kind, OPENERS[""])})
    if step >= total:
        return fill(drafts["followup_last"], values)
    return fill(drafts["followup_middle"], values)
