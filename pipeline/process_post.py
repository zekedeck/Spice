from langdetect import detect, LangDetectException

from pipeline.nlp import extract_locations, extract_emoji_locations
from pipeline.semantic_filter import filter_venue_locations
from pipeline.geocoder import resolve_post_locations
from pipeline.scorer import apply_confidence_threshold


def resolve_post(text, nlp, geocoder, conn, llm_client=None):
    """Runs the shared post-resolution pipeline: language check, location
    extraction, venue filtering, geocoding, confidence threshold.

    Returns (confident_candidates, skip_reason) where skip_reason is None
    on success, or one of "language" / "no_location" / "no_geocode" /
    "low_confidence" on an early exit (confident_candidates is None/empty
    in that case).
    """
    try:
        if detect(text) != "en":
            return None, "language"
    except LangDetectException:
        pass

    emoji_pairs = extract_emoji_locations(text)
    nlp_pairs, doc = extract_locations(text, nlp)
    all_pairs = emoji_pairs + [p for p in nlp_pairs if p[0] not in {e[0] for e in emoji_pairs}]

    venue_pairs = filter_venue_locations(all_pairs, text, doc=doc)

    if not venue_pairs:
        return None, "no_location"

    candidates = resolve_post_locations(
        text, venue_pairs, geocoder, conn, all_entity_pairs=all_pairs, llm_client=llm_client
    )

    if not candidates:
        return None, "no_geocode"

    confident = apply_confidence_threshold(candidates, text, all_pairs)

    if not confident:
        return None, "low_confidence"

    return confident, None
