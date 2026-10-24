"""Durable local state: one SQLite file, standard library only.

Tables: replies (inbound), cards (approval cards), sends (the send ledger), followups, suppressed,
audit (append-only log) and kv (poll cursor, Telegram update offset).

Every state change is a conditional UPDATE ("... WHERE state = <expected>") inside an IMMEDIATE
transaction. If a second tap, a second process or a restart tries to move a row that already
moved, the update matches nothing and the caller is told so. Two constraints back the send
ledger: `sends.intent_key` is UNIQUE (one row per card or follow-up step, ever) and so is
`(recipient, body_hash)` (the same text never goes to the same address twice).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY,
    provider_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL DEFAULT '',
    lead_email TEXT NOT NULL,
    inbox TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    body_complete INTEGER NOT NULL DEFAULT 1,
    received_at TEXT NOT NULL,
    auto_reply INTEGER NOT NULL DEFAULT 0,
    first_name TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    label TEXT,
    reason TEXT,
    draft TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'waiting_page', 'carded', 'suppressed', 'ignored', 'failed')),
    page_url TEXT,
    page_marker TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    reply_id INTEGER NOT NULL REFERENCES replies(id),
    kind TEXT NOT NULL CHECK (kind IN ('reply', 'followup')),
    followup_id INTEGER,
    draft TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'sending', 'sent', 'locked', 'skipped', 'stale', 'dry_run', 'closed')),
    message_id TEXT,
    post_state TEXT NOT NULL DEFAULT 'unposted' CHECK (post_state IN ('unposted', 'posting', 'posted', 'locked')),
    reviewed_draft TEXT,
    edit_prompt_id TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY,
    intent_key TEXT NOT NULL UNIQUE,
    card_id TEXT REFERENCES cards(id),
    reply_to_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'intent'
        CHECK (state IN ('intent', 'accepted', 'confirmed', 'locked', 'resolved_sent', 'resolved_not_sent')),
    provider_id TEXT,
    readbacks INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (recipient, body_hash)
);
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY,
    reply_id INTEGER NOT NULL REFERENCES replies(id),
    step INTEGER NOT NULL,
    due_at TEXT NOT NULL,
    why TEXT NOT NULL,
    hint TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'scheduled' CHECK (state IN ('scheduled', 'carded', 'done', 'cancelled')),
    card_id TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (reply_id, step)
);
CREATE TABLE IF NOT EXISTS suppressed (email TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def iso(t: dt.datetime) -> str:
    """UTC, second precision: these strings sort in time order."""
    return t.astimezone(dt.UTC).isoformat(timespec="seconds")


def zulu(t: dt.datetime) -> str:
    """UTC with a Z, the form the Instantly API uses."""
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def body_hash(body: str) -> str:
    return hashlib.sha256(" ".join(body.split()).encode()).hexdigest()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Store:
    def __init__(self, path: Path | str, clock: Callable[[], dt.datetime] = _utcnow):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        has_tables = self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
        if version not in (0, SCHEMA_VERSION) or (version == 0 and has_tables):
            self.db.close()
            raise RuntimeError("unsupported state schema %d; automatic migration is not supported" % version)
        if version == 0:
            self.db.executescript("BEGIN IMMEDIATE;\n%s\nPRAGMA user_version = %d;\nCOMMIT;"
                                  % (SCHEMA, SCHEMA_VERSION))

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def now(self) -> str:
        return iso(self.clock())

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _move(self, c: sqlite3.Connection, table: str, key: Any, to: str, from_: tuple[str, ...],
              at: str | None = None, **fields: Any) -> bool:
        """The one way rows change state: only from an expected state, inside the caller's transaction."""
        sets = ", ".join(["state = ?", "updated_at = ?"] + ["%s = ?" % k for k in fields])
        marks = ", ".join("?" for _ in from_)
        cur = c.execute("UPDATE %s SET %s WHERE id = ? AND state IN (%s)" % (table, sets, marks),
                        (to, at or self.now(), *fields.values(), key, *from_))
        return cur.rowcount == 1

    # ---- audit and key/value -------------------------------------------------------------------

    def log(self, kind: str, ref: Any = "", detail: str = "", c: sqlite3.Connection | None = None) -> None:
        (c or self.db).execute("INSERT INTO audit (at, kind, ref, detail) VALUES (?, ?, ?, ?)",
                               (self.now(), kind, str(ref), detail[:500]))

    def audit(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            return self._all("SELECT * FROM audit WHERE kind = ? ORDER BY id", (kind,))
        return self._all("SELECT * FROM audit ORDER BY id")

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def put(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv (key, value) VALUES (?, ?)"
                        " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def claim_edit_callback(self, callback_id: str) -> bool:
        cur = self.db.execute("INSERT OR IGNORE INTO kv (key, value) VALUES (?, 'claimed')",
                              ("telegram.edit:" + callback_id,))
        return cur.rowcount == 1

    # ---- replies -------------------------------------------------------------------------------

    def add_reply(self, r: dict[str, Any]) -> int | None:
        """Store an inbound reply once. None if this provider message is already stored."""
        now = self.now()
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO replies (provider_id, thread_id, lead_email, inbox, subject, body, received_at,"
                " body_complete, auto_reply, first_name, company, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["provider_id"], r.get("thread_id", ""), r["lead_email"].lower(), r["inbox"], r.get("subject", ""),
                 r.get("body", ""), r["received_at"], int(not r.get("truncated")),
                 int(bool(r.get("auto_reply"))), r.get("first_name", ""),
                 r.get("company", ""), now, now))
            if cur.rowcount != 1:
                return None
            self.log("reply.stored", cur.lastrowid, r["lead_email"].lower(), c)
            return int(cur.lastrowid)

    def reply(self, reply_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM replies WHERE id = ?", (reply_id,))

    def has_reply(self, provider_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM replies WHERE provider_id = ?", (provider_id,)) is not None

    def replies(self, *statuses: str) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in statuses)
        return self._all("SELECT * FROM replies WHERE status IN (%s) ORDER BY received_at, id" % marks, statuses)

    def update_reply(self, reply_id: int, from_: tuple[str, ...] = ("new", "waiting_page"), **fields: Any) -> bool:
        sets = ", ".join(["updated_at = ?"] + ["%s = ?" % k for k in fields])
        marks = ", ".join("?" for _ in from_)
        cur = self.db.execute("UPDATE replies SET %s WHERE id = ? AND status IN (%s)" % (sets, marks),
                              (self.now(), *fields.values(), reply_id, *from_))
        return cur.rowcount == 1

    # ---- cards ---------------------------------------------------------------------------------

    def create_card(self, reply_id: int, kind: str, draft: str, followup_id: int | None = None) -> dict[str, Any]:
        card_id = secrets.token_hex(6)
        now = self.now()
        with self.tx() as c:
            c.execute("INSERT INTO cards (id, reply_id, kind, followup_id, draft, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,?)", (card_id, reply_id, kind, followup_id, draft, now, now))
            if kind == "reply":
                moved = c.execute("UPDATE replies SET status = 'carded', updated_at = ? WHERE id = ?"
                                  " AND status IN ('new', 'waiting_page')", (now, reply_id)).rowcount == 1
            else:
                moved = self._move(c, "followups", followup_id, "carded", ("scheduled",), card_id=card_id)
            if not moved:  # one card per reply and per follow-up step; the transaction rolls back
                raise ValueError("a card for this %s already exists" % kind)
            self.log("card.created", card_id, "%s for reply %d" % (kind, reply_id), c)
        card = self.card(card_id)
        assert card is not None
        return card

    def card(self, card_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM cards WHERE id = ?", (card_id,))

    def cards(self, *states: str) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in states)
        return self._all("SELECT * FROM cards WHERE state IN (%s) ORDER BY created_at, id" % marks, states)

    def card_for_prompt(self, prompt_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM cards WHERE edit_prompt_id = ?", (str(prompt_id),))

    def move_card(self, card_id: str, to: str, from_: tuple[str, ...], note: str = "", *,
                  at: str | None = None) -> bool:
        with self.tx() as c:
            moved = self._move(c, "cards", card_id, to, from_, at=at, note=note)
            if moved:
                self.log("card." + to, card_id, note, c)
            return moved

    def claim_card(self, card_id: str, expected_draft: str | None = None) -> bool:
        """pending -> sending. Exactly one tap wins; every later tap gets False."""
        if expected_draft is not None:
            with self.tx() as c:
                cur = c.execute("UPDATE cards SET state = 'sending', updated_at = ?, note = 'send tapped'"
                                " WHERE id = ? AND state = 'pending' AND draft = ?",
                                (self.now(), card_id, expected_draft))
                if cur.rowcount == 1:
                    self.log("card.sending", card_id, "send tapped", c)
                return cur.rowcount == 1
        return self.move_card(card_id, "sending", ("pending",), "send tapped")

    def set_card(self, card_id: str, **fields: Any) -> None:
        sets = ", ".join(["updated_at = ?"] + ["%s = ?" % k for k in fields])
        self.db.execute("UPDATE cards SET %s WHERE id = ?" % sets, (self.now(), *fields.values(), card_id))

    def claim_card_post(self, card_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE cards SET post_state = 'posting' WHERE id = ? AND state = 'pending'"
                            " AND post_state = 'unposted' AND message_id IS NULL", (card_id,))
            return cur.rowcount == 1

    def finish_card_post(self, card_id: str, message_id: str | None, draft: str, note: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE cards SET post_state = ?, message_id = ?, reviewed_draft = ?, note = ?"
                      " WHERE id = ? AND post_state = 'posting'",
                      ("posted" if message_id else "locked", message_id, draft if message_id else None, note, card_id))

    def mark_card_reviewed(self, card_id: str, draft: str) -> None:
        self.db.execute("UPDATE cards SET reviewed_draft = ? WHERE id = ? AND draft = ?"
                        " AND post_state = 'posted' AND state = 'pending'", (draft, card_id, draft))

    def edit_draft(self, card_id: str, draft: str) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE cards SET draft = ?, updated_at = ? WHERE id = ? AND state = 'pending'",
                            (draft, self.now(), card_id))
            if cur.rowcount == 1:
                self.log("card.edited", card_id, "%d characters" % len(draft), c)
            return cur.rowcount == 1

    # ---- the send ledger -----------------------------------------------------------------------

    def create_intent(self, key: str, card_id: str | None, email: dict[str, str]) -> int | None:
        """Write the exact email before the provider is called. None if the key or the same
        email to the same address is already in the ledger: then nothing may be sent."""
        now = self.now()
        try:
            with self.tx() as c:
                cur = c.execute(
                    "INSERT INTO sends (intent_key, card_id, reply_to_id, thread_id, sender, recipient, subject, body,"
                    " body_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (key, card_id, email["reply_to_id"], email.get("thread_id", ""), email["sender"],
                     email["recipient"].lower(), email["subject"], email["body"], body_hash(email["body"]), now, now))
                self.log("send.intent", cur.lastrowid, key, c)
                return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def send(self, send_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM sends WHERE id = ?", (send_id,))

    def send_by_key(self, key: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM sends WHERE intent_key = ?", (key,))

    def same_email(self, recipient: str, body: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM sends WHERE recipient = ? AND body_hash = ?",
                         (recipient.lower(), body_hash(body)))

    def sends(self, *states: str) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in states)
        return self._all("SELECT * FROM sends WHERE state IN (%s) ORDER BY id" % marks, states)

    def move_send(self, send_id: int, to: str, from_: tuple[str, ...], note: str, **fields: Any) -> bool:
        with self.tx() as c:
            moved = self._move(c, "sends", send_id, to, from_, note=note, **fields)
            if moved:
                self.log("send." + to, send_id, note, c)
            return moved

    def note_readback(self, send_id: int, n: int, result: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sends SET readbacks = ?, note = ?, updated_at = ? WHERE id = ?",
                      (n, result[:300], self.now(), send_id))
            self.log("send.readback", send_id, result, c)

    # ---- follow-ups ----------------------------------------------------------------------------

    def schedule_followup(self, reply_id: int, step: int, due: dt.datetime, why: str, hint: str = "") -> int | None:
        now = self.now()
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO followups (reply_id, step, due_at, why, hint, created_at,"
                            " updated_at) VALUES (?,?,?,?,?,?,?)", (reply_id, step, iso(due), why, hint, now, now))
            if cur.rowcount != 1:
                return None
            self.log("followup.scheduled", cur.lastrowid, "step %d at %s: %s" % (step, iso(due), why), c)
            return int(cur.lastrowid)

    def followup(self, followup_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM followups WHERE id = ?", (followup_id,))

    def followups(self, *states: str) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in states)
        return self._all("SELECT * FROM followups WHERE state IN (%s) ORDER BY due_at, id" % marks, states)

    def due_followups(self, now: dt.datetime) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM followups WHERE state = 'scheduled' AND due_at <= ? ORDER BY due_at, id",
                         (iso(now),))

    def move_followup(self, followup_id: int, to: str, from_: tuple[str, ...], note: str = "") -> bool:
        with self.tx() as c:
            moved = self._move(c, "followups", followup_id, to, from_, note=note)
            if moved:
                self.log("followup." + to, followup_id, note, c)
            return moved

    def cancel_followups(self, lead_email: str, reason: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE followups SET state = 'cancelled', note = ?, updated_at = ? WHERE state = 'scheduled'"
                " AND reply_id IN (SELECT id FROM replies WHERE lead_email = ?)",
                (reason, self.now(), lead_email.lower()))
            if cur.rowcount:
                self.log("followup.cancelled", lead_email.lower(), "%d: %s" % (cur.rowcount, reason), c)
            return cur.rowcount

    def disable_followups(self) -> None:
        with self.tx() as c:
            c.execute("UPDATE followups SET state = 'cancelled', note = 'follow-ups disabled', updated_at = ?"
                      " WHERE state IN ('scheduled', 'carded')", (self.now(),))
            c.execute("UPDATE cards SET state = 'closed', note = 'follow-ups disabled', updated_at = ?"
                      " WHERE kind = 'followup' AND state = 'pending'", (self.now(),))

    # ---- suppression ---------------------------------------------------------------------------

    def suppress(self, email: str, reason: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO suppressed (email, reason, created_at) VALUES (?, ?, ?)",
                      (email.lower(), reason, self.now()))
            self.log("suppressed", email.lower(), reason, c)

    def is_suppressed(self, email: str) -> bool:
        return self._one("SELECT 1 AS x FROM suppressed WHERE email = ?", (email.lower(),)) is not None

    # ---- status --------------------------------------------------------------------------------

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for table, col in (("replies", "COALESCE(label, status)"), ("cards", "state"), ("sends", "state"),
                           ("followups", "state")):
            rows = self.db.execute("SELECT %s AS k, COUNT(*) AS n FROM %s GROUP BY %s" % (col, table, col)).fetchall()
            out[table] = {str(r["k"]): int(r["n"]) for r in rows}
        return out
