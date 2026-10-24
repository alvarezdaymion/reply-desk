"""Load reply-desk.toml and fill in defaults. Credentials never live here: they come from the
environment (see .env.example)."""

from __future__ import annotations

import copy
import datetime as dt
import re
import string
import tomllib
import zoneinfo
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLACEHOLDERS = {"first_name", "company", "business", "signature", "booking_url", "page_url", "answer",
                "when", "opener"}
PAGE_PLACEHOLDERS = {"first_name", "company", "business"}

# A line whose placeholder has no value is left out (see drafts.fill).
DEFAULT_DRAFTS = {
    "interested": (
        "Hi {first_name},\n\n"
        "Thanks for getting back to me.\n\n"
        "I put together a one-page overview for {company}: {page_url}\n\n"
        "If you'd like to talk it through, you can pick a time here: {booking_url}\n\n"
        "{signature}"
    ),
    "question": "Hi {first_name},\n\n{answer}\n\n{signature}",
    "not_now_later": "Hi {first_name},\n\nThanks, understood. I'll check back {when}.\n\n{signature}",
    "not_now_close": (
        "Hi {first_name},\n\nThanks for letting me know. I won't follow up. "
        "If anything changes, just reply here.\n\n{signature}"
    ),
    "followup": (
        "Hi {first_name},\n\n"
        "{opener}\n\n"
        "The overview for {company} is here: {page_url}\n\n"
        "{signature}"
    ),
    "followup_middle": (
        "Hi {first_name},\n\n"
        "One more nudge on this. If it helps, you can pick a time here: {booking_url}\n\n"
        "{signature}"
    ),
    "followup_last": (
        "Hi {first_name},\n\n"
        "Last note from me. If the timing is ever right, just reply here.\n\n"
        "{signature}"
    ),
}

DEFAULTS: dict[str, Any] = {
    "business": {
        "name": "Your business",
        "signature": "Your name\nYour business",
        "booking_url": "",
        "facts": "",
    },
    "sending": {
        "mode": "dry-run",
        "readback_seconds": [3, 10, 20],
    },
    "inbox": {
        "campaign_ids": [],
        "lookback_hours": 24,
        "poll_seconds": 60,
        "blocklist_unsubscribes": False,
    },
    "telegram": {"allowed_user_ids": []},
    "model": {
        "enabled": False,
        "model": "claude-opus-5-5",
        "effort": "low",
        "fallbacks": True,
        "max_tokens": 4000,
    },
    "pages": {
        "enabled": True,
        "base_url": "https://pages.example.com",
        "publisher": "local",
        "command": [],
        "command_timeout_seconds": 120,
        "check_attempts": 5,
        "headline": "Prepared for {first_name} at {company}",
        "paragraphs": [
            "Thanks for replying. This page sums up how {business} could help {company}.",
        ],
        "points": [],
        "cta_label": "Pick a time to talk",
    },
    "followups": {
        "gaps_days": [2, 4, 7],
        "send_hour": 9,
        "timezone": "UTC",
        "max_days_out": 90,
    },
    "answers": [],
    "drafts": DEFAULT_DRAFTS,
    "paths": {"state": "state", "site": "site"},
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Config:
    data: dict[str, Any]
    root: Path

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def state_dir(self) -> Path:
        return (self.root / self.data["paths"]["state"]).resolve()

    @property
    def site_dir(self) -> Path:
        return (self.root / self.data["paths"]["site"]).resolve()

    @property
    def db_path(self) -> Path:
        return self.state_dir / "reply-desk.db"

    @property
    def live(self) -> bool:
        return self.data["sending"]["mode"] == "live"

    def tz(self) -> dt.tzinfo:
        return tzinfo_for(self.data["followups"]["timezone"])


def tzinfo_for(name: str) -> dt.tzinfo:
    """Use UTC or an explicit IANA name so future dates retain daylight-saving rules."""
    if not name:
        raise ValueError("a time zone is required")
    if name.upper() == "UTC":
        return dt.UTC
    return zoneinfo.ZoneInfo(name)


def template_fields(template: str) -> set[str]:
    fields = set()
    for _, field, spec, conversion in string.Formatter().parse(template):
        if field is not None:
            if spec or conversion:
                raise ValueError("template placeholders cannot use format specifications or conversions")
            fields.add(field)
    return fields


def validate(data: dict[str, Any]) -> None:
    if data["sending"]["mode"] not in ("dry-run", "live"):
        raise ValueError("sending.mode must be dry-run or live")
    rb = data["sending"]["readback_seconds"]
    if not rb or any(float(b) <= float(a) for a, b in zip([0, *rb], rb, strict=False)):
        raise ValueError("sending.readback_seconds must be a non-empty list of increasing positive numbers")
    pages = data["pages"]
    if pages["publisher"] not in ("local", "command"):
        raise ValueError("pages.publisher must be local or command")
    if pages["publisher"] == "command" and not (pages["command"] and all(isinstance(a, str) for a in pages["command"])):
        raise ValueError("pages.command must be a list of strings when pages.publisher = \"command\"")
    if pages["enabled"] and not re.match(r"https?://[^/\s]+", pages["base_url"]):
        raise ValueError("pages.base_url must start with http:// or https://")
    fu = data["followups"]
    if any(int(g) < 1 for g in fu["gaps_days"]):
        raise ValueError("followups.gaps_days must be whole days of 1 or more")
    if not 0 <= int(fu["send_hour"]) <= 23:
        raise ValueError("followups.send_hour must be 0-23")
    try:
        tzinfo_for(fu["timezone"])
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError("followups.timezone %r is not a known time zone" % fu["timezone"]) from e
    for name, template in data["drafts"].items():
        unknown = template_fields(template) - PLACEHOLDERS
        if unknown:
            raise ValueError("drafts.%s uses unknown placeholders: %s" % (name, ", ".join(sorted(unknown))))
    missing = set(DEFAULT_DRAFTS) - set(data["drafts"])
    if missing:
        raise ValueError("drafts is missing: %s" % ", ".join(sorted(missing)))
    for text in [pages["headline"], pages["cta_label"], *pages["paragraphs"], *pages["points"]]:
        unknown = template_fields(text) - PAGE_PLACEHOLDERS
        if unknown:
            raise ValueError("pages text uses unknown placeholders: %s" % ", ".join(sorted(unknown)))
    for i, a in enumerate(data["answers"]):
        if not (isinstance(a.get("keywords"), list) and a["keywords"] and isinstance(a.get("text"), str)):
            raise ValueError("answers[%d] needs keywords (a list) and text" % i)
        if template_fields(a["text"]):
            raise ValueError("answers[%d].text must not contain placeholders" % i)
    if data["model"]["enabled"] and data["model"]["effort"] not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("model.effort must be low, medium, high, xhigh or max")


def from_dict(data: dict[str, Any], root: Path | str = ".") -> Config:
    merged = _merge(DEFAULTS, data)
    validate(merged)
    return Config(merged, Path(root).resolve())


def load(path: Path | str) -> Config:
    p = Path(path)
    with p.open("rb") as f:
        data = tomllib.load(f)
    return from_dict(data, p.parent)
