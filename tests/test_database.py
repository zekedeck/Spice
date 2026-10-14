"""Tests for pipeline.database's pins-table schema migration.

Run from the repo root with:  python -m pytest tests/ -q

Context: the pins table lives in a SQLite DB that persists ONLY via a
GitHub Actions cache (never committed) across pipeline runs. Adding new
columns (address_type, provenance, borough_conflict, specificity_score)
must not crash against a DB cached from a run before those columns
existed. SQLite has no "ALTER TABLE ADD COLUMN IF NOT EXISTS", so the
migration guard checks PRAGMA table_info(pins) first and only runs
ALTER TABLE for columns that are actually missing.
"""

import sqlite3

from pipeline.database import init_db, init_pins_table, insert_pin, get_pins_since

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
