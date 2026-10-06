import re
import logging

logger = logging.getLogger(__name__)

# Post-level signals: if ANY of these appear anywhere in the post text,
# it is almost certainly a music-bot/now-playing post → suppress all locations.
MUSIC_BOT_SIGNALS = [
    "now playing", "#nowplaying", "np:", "🎵", "🎶", "🎧",
    "now spinning", "currently playing",
]

# Window-level signals: checked within ±150 chars of the entity.
# A match here reduces confidence that the entity is a physical location.
MUSIC_CONTEXT_SIGNALS = [
    "feat.", "featuring", "track from", "tracks from", "song is called",
    "song called", "new music from", "single from", "from the album",
    "produced by", "off the album",
]

MEDIA_SIGNALS = [
    "easter egg", "video game", "the game", "in games", "in the game",
    "escape velocity", "animated", "cartoon", "documentary",
    "netflix", "disney", "hulu", "streaming", "box office",
    "trailer", "screenplay", "episode", "sequel", "prequel",
]

VENUE_SIGNALS = [
    "join us", "rally", "protest at", "march to", "march from",
    "gather", "vigil", "demonstration", "action at", "action announcement",
    "come to", "see you at", "meet us at", "we will be at", "we are at",
    "📍", "location:", "where:", "address:", "meet at", "protest outside",
    "march on", "assemble at", "outside the", "in front of",
]

SUBJECT_SIGNALS = [
    "killed in", "killed by", "occupied", "invaded", "bombing in",
    "war in", "genocide in", "massacre in", "attack in", "airstrike",
    "martyred in", "died in", "murdered in", "slaughtered in",
    "has killed", "have killed", "is killing", "are killing",
    "movie", "film", "series",
    "the song", "song called", "called gold",
]


def _get_sentence_for_entity(entity_text: str, doc) -> str:
    """Return the sentence text containing the entity, or empty string."""
    if doc is None:
        return ""
    lower_text = doc.text.lower()
    idx = lower_text.find(entity_text.lower())
    if idx < 0:
        return ""
    for sent in doc.sents:
        if sent.start_char <= idx < sent.end_char:
            return sent.text.lower()
    return ""


def is_venue_mention(entity_text: str, post_text: str, entity_label: str, doc=None) -> bool:
    post_lower = post_text.lower()

    # Music-bot check must fire before any label-based early returns (including EMOJI)
    if any(sig in post_lower for sig in MUSIC_BOT_SIGNALS):
        logger.debug("Music-bot signal suppressed: %r", entity_text)
        return False

    # Build window around entity (±150 chars)
    match = re.search(re.escape(entity_text), post_text, re.IGNORECASE)
    if match:
        start = max(0, match.start() - 150)
        end = min(len(post_text), match.end() + 150)
        window = post_text[start:end].lower()
    else:
        window = post_lower

    # Music context in window → suppress
    if any(sig in window for sig in MUSIC_CONTEXT_SIGNALS):
        logger.debug("Music-context signal suppressed: %r", entity_text)
        return False

    # Media/game context in window → suppress
    if any(sig in window for sig in MEDIA_SIGNALS):
        logger.debug("Media-context signal suppressed: %r", entity_text)
        return False

    # Hard venue signal → accept
    if any(sig in window for sig in VENUE_SIGNALS):
        return True

    # Subject signal — check same sentence first (stronger penalty) via doc
    entity_sentence = _get_sentence_for_entity(entity_text, doc)
    if entity_sentence:
        if any(sig in entity_sentence for sig in SUBJECT_SIGNALS):
            logger.debug("Subject signal (same sentence) suppressed: %r", entity_text)
            return False
    else:
        # Fallback: check window without sentence boundary
        if any(sig in window for sig in SUBJECT_SIGNALS):
            return False

    if entity_label == "EMOJI":
        return True

    if entity_label == "GPE":
        # Accept GPE if a spatial preposition appears directly before the entity
        SPATIAL_PREPS = {"at", "in", "near", "outside", "visiting", "around", "inside"}
        if match:
            pre_window = post_text[max(0, match.start() - 40):match.start()].lower().split()
            if any(w in SPATIAL_PREPS for w in pre_window[-3:]):
                return True
        return False

    return entity_label in ("FAC", "EMOJI")


def filter_venue_locations(entity_pairs: list, post_text: str, doc=None) -> list:
    result = []
    for entity_text, entity_label in entity_pairs:
        if is_venue_mention(entity_text, post_text, entity_label, doc=doc):
            result.append((entity_text, entity_label))
        else:
            logger.debug("Discarded entity: %r (label=%s)", entity_text, entity_label)
    return result
