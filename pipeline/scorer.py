import logging
from pipeline.geocoder import _ADDRESS_TYPES, _CITY_TYPES, _STATE_TYPES

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.40

_GROUNDED_PROVENANCE = {"rule", "llm_accept", "llm_query"}


def score_candidate(candidate, post_text: str = None, all_entity_pairs: list = None) -> float:
    # 1. Provenance: was this candidate ever checked against a real
    #    gazetteer, or is it a bare LLM claim with nothing behind it?
    #    "rule"/"llm_accept"/"llm_query" all still call Nominatim -- only
    #    "llm_resolve" invents coordinates with no lookup at all.
    if candidate.provenance in _GROUNDED_PROVENANCE:
        score = 0.55
    elif candidate.provenance == "llm_resolve":
        score = 0.35
    else:
        score = 0.45  # unknown/legacy provenance -- conservative, not a bet either way

    # 2. Address type: the raw Nominatim classification, only meaningful
    #    when grounded (empty for llm_resolve, which never queries it).
    #    Reuses the pipeline's own existing tiers rather than inventing new
    #    ones -- these are the same sets assign_precision() already uses.
    at = (candidate.address_type or "").lower()
    if at in _ADDRESS_TYPES:
        score += 0.25      # a real venue/POI result
    elif at in _CITY_TYPES:
        score += 0.15      # a real but informal area (neighbourhood, borough...)
    elif at in _STATE_TYPES or at == "country":
        score -= 0.15      # grounded, but just a jurisdiction match
    # else empty/unclassified: no adjustment -- specificity carries the
    # full weight below, since there's nothing else to check against.

    # 3. Specificity: the linguistic signal built earlier this project,
    #    available for every candidate regardless of provenance. This is
    #    what tells "New York" apart from "Union Square" -- raw word count
    #    never did, and that was the single largest lever in the old formula.
    score += (candidate.specificity_score or 0.0) * 0.3

    # 4. Borough conflict: an already-detected contradiction between
    #    another place named in the same post and where this candidate
    #    resolved. A real internal inconsistency check, not a guess.
    if candidate.borough_conflict:
        score *= 0.5

    final = round(min(1.0, max(0.0, score)), 3)
    logger.debug("Score for '%s': %.3f (provenance=%s address_type=%s specificity=%s)",
                 candidate.text, final, candidate.provenance, candidate.address_type,
                 candidate.specificity_score)
    return final


def apply_confidence_threshold(candidates: list, post_text: str, all_entity_pairs: list) -> list:
    scored = []
    for c in candidates:
        c.confidence = score_candidate(c, post_text, all_entity_pairs)
        if c.confidence >= CONFIDENCE_THRESHOLD:
            scored.append(c)
        else:
            logger.debug("Dropped '%s' — confidence %.3f below threshold %.2f", c.text, c.confidence, CONFIDENCE_THRESHOLD)
    return scored
