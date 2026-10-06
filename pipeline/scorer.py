import logging
from pipeline.nlp import has_emoji

logger = logging.getLogger(__name__)

PREPOSITION_SIGNALS = {"at", "in", "near", "outside", "visiting", "from", "to", "around", "inside"}

CONFIDENCE_THRESHOLD = 0.6


def score_candidate(candidate, post_text: str, all_entity_pairs: list) -> float:
    score = 0.5

    # Entity length — 2+ words is a strong signal
    tokens = candidate.text.split()
    if len(tokens) >= 2:
        score += 0.2
    else:
        score -= 0.3

    # Emoji in entity string
    if has_emoji(candidate.text):
        score -= 0.2

    # Nominatim importance score
    if candidate.importance > 0.5:
        score += 0.15

    # Preposition window — scan 3 words before entity in post text
    lower_text = post_text.lower()
    entity_lower = candidate.text.lower()
    idx = lower_text.find(entity_lower)
    if idx > 0:
        window = lower_text[max(0, idx - 40):idx].split()
        if any(w in PREPOSITION_SIGNALS for w in window[-3:]):
            score += 0.1

    # Co-occurrence — other geo entities in the same post reinforce confidence
    if len(all_entity_pairs) > 1:
        score += 0.1

    final = round(min(1.0, max(0.0, score)), 3)
    logger.debug("Score for '%s': %.3f", candidate.text, final)
    return final


def apply_confidence_threshold(candidates: list, post_text: str, all_entity_pairs: list) -> list:
    scored = []
    for c in candidates:
        c.confidence = score_candidate(c, post_text, all_entity_pairs)
        if c.confidence >= CONFIDENCE_THRESHOLD:
            scored.append(c)
        else:
            logger.debug("Dropped '%s' — confidence %.3f below threshold %.1f", c.text, c.confidence, CONFIDENCE_THRESHOLD)
    return scored
