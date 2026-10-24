"""What `reply-desk status` (and the end of the demo) prints: counts, locked sends, planned follow-ups."""

from __future__ import annotations

import datetime as dt

from .sender import parse_ts
from .store import Store
from .timing import human_day


def _counts(d: dict[str, int], order: list[str]) -> str:
    keys = [k for k in order if k in d] + sorted(k for k in d if k not in order)
    return ", ".join("%s %d" % (k.replace("_", " "), d[k]) for k in keys) or "none"


def status_lines(store: Store, tz: dt.tzinfo) -> list[str]:
    c = store.counts()
    lines = [
        "Replies: %d (%s)" % (sum(c["replies"].values()), _counts(c["replies"], ["interested", "question", "not_now",
                                                                                   "unsubscribe", "ignored"])),
        "Cards: %d (%s)" % (sum(c["cards"].values()), _counts(c["cards"], ["pending", "sent", "locked", "skipped"])),
        "Sends: %d (%s). Each is one call to the provider; none is ever sent twice."
        % (sum(c["sends"].values()), _counts(c["sends"], ["confirmed", "locked"])),
    ]
    locked = store.sends("locked")
    if locked:
        lines += ["", "Locked sends. Check the sent folder in your email platform, then run one of:",
                  "  reply-desk resolve <id> --sent       it went out",
                  "  reply-desk resolve <id> --not-sent   it did not (answer by hand if it still needs one)",
                  "  reply-desk resolve --recheck         read every locked send back once more"]
        for s in locked:
            lines.append("  #%d to %s, \"%s\": %s" % (s["id"], s["recipient"], s["subject"], s["note"] or ""))
    planned = store.followups("scheduled")
    if planned:
        lines += ["", "Follow-ups planned:"]
        for f in planned:
            reply = store.reply(f["reply_id"]) or {}
            due = (parse_ts(f["due_at"]) or dt.datetime.now(dt.UTC)).astimezone(tz)
            lines.append("  %-22s step %d  %s %s  %s" % (reply.get("lead_email", "?"), f["step"], human_day(due),
                                                         due.strftime("%H:%M"), f["why"]))
    return lines
