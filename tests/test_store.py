"""Durable state upgrades preserve sends and hold uncertain card deliveries."""

import sqlite3

from reply_desk.store import Store


def test_version_one_upgrade_preserves_ledger_and_holds_unidentified_posts(tmp_path):
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
            PRAGMA user_version = 1;
        """)
    store = Store(path)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.reply(1)["body_complete"] == 1
    assert store.card("c1")["post_state"] == "posted"
    assert store.card("c1")["reviewed_draft"] == "First draft"
    assert store.card("c2")["post_state"] == "locked"
    assert store.card("c2")["reviewed_draft"] is None
    assert store.send(1) == {"id": 1, "intent_key": "reply:1", "state": "locked"}
    store.close()
    store = Store(path)
    assert store.card("c2")["post_state"] == "locked"
    store.close()
