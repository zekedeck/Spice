"""One-time import: merge pins from a JSON export into the live pins table.

Scaffolding for a single manual merge, not part of normal pipeline operation.
Safe to re-run (insert_pin is INSERT OR REPLACE, keyed by pin_id).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.database import init_db, insert_pin
from config import DB_PATH

EXPORT_PATH = "scripts/backfill_pins_export.json"


def main():
    conn = init_db(DB_PATH)
    with open(EXPORT_PATH) as f:
        rows = json.load(f)

    for row in rows:
        insert_pin(
            conn,
            pin_id=row["pin_id"],
            post_uri=row["post_uri"],
            post_url=row["post_url"],
            handle=row["handle"],
            text=row["text"],
            post_created_at=row["post_created_at"],
            lat=row["lat"],
            lng=row["lng"],
            location_text=row["location_text"],
            mapped_location=row["mapped_location"],
            precision=row["precision"],
            confidence=row["confidence"],
            candidate_index=row["candidate_index"],
            candidate_total=row["candidate_total"],
            other_locations=row.get("other_locations"),
        )

    print(f"Imported {len(rows)} backfill pins into {DB_PATH}")


if __name__ == "__main__":
    main()
