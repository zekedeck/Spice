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

    init_geocode_cache_table(conn)
    init_pins_table(conn)
    init_llm_cache_table(conn)

    conn.commit()
    return conn


def init_geocode_cache_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            query_key TEXT PRIMARY KEY,
            lat REAL,
            lng REAL,
            display_name TEXT,
            address_type TEXT,
            importance REAL,
            cached_at TEXT,
            ttl_days INTEGER DEFAULT 30
        )
    """)


def init_llm_cache_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_entity_cache (
            cache_key TEXT PRIMARY KEY,
            action TEXT,
            lat REAL,
            lng REAL,
            query TEXT,
            confidence REAL,
            cached_at TEXT,
            ttl_days INTEGER DEFAULT 30
        )
    """)


def init_pins_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pins (
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

    conn.execute("CREATE INDEX IF NOT EXISTS idx_pins_post_created_at ON pins (post_created_at)")


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


def insert_pin(conn, pin_id, post_uri, post_url, handle, text, post_created_at, lat, lng, location_text, mapped_location, precision, confidence, candidate_index, candidate_total, other_locations=None) -> None:
    inserted_at = datetime.datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO pins
            (pin_id, post_uri, post_url, handle, text, post_created_at, lat, lng,
             location_text, mapped_location, precision, confidence, candidate_index,
             candidate_total, other_locations, inserted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pin_id, post_uri, post_url, handle, text, post_created_at, lat, lng,
         location_text, mapped_location, precision, confidence, candidate_index,
         candidate_total, other_locations, inserted_at),
    )
    conn.commit()


def get_pins_since(conn, since_iso: str) -> list:
    cursor = conn.execute(
        "SELECT * FROM pins WHERE post_created_at >= ?",
        (since_iso,),
    )
    return [dict(row) for row in cursor.fetchall()]


def get_pins_for_range(conn, start_iso: str, end_iso: str) -> list:
    cursor = conn.execute(
        "SELECT * FROM pins WHERE post_created_at >= ? AND post_created_at < ?",
        (start_iso, end_iso),
    )
    return [dict(row) for row in cursor.fetchall()]


def prune_old_pins(conn, days: int = 30) -> int:
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
    cursor = conn.execute("DELETE FROM pins WHERE post_created_at < ?", (cutoff,))
    conn.commit()
    count = cursor.rowcount
    logger.info("Pruned %d pins older than %d days", count, days)
    return count


def get_cached_geocode(conn, query_key: str) -> dict | None:
    cursor = conn.execute(
        "SELECT lat, lng, display_name, address_type, importance, cached_at, ttl_days "
        "FROM geocode_cache WHERE query_key = ?",
        (query_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    row = dict(row)
    cached_at = datetime.datetime.fromisoformat(row["cached_at"])
    ttl_days = row["ttl_days"]
    if datetime.datetime.utcnow() > cached_at + datetime.timedelta(days=ttl_days):
        return None

    return {
        "lat": row["lat"],
        "lng": row["lng"],
        "display_name": row["display_name"],
        "address_type": row["address_type"],
        "importance": row["importance"],
    }


def set_cached_geocode(conn, query_key: str, lat: float, lng: float, display_name: str, address_type: str, importance: float, ttl_days: int = 30) -> None:
    cached_at = datetime.datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO geocode_cache
            (query_key, lat, lng, display_name, address_type, importance, cached_at, ttl_days)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (query_key, lat, lng, display_name, address_type, importance, cached_at, ttl_days),
    )
    conn.commit()


def get_cached_llm_result(conn, cache_key: str) -> dict | None:
    cursor = conn.execute(
        "SELECT action, lat, lng, query, confidence, cached_at, ttl_days "
        "FROM llm_entity_cache WHERE cache_key = ?",
        (cache_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    row = dict(row)
    cached_at = datetime.datetime.fromisoformat(row["cached_at"])
    ttl_days = row["ttl_days"]
    if datetime.datetime.utcnow() > cached_at + datetime.timedelta(days=ttl_days):
        return None

    return {
        "action": row["action"],
        "lat": row["lat"],
        "lng": row["lng"],
        "query": row["query"],
        "confidence": row["confidence"],
    }


def set_cached_llm_result(conn, cache_key: str, action: str, lat: float, lng: float, query: str, confidence: float, ttl_days: int = 30) -> None:
    cached_at = datetime.datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO llm_entity_cache
            (cache_key, action, lat, lng, query, confidence, cached_at, ttl_days)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (cache_key, action, lat, lng, query, confidence, cached_at, ttl_days),
    )
    conn.commit()
