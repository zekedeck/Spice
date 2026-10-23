"""Tests for pipeline.database's pins/posts-table schema migrations and
the IngestedPost ingestion contract (models.py).

Run from the repo root with:  python -m pytest tests/ -q

Context: the pins and posts tables live in a SQLite DB that persists
ONLY via a GitHub Actions cache (never committed) across pipeline runs.
Adding new columns (pins: address_type, provenance, borough_conflict,
specificity_score; posts: source) must not crash against a DB cached
from a run before those columns existed. SQLite has no "ALTER TABLE ADD
COLUMN IF NOT EXISTS", so each migration guard checks PRAGMA table_info
first and only runs ALTER TABLE for columns that are actually missing.
"""

import sqlite3

from pipeline.database import (
    init_db,
    init_pins_table,
    init_posts_table,
    insert_pin,
    insert_post,
    get_pins_since,
    get_unprocessed_posts,
)
from models import IngestedPost

_NEW_COLUMNS = ("address_type", "provenance", "borough_conflict", "specificity_score")


def _create_old_schema_pins_table(db_path: str) -> None:
    """Create a pins table using exactly the pre-migration column set, the
    way a DB cached from a run before this change would look."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE pins (
            pin_id TEXT PRIMARY KEY,
            post_uri TEXT,
            post_url TEXT,
            handle TEXT,
            text TEXT,
            post_created_at TEXT,
            lat REAL,
            lng REAL,
            location_text TEXT,
            mapped_location TEXT,
            precision TEXT,
            confidence REAL,
            candidate_index INTEGER,
            candidate_total INTEGER,
            other_locations TEXT,
            inserted_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def test_init_db_migrates_old_schema_pins_table_without_raising(tmp_path):
    db_path = str(tmp_path / "old_schema.db")
    _create_old_schema_pins_table(db_path)

    # Must not raise against the pre-existing, pre-migration table.
    conn = init_db(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(pins)").fetchall()}
    for col in _NEW_COLUMNS:
        assert col in columns
    conn.close()


def test_insert_pin_works_after_migration_with_new_fields(tmp_path):
    db_path = str(tmp_path / "old_schema.db")
    _create_old_schema_pins_table(db_path)

    conn = init_db(db_path)

    insert_pin(
        conn,
        pin_id="at://test/1__1",
        post_uri="at://test/1",
        post_url="https://bsky.app/profile/alice/post/1",
        handle="alice",
        text="hanging out at Times Square",
        post_created_at="2026-01-01T00:00:00",
        lat=40.757,
        lng=-73.986,
        location_text="Times Square",
        mapped_location="Times Square, Manhattan, New York County, City of New York, New York, United States",
        precision="address",
        confidence=0.8,
        candidate_index=1,
        candidate_total=1,
        address_type="square",
        provenance="rule",
        borough_conflict=False,
        specificity_score=0.9,
    )

    rows = get_pins_since(conn, "2020-01-01T00:00:00")
    assert len(rows) == 1
    row = rows[0]
    assert row["address_type"] == "square"
    assert row["provenance"] == "rule"
    assert row["borough_conflict"] == 0
    assert row["specificity_score"] == 0.9
    conn.close()


def _create_old_schema_posts_table(db_path: str) -> None:
    """Create a posts table using exactly the pre-"source"-column schema,
    the way a DB cached from a run before this change would look."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            did TEXT NOT NULL,
            handle TEXT,
            post_uri TEXT UNIQUE NOT NULL,
            post_url TEXT,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            processed INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def test_init_db_migrates_old_schema_posts_table_without_raising(tmp_path):
    db_path = str(tmp_path / "old_schema_posts.db")
    _create_old_schema_posts_table(db_path)

    # Must not raise against the pre-existing, pre-"source"-column table.
    conn = init_db(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
    assert "source" in columns
    conn.close()


def test_posts_migration_guard_is_idempotent_on_already_migrated_table(tmp_path):
    db_path = str(tmp_path / "fresh_posts.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    init_posts_table(conn)
    init_posts_table(conn)  # second call: "source" column already exists

    columns = {row[1] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
    assert "source" in columns
    conn.close()


def test_insert_post_round_trips_ingested_post_contract(tmp_path):
    """insert_post() takes an IngestedPost (models.py) and must persist
    every field, including the new `source` column, readable back via
    get_unprocessed_posts()."""
    db_path = str(tmp_path / "ingested.db")
    conn = init_db(db_path)

    ingested = IngestedPost(
        source="bluesky_accountlist",
        author_id="did:plc:testauthor123",
        handle="alice.bsky.social",
        post_uri="at://did:plc:testauthor123/app.bsky.feed.post/abc123",
        post_url="https://bsky.app/profile/alice.bsky.social/post/abc123",
        text="walking through Prospect Park in Brooklyn",
        created_at="2026-01-01T00:00:00",
    )

    inserted = insert_post(conn, ingested)
    assert inserted is True

    rows = get_unprocessed_posts(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "bluesky_accountlist"
    assert row["did"] == "did:plc:testauthor123"
    assert row["handle"] == "alice.bsky.social"
    assert row["post_uri"] == ingested.post_uri
    assert row["post_url"] == ingested.post_url
    assert row["text"] == ingested.text
    assert row["created_at"] == ingested.created_at
    conn.close()


def test_insert_post_dedups_on_post_uri_via_insert_or_ignore(tmp_path):
    db_path = str(tmp_path / "dedup.db")
    conn = init_db(db_path)

    ingested = IngestedPost(
        source="bluesky_firehose",
        author_id="did:plc:dup",
        handle="bob.bsky.social",
        post_uri="at://did:plc:dup/app.bsky.feed.post/same",
        post_url="https://bsky.app/profile/bob.bsky.social/post/same",
        text="at Times Square",
        created_at="2026-01-01T00:00:00",
    )

    assert insert_post(conn, ingested) is True
    # Same post_uri again (e.g. a different source re-seeing the same
    # post, or a retried insert) must be a silent no-op, not a crash.
    assert insert_post(conn, ingested) is False

    rows = get_unprocessed_posts(conn)
    assert len(rows) == 1
    conn.close()


def test_migration_guard_is_idempotent_on_already_migrated_table(tmp_path):
    """Calling init_pins_table twice (e.g. init_db run twice against the
    same cached DB) must not raise, even though the columns already
    exist on the second call."""
    db_path = str(tmp_path / "fresh.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    init_pins_table(conn)
    init_pins_table(conn)  # second call: columns already exist

    columns = {row[1] for row in conn.execute("PRAGMA table_info(pins)").fetchall()}
    for col in _NEW_COLUMNS:
        assert col in columns
    conn.close()
