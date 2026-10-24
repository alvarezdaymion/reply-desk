"""Unsupported state schemas are rejected without changing saved data."""

import sqlite3

import pytest

from reply_desk.store import Store


@pytest.mark.parametrize("version", [0, 1, 3])
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
