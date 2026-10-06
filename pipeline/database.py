import sqlite3
import logging
import datetime

logger = logging.getLogger(__name__)


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
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

    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_processed ON posts (processed)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts (created_at)")
    conn.commit()
    return conn


def insert_post(conn, did, handle, post_uri, post_url, text, created_at) -> bool:
    collected_at = datetime.datetime.utcnow().isoformat()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO posts
            (did, handle, post_uri, post_url, text, created_at, collected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (did, handle, post_uri, post_url, text, created_at, collected_at),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_unprocessed_posts(conn, limit: int = 50000, since: str = None, reprocess: bool = False) -> list:
    if since:
        cursor = conn.execute(
            "SELECT * FROM posts WHERE created_at >= ? ORDER BY created_at ASC LIMIT ?",
            (since, limit),
        )
    elif reprocess:
        cursor = conn.execute(
            "SELECT * FROM posts ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
    else:
        cursor = conn.execute(
            "SELECT * FROM posts WHERE processed = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
    return [dict(row) for row in cursor.fetchall()]


def mark_processed(conn, post_uri: str) -> None:
    conn.execute("UPDATE posts SET processed = 1 WHERE post_uri = ?", (post_uri,))
    conn.commit()


def prune_old_posts(conn, days: int = 30) -> int:
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
    cursor = conn.execute("DELETE FROM posts WHERE created_at < ?", (cutoff,))
    conn.commit()
    count = cursor.rowcount
    logger.info("Pruned %d posts older than %d days", count, days)
    return count


def delete_post(conn, post_uri: str) -> None:
    conn.execute("DELETE FROM posts WHERE post_uri = ?", (post_uri,))
    conn.commit()
