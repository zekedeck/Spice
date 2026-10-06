import glob
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from langdetect import detect, LangDetectException

from pipeline.database import (
    init_db,
    get_unprocessed_posts,
    mark_processed,
    insert_pin,
    get_pins_since,
    get_pins_for_range,
    prune_old_pins,
)
from pipeline.nlp import load_nlp_model, extract_locations, extract_emoji_locations
from pipeline.semantic_filter import filter_venue_locations
from pipeline.geocoder import init_geocoder, resolve_post_locations
from pipeline.scorer import apply_confidence_threshold
from pipeline.writer import post_to_features, write_geojson
from models import GeoPost
import config
from config import DB_PATH, LIVE_WINDOW_HOURS, ARCHIVE_RETENTION_DAYS, LIVE_OUTPUT_PATH, ARCHIVE_DIR


def _pin_rows_to_feature_collection(rows: list) -> dict:
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


def run_worker() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(__name__)

    geocoder = init_geocoder()
    nlp = load_nlp_model()
    conn = init_db(DB_PATH)
    posts = get_unprocessed_posts(conn, since=None, reprocess=False)

    log.info("Processing %d unprocessed posts", len(posts))

    geo_posts = []
    skipped_no_location = 0
    skipped_no_geocode = 0
    skipped_language = 0
    skipped_low_confidence = 0

    total_posts = len(posts)
    for i, post in enumerate(posts):
        if i > 0 and i % 1000 == 0:
            log.info(
                "Progress: %d/%d posts | geo_posts=%d | skipped: lang=%d no_loc=%d no_geo=%d low_conf=%d",
                i, total_posts, len(geo_posts),
                skipped_language, skipped_no_location, skipped_no_geocode,
                skipped_low_confidence,
            )
        text = post["text"]

        try:
            if detect(text) != "en":
                skipped_language += 1
                mark_processed(conn, post["post_uri"])
                continue
        except LangDetectException:
            pass

        emoji_pairs = extract_emoji_locations(text)
        nlp_pairs, doc = extract_locations(text, nlp)
        all_pairs = emoji_pairs + [p for p in nlp_pairs if p[0] not in {e[0] for e in emoji_pairs}]

        venue_pairs = filter_venue_locations(all_pairs, text, doc=doc)

        if not venue_pairs:
            skipped_no_location += 1
            mark_processed(conn, post["post_uri"])
            continue

        candidates = resolve_post_locations(text, venue_pairs, geocoder, conn, all_entity_pairs=all_pairs)

        if not candidates:
            skipped_no_geocode += 1
            mark_processed(conn, post["post_uri"])
            continue

        confident = apply_confidence_threshold(candidates, text, all_pairs)

        if not confident:
            skipped_low_confidence += 1
            mark_processed(conn, post["post_uri"])
            continue

        filtered = confident

        geo_post = GeoPost(
            post_uri=post["post_uri"],
            handle=post["handle"],
            did=post["did"],
            text=text,
            created_at=post["created_at"],
            post_url=post["post_url"],
            candidates=filtered,
        )
        geo_posts.append(geo_post)

        features = post_to_features(geo_post)
        total = len(features)
        for idx, feature in enumerate(features, start=1):
            props = feature["properties"]
            lng, lat = feature["geometry"]["coordinates"]
            insert_pin(
                conn,
                pin_id=f"{geo_post.post_uri}__{idx}",
                post_uri=props["post_uri"],
                post_url=props["source"],
                handle=props["handle"],
                text=props["text"],
                post_created_at=props["created_at"],
                lat=lat,
                lng=lng,
                location_text=props["location_text"],
                mapped_location=props["mapped_location"],
                precision=props["precision_level"],
                confidence=props["confidence"],
                candidate_index=idx,
                candidate_total=total,
                other_locations=props.get("other_locations"),
            )

        mark_processed(conn, post["post_uri"])

    # Regenerate live + archive output files fresh from the pins table.
    since_iso = (datetime.utcnow() - timedelta(hours=LIVE_WINDOW_HOURS)).isoformat()
    live_rows = get_pins_since(conn, since_iso)
    live_collection = _pin_rows_to_feature_collection(live_rows)
    os.makedirs(os.path.dirname(config.LIVE_OUTPUT_PATH), exist_ok=True)
    write_geojson(live_collection, LIVE_OUTPUT_PATH)

    now_et = datetime.now(ZoneInfo("America/New_York"))
    date_str = now_et.strftime("%Y-%m-%d")
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + timedelta(days=1)
    start_iso = start_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
    end_iso = end_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()

    archive_rows = get_pins_for_range(conn, start_iso, end_iso)
    archive_collection = _pin_rows_to_feature_collection(archive_rows)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    write_geojson(archive_collection, f"{ARCHIVE_DIR}/{date_str}.geojson")

    # Prune old data: DB pins table + old archive files.
    prune_old_pins(conn, ARCHIVE_RETENTION_DAYS)

    cutoff_date = now_et.date() - timedelta(days=ARCHIVE_RETENTION_DAYS)
    for path in glob.glob(f"{ARCHIVE_DIR}/*.geojson"):
        basename = os.path.basename(path)
        file_date_str = basename[:-len(".geojson")]
        try:
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff_date:
            os.remove(path)
            log.info("Deleted expired archive file %s", path)

    total_pins = sum(len(p.candidates) for p in geo_posts)
    log.info(
        "Done. Total: %d | Non-English: %d | No location: %d | No geocode: %d | Low confidence: %d | Written: %d posts -> %d pins",
        len(posts), skipped_language, skipped_no_location, skipped_no_geocode, skipped_low_confidence, len(geo_posts), total_pins
    )


if __name__ == "__main__":
    run_worker()
