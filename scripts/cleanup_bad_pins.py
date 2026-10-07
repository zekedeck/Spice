"""One-time cleanup: delete specific known-bad pins from the persistent DB.

Scaffolding for a single manual cleanup, not part of normal pipeline
operation. These two pins were produced by the bounded-search false-match
bug fixed in pipeline/geocoder.py (commit 46aa04b) -- the geocoder fix
prevents new occurrences, but doesn't retroactively remove rows already
inserted before the fix shipped.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.database import init_db
from config import DB_PATH

BAD_PINS = [
    ("at://did:plc:t7fedaekbh6ovf3vwyh6zks3/app.bsky.feed.post/3mxaf64bx6426", "Seneca County"),
    ("at://did:plc:5b52j7bfya4heoo2yodrdvjq/app.bsky.feed.post/3mxc53pfsds24", "East Williamsburg"),
]


def main():
    conn = init_db(DB_PATH)
    total_deleted = 0
    for post_uri, location_text in BAD_PINS:
        cursor = conn.execute(
            "DELETE FROM pins WHERE post_uri = ? AND location_text = ?",
            (post_uri, location_text),
        )
        conn.commit()
        print(f"Deleted {cursor.rowcount} pin(s) for post_uri={post_uri!r} location_text={location_text!r}")
        total_deleted += cursor.rowcount
    print(f"Total deleted: {total_deleted}")


if __name__ == "__main__":
    main()
