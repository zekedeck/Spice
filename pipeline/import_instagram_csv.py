"""One-off backfill: import a historical Instagram CSV export (Apify
"instagram-scraper" dataset shape) through the real Spice pipeline and
write day-grouped archive GeoJSON files.

NOT wired into the GitHub Actions workflow and NOT a recurring collector
-- run manually, e.g.:

    python3 -m pipeline.import_instagram_csv dataset_instagram-scraper_2026-10-29_01-32-26-432.csv

What it does:
  1. Parses the CSV with csv.DictReader (utf-8-sig) -- NOT naive line
     splitting, since captions contain embedded newlines inside quoted
     fields.
  2. For each row with a non-empty caption, builds an IngestedPost
     (models.py's ingestion contract) and inserts it via
     pipeline.database.insert_post() into a DEDICATED SQLite DB
     (data/imports/<account>.db), completely separate from the live
     data/raw/posts.db.
  3. Runs pipeline.process_post.resolve_post() -- the exact same
     NLP-extraction -> geocoding -> scoring pipeline worker.py calls --
     against every inserted, unprocessed post in that dedicated DB.
  4. Writes day-grouped GeoJSON files, in the same shape worker.py's
     archive files use, to data/archive_imports/<account>/YYYY-MM-DD.geojson
     -- grouped by the post's own created_at date (historical), NOT
     collection date. This directory is never scanned by worker.py's
     ARCHIVE_RETENTION_DAYS pruning loop (which only globs data/archive/).

Rate limiting: resolve_post_locations() already sleeps 1s between
Nominatim calls (geocoder.py's own courtesy-limit convention) -- this
script does not add, remove, or short-circuit that.

LLM layer: if GROQ_API_KEY/ANTHROPIC_API_KEY aren't set, init_llm_client()
gracefully returns None and resolve_post() runs the rule-based-only path.
That's an expected, already-established local-run degradation, not an
error this script needs to handle specially.
"""
import argparse
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime

from models import IngestedPost
from pipeline.database import (
    init_db,
    insert_post,
    get_unprocessed_posts,
    mark_processed,
    insert_pin,
)
from pipeline.nlp import load_nlp_model
from pipeline.geocoder import init_geocoder
from pipeline.llm_geotagging import init_llm_client
from pipeline.process_post import resolve_post
from pipeline.writer import write_geojson

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TEXT_TRUNCATE_LEN = 4096  # same convention as pipeline/collector.py's text[:4096]

SOURCE_PREFIX = "instagram_import_"


def _pin_rows_to_feature_collection(rows: list) -> dict:
    """Same archive-file shape as worker.py's _pin_rows_to_feature_collection
    (duplicated here rather than imported from worker, since worker.py is a
    script entrypoint module, not a library this one should depend on)."""
    features = []
    for row in rows:
        properties = {
            "post_uri": row["post_uri"],
            "handle": row["handle"],
            "text": row["text"],
            "created_at": row["post_created_at"],
            "source": row["post_url"],
            "mapped_location": row["mapped_location"],
            "precision_level": row["precision"],
            "location_text": row["location_text"],
            "confidence": row["confidence"],
            "address_type": row["address_type"],
            "provenance": row["provenance"],
            "borough_conflict": bool(row["borough_conflict"]) if row["borough_conflict"] is not None else False,
            "specificity_score": row["specificity_score"],
        }
        if row.get("other_locations"):
            properties["other_locations"] = row["other_locations"]

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [row["lng"], row["lat"]],
            },
            "properties": properties,
        })

    return {
        "type": "FeatureCollection",
        "properties": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "total_pins": len(features),
        },
        "features": features,
    }


def parse_csv_rows(csv_path: str) -> list:
    rows = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            caption = (row.get("caption") or "").strip()
            if not caption:
                continue
            rows.append(row)
    return rows


def ingest_rows(conn, rows: list, account: str) -> tuple:
    """Inserts each row as an IngestedPost. Returns (inserted, duplicates)."""
    inserted = 0
    duplicates = 0
    for row in rows:
        handle = row.get("ownerUsername") or account
        caption = row["caption"].strip()[:TEXT_TRUNCATE_LEN]
        post = IngestedPost(
            source=f"{SOURCE_PREFIX}{account}",
            author_id=handle,
            handle=handle,
            post_uri=row["url"],
            post_url=row["url"],
            text=caption,
            created_at=row["timestamp"],
        )
        if insert_post(conn, post):
            inserted += 1
        else:
            duplicates += 1
    return inserted, duplicates


def process_posts(conn, nlp, geocoder, llm_client) -> dict:
    """Runs resolve_post() against every unprocessed post in conn, inserting
    pins exactly as worker.py does. Returns summary stats."""
    posts = get_unprocessed_posts(conn, since=None, reprocess=False)
    total = len(posts)
    log.info("Processing %d unprocessed posts", total)

    stats = {
        "total": total,
        "with_pins": 0,
        "total_pins": 0,
        "skipped_language": 0,
        "skipped_no_location": 0,
        "skipped_no_geocode": 0,
        "skipped_low_confidence": 0,
        "skipped_error": 0,
    }

    for i, post in enumerate(posts):
        if i > 0 and i % 25 == 0:
            log.info(
                "Progress: %d/%d | with_pins=%d pins=%d | skipped: lang=%d no_loc=%d no_geo=%d low_conf=%d err=%d",
                i, total, stats["with_pins"], stats["total_pins"],
                stats["skipped_language"], stats["skipped_no_location"],
                stats["skipped_no_geocode"], stats["skipped_low_confidence"],
                stats["skipped_error"],
            )

        text = post["text"]
        try:
            confident, skip_reason = resolve_post(text, nlp, geocoder, conn, llm_client=llm_client)

            if skip_reason == "language":
                stats["skipped_language"] += 1
                mark_processed(conn, post["post_uri"])
                continue
            if skip_reason == "no_location":
                stats["skipped_no_location"] += 1
                mark_processed(conn, post["post_uri"])
                continue
            if skip_reason == "no_geocode":
                stats["skipped_no_geocode"] += 1
                mark_processed(conn, post["post_uri"])
                continue
            if skip_reason == "low_confidence":
                stats["skipped_low_confidence"] += 1
                mark_processed(conn, post["post_uri"])
                continue

            candidates = confident
            total_candidates = len(candidates)
            stats["with_pins"] += 1
            stats["total_pins"] += total_candidates

            for idx, candidate in enumerate(candidates, start=1):
                other_locations = ", ".join(
                    c.text for c in candidates if c.text != candidate.text
                )
                insert_pin(
                    conn,
                    pin_id=f"{post['post_uri']}__{idx}",
                    post_uri=post["post_uri"],
                    post_url=post["post_url"],
                    handle=post["handle"],
                    text=text,
                    post_created_at=post["created_at"],
                    lat=candidate.lat,
                    lng=candidate.lng,
                    location_text=candidate.text,
                    mapped_location=candidate.display_name,
                    precision=candidate.precision,
                    confidence=candidate.confidence,
                    candidate_index=idx,
                    candidate_total=total_candidates,
                    other_locations=other_locations or None,
                    address_type=candidate.address_type,
                    provenance=candidate.provenance,
                    borough_conflict=candidate.borough_conflict,
                    specificity_score=candidate.specificity_score,
                )

            mark_processed(conn, post["post_uri"])
        except Exception:
            stats["skipped_error"] += 1
            log.exception("Unhandled error processing post %s; marking processed and skipping", post["post_uri"])
            mark_processed(conn, post["post_uri"])
            continue

    return stats


def write_day_grouped_archive(conn, output_dir: str) -> dict:
    """Groups all pins currently in conn's pins table by the post's own
    created_at date (historical date, not collection date) and writes one
    GeoJSON file per distinct date into output_dir."""
    os.makedirs(output_dir, exist_ok=True)

    cursor = conn.execute("SELECT * FROM pins")
    rows = [dict(row) for row in cursor.fetchall()]

    by_day = defaultdict(list)
    for row in rows:
        created_at = row["post_created_at"] or ""
        day = created_at[:10]  # ISO-8601 timestamps start with YYYY-MM-DD
        if len(day) == 10:
            by_day[day].append(row)

    for day, day_rows in by_day.items():
        collection = _pin_rows_to_feature_collection(day_rows)
        write_geojson(collection, os.path.join(output_dir, f"{day}.geojson"))

    day_counts = {day: len(day_rows) for day, day_rows in by_day.items()}

    # manifest.json: the real set of dates this import actually produced
    # data for, plus min/max. The frontend (app.js's IMPORT_MODE) reads
    # this directly rather than guessing/globbing -- it's served as a
    # static file over GitHub Pages with no directory listing available.
    manifest = {
        "account": os.path.basename(output_dir.rstrip("/")),
        "dates": sorted(day_counts),
        "min_date": min(day_counts) if day_counts else None,
        "max_date": max(day_counts) if day_counts else None,
        "total_pins": sum(day_counts.values()),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    with open(os.path.join(output_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return day_counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="Path to the Apify Instagram-scraper CSV export")
    parser.add_argument(
        "--account", default="account1",
        help="Account slug, used for the dedicated DB path and output dir name",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Override the dedicated SQLite DB path (default: data/imports/<account>.db)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Override the output dir (default: data/archive_imports/<account>/)",
    )
    args = parser.parse_args()

    db_path = args.db_path or f"data/imports/{args.account}.db"
    output_dir = args.output_dir or f"data/archive_imports/{args.account}"

    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    log.info("Parsing CSV: %s", args.csv_path)
    rows = parse_csv_rows(args.csv_path)
    log.info("Parsed %d rows with non-empty captions", len(rows))

    conn = init_db(db_path)
    inserted, duplicates = ingest_rows(conn, rows, args.account)
    log.info("Inserted %d new posts (%d already present / duplicate)", inserted, duplicates)

    log.info("Loading NLP model, geocoder, LLM client...")
    nlp = load_nlp_model()
    geocoder = init_geocoder()
    llm_client = init_llm_client()
    if llm_client is None:
        log.info("No LLM client available (missing API key) -- running rule-based-only path, as expected.")

    stats = process_posts(conn, nlp, geocoder, llm_client)

    day_counts = write_day_grouped_archive(conn, output_dir)

    print("\n" + "=" * 70)
    print("IMPORT SUMMARY")
    print("=" * 70)
    print(f"CSV rows with captions parsed : {len(rows)}")
    print(f"Posts inserted (new)           : {inserted}")
    print(f"Posts already present          : {duplicates}")
    print(f"Posts processed this run       : {stats['total']}")
    print(f"Posts producing >=1 geocoded pin: {stats['with_pins']}")
    print(f"Total pins written             : {stats['total_pins']}")
    print(f"  skipped (non-English)        : {stats['skipped_language']}")
    print(f"  skipped (no location found)  : {stats['skipped_no_location']}")
    print(f"  skipped (no geocode)         : {stats['skipped_no_geocode']}")
    print(f"  skipped (low confidence)     : {stats['skipped_low_confidence']}")
    print(f"  skipped (error)              : {stats['skipped_error']}")
    print(f"Output directory               : {output_dir}")
    print(f"Distinct days with data        : {len(day_counts)}")
    if day_counts:
        for day in sorted(day_counts):
            print(f"  {day}: {day_counts[day]} pins")
        print(f"Date range of output: {min(day_counts)} .. {max(day_counts)}")
    print("=" * 70)


if __name__ == "__main__":
    main()
