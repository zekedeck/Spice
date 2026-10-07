# Legacy interactive CLI tool, kept for offline/manual debugging only.
# Production runs go through worker.py (headless, no input() prompts) via
# GitHub Actions -- not this file. radius_filter.py and github_push.py
# below are used only here; worker.py doesn't need them since the
# geocoder is already NYC-bbox-bounded and the Actions workflow itself
# handles git commit/push, not Python.
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from langdetect import detect, LangDetectException

from pipeline.database import init_db, get_unprocessed_posts, mark_processed
from pipeline.nlp import load_nlp_model, extract_locations, extract_emoji_locations
from pipeline.semantic_filter import filter_venue_locations
from pipeline.geocoder import init_geocoder, resolve_post_locations, geocode_query
from pipeline.radius_filter import filter_by_radius
from pipeline.scorer import apply_confidence_threshold
from pipeline.writer import build_feature_collection, write_geojson
from pipeline.github_push import push_to_github
from models import GeoPost
from config import DB_PATH, DEFAULT_RADIUS_MILES


def run_pipeline() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(__name__)

    geocoder = init_geocoder()

    mode_input = input("Mode — radius filter or global map? [r/g, default r]: ").strip().lower()
    global_mode = mode_input == "g"

    anchor_center = None
    anchor_resolved = ""
    radius_miles = 0.0
    address_input = ""

    if global_mode:
        print("Global mode — all geocoded locations will be mapped.")
    else:
        address_input = input("Address to search around: ").strip()
        result = geocode_query(address_input, geocoder)
        if result is None:
            print(f"Could not geocode address: {address_input}")
            sys.exit(1)

        anchor_center = (result["lat"], result["lng"])
        anchor_resolved = result["display_name"]

        print(f"Resolved to: {anchor_resolved}")
        proceed = input("Proceed? [y/n]: ").strip()
        if proceed != "y":
            print("Aborted.")
            sys.exit(0)

        radius_input = input(f"Radius in miles [default {DEFAULT_RADIUS_MILES}]: ").strip()
        if radius_input:
            try:
                radius_miles = float(radius_input)
            except ValueError:
                print(f"Invalid radius '{radius_input}', using default {DEFAULT_RADIUS_MILES}")
                radius_miles = DEFAULT_RADIUS_MILES
        else:
            radius_miles = DEFAULT_RADIUS_MILES

    reprocess_input = input("Reprocess already-processed posts? [y/n, default n]: ").strip().lower()
    reprocess = reprocess_input == "y"

    lookback_input = input("Look back from EST (MM/DD/YY HH:MM) [default: all posts]: ").strip()
    since_iso = None
    if lookback_input:
        try:
            since_dt = datetime.strptime(lookback_input, "%m/%d/%y %H:%M")
            since_dt_est = since_dt.replace(tzinfo=ZoneInfo("America/New_York"))
            since_iso = since_dt_est.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
            print(f"Filtering posts from {since_dt.strftime('%B %d, %Y %H:%M')} EST onward")
        except ValueError:
            print(f"Could not parse '{lookback_input}', processing all posts")

    output_input = input("Output file [default output.geojson]: ").strip()
    output_file = output_input if output_input else "output.geojson"
    if not output_file.endswith(".geojson"):
        output_file += ".geojson"

    nlp = load_nlp_model()
    conn = init_db(DB_PATH)
    posts = get_unprocessed_posts(conn, since=since_iso, reprocess=reprocess)

    log.info("Processing %d unprocessed posts", len(posts))

    geo_posts = []
    skipped_no_location = 0
    skipped_no_geocode = 0
    skipped_outside_radius = 0
    skipped_language = 0
    skipped_low_confidence = 0

    total_posts = len(posts)
    for i, post in enumerate(posts):
        if i > 0 and i % 1000 == 0:
            log.info(
                "Progress: %d/%d posts | geo_posts=%d | skipped: lang=%d no_loc=%d no_geo=%d low_conf=%d radius=%d",
                i, total_posts, len(geo_posts),
                skipped_language, skipped_no_location, skipped_no_geocode,
                skipped_low_confidence, skipped_outside_radius,
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

        if global_mode:
            filtered = confident
        else:
            filtered = filter_by_radius(confident, anchor_center, radius_miles)

        if not filtered:
            skipped_outside_radius += 1
            mark_processed(conn, post["post_uri"])
            continue

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
        mark_processed(conn, post["post_uri"])

    collection = build_feature_collection(
        geo_posts,
        anchor_address=address_input,
        anchor_resolved=anchor_resolved,
        radius_miles=radius_miles,
    )
    write_geojson(collection, output_file)
    push_to_github(output_file)

    total_pins = sum(len(p.candidates) for p in geo_posts)
    log.info(
        "Done. Total: %d | Non-English: %d | No location: %d | No geocode: %d | Low confidence: %d | Outside radius: %d | Written: %d posts → %d pins",
        len(posts), skipped_language, skipped_no_location, skipped_no_geocode, skipped_low_confidence, skipped_outside_radius, len(geo_posts), total_pins
    )


if __name__ == "__main__":
    run_pipeline()
