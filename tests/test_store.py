"""State schema compatibility and timestamp ordering."""

import datetime as dt
import sqlite3

import pytest

from reply_desk.store import Store, iso, zulu


@pytest.mark.parametrize("version", [0, 1, 2, 4])
def test_unsupported_schema_cannot_promote_an_unverified_body(tmp_path, version):
    path = tmp_path / "desk.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE replies (id INTEGER PRIMARY KEY, body TEXT);
            CREATE TABLE cards (id TEXT PRIMARY KEY, message_id TEXT, draft TEXT);
            CREATE TABLE sends (id INTEGER PRIMARY KEY, intent_key TEXT UNIQUE, state TEXT);
            INSERT INTO replies VALUES (1, 'Tell me more');
            INSERT INTO cards VALUES ('c1', '101', 'First draft');
            INSERT INTO cards VALUES ('c2', NULL, 'Second draft');
            INSERT INTO sends VALUES (1, 'reply:1', 'locked');
        """)
        db.execute("PRAGMA user_version = %d" % version)
    with pytest.raises(RuntimeError, match="unsupported state schema"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert db.execute("SELECT * FROM replies").fetchall() == [(1, "Tell me more")]
        assert db.execute("SELECT * FROM sends").fetchall() == [(1, "reply:1", "locked")]
        assert "body_complete" not in {row[1] for row in db.execute("PRAGMA table_info(replies)")}


def test_previous_schema_is_rejected_even_when_its_columns_match(tmp_path):
    path = tmp_path / "desk.db"
    store = Store(path)
    rid = store.add_reply({"provider_id": "in-1", "lead_email": "lead@example.com", "inbox": "sender@example.org",
                           "body": "Tell me more", "received_at": "2026-10-21T13:00:00+00:00"})
    assert store.reply(rid)["body_complete"] == 1
    store.db.execute("PRAGMA user_version = 2")
    store.close()
    with pytest.raises(RuntimeError, match="unsupported state schema 2"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT body, body_complete FROM replies").fetchall() == [("Tell me more", 1)]


def test_fresh_state_uses_the_verified_schema_and_reopens(tmp_path):
    path = tmp_path / "desk.db"
    store = Store(path)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
    store.close()
    store = Store(path)
    assert store.counts()["sends"] == {}
    store.close()


def test_timestamp_formats_keep_fractional_precision_in_utc():
    moment = dt.datetime.fromisoformat("2026-10-21T15:00:00.125001+02:00")
    assert iso(moment) == "2026-10-21T13:00:00.125001+00:00"
    assert zulu(moment) == "2026-10-21T13:00:00.125001Z"
    assert iso(moment.replace(microsecond=0)) == "2026-10-21T13:00:00.000000+00:00"


def test_reply_freshness_and_cancellation_use_normalized_fractional_times(tmp_path):
    store = Store(tmp_path / "desk.db")
    ids = [store.add_reply({"provider_id": mid, "lead_email": "lead@example.com", "inbox": "sender@example.org",
                            "received_at": at}) for mid, at in [
                                ("in-late", "2026-10-21T08:00:00.9-05:00"),
                                ("in-early", "2026-10-21T15:00:00.100001+02:00"),
                                ("in-middle", "2026-10-21T13:00:00.500Z")]]
    late, early, middle = ids
    assert store.reply(early)["received_at"] == "2026-10-21T13:00:00.100001+00:00"
    assert not store.has_newer_reply(late)
    assert store.has_newer_reply(early) and store.has_newer_reply(middle)
    assert [r["id"] for r in store.replies("new")] == [early, middle, late]
    due = dt.datetime.fromisoformat("2026-10-23T09:00:00.75+00:00")
    later_followup = store.schedule_followup(late, 1, due, "due")
    earlier_followup = store.schedule_followup(early, 1, due, "due")
    assert store.cancel_followups("lead@example.com", "new reply", before_reply_id=middle) == 1
    assert store.followup(earlier_followup)["state"] == "cancelled"
    assert store.followup(later_followup)["state"] == "scheduled"
    assert not store.due_followups(due - dt.timedelta(microseconds=1))
    assert [f["id"] for f in store.due_followups(due)] == [later_followup]
    store.close()


@pytest.mark.parametrize("flag,expected", [(True, 1), (1, 1), ("1", 1), ("true", 1),
                                          (False, 0), (0, 0), ("0", 0), ("false", 0), (None, 0), ("unknown", 0)])
def test_stored_auto_reply_flags_use_the_provider_conventions(tmp_path, flag, expected):
    store = Store(tmp_path / "desk.db")
    rid = store.add_reply({"provider_id": "in-1", "lead_email": "lead@example.com", "inbox": "sender@example.org",
                           "received_at": "2026-10-21T13:00:00Z", "auto_reply": flag})
    assert store.reply(rid)["auto_reply"] == expected
    store.close()
