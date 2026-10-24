"""The text of an approval card, in Telegram's HTML subset, and a plain-text version for the console."""

from __future__ import annotations

import html
import re
from typing import Any

from .classify import authored_text

TITLES = {"interested": "Interested", "question": "Question", "not_now": "Not now", "unsubscribe": "Unsubscribe"}
BUTTONS = (("Send", "send"), ("Edit", "edit"), ("Skip", "skip"))


def buttons(card_id: str) -> list[tuple[str, str]]:
    return [(label, "rd:%s:%s" % (action, card_id)) for label, action in BUTTONS]


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def card_text(card: dict[str, Any], reply: dict[str, Any], followup: dict[str, Any] | None = None,
              total_steps: int = 0, status: str = "") -> str:
    e = html.escape
    who = ", ".join(x for x in (reply.get("first_name"), reply.get("company")) if x) or reply["lead_email"]
    if card["kind"] == "followup" and followup:
        title, why = "Follow-up %d of %d" % (followup["step"], total_steps), followup["why"]
    else:
        title, why = TITLES.get(reply.get("label") or "", reply.get("label") or "Reply"), reply.get("reason") or ""
    lines = ["<b>%s</b> - %s" % (e(title), e(who)),
             "%s wrote to %s" % (e(reply["lead_email"]), e(reply["inbox"])),
             e(reply.get("subject") or "(no subject)"),
             "Why: %s" % e(why), ""]
    if card["kind"] == "reply":
        lines += ["<b>They wrote</b>", e(clip(authored_text(reply.get("body") or ""), 700)), ""]
    lines += ["<b>Draft</b>", e(clip(card["draft"], 2500))]
    if reply.get("page_url") and reply["page_url"] in card["draft"]:
        lines += ["", "Page (checked live): %s" % e(reply["page_url"])]
    if status:
        lines += ["", "<b>%s</b>" % e(status)]
    return "\n".join(lines)


def plain(text: str) -> str:
    return html.unescape(re.sub(r"</?[a-z]+>", "", text))
