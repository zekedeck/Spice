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

# A country/state/region name (GPE) appearing anywhere in a post alongside
# one of these is almost certainly being discussed as a world-events
# SUBJECT ("Israel's genocide", "what is happening in Palestine a
# genocide", "crimes against the Palestinian people"), not describing
# where a NYC-area event is physically taking place. Checked as a plain
# substring anywhere in the post (not phrase-order-dependent like
# SUBJECT_SIGNALS below, and not limited to a window around the entity)
# specifically because real-world phrasing varies too much to pin down
# as a fixed "noun BEFORE/AFTER preposition" pattern -- e.g. "genocide in
# Palestine" and "in Palestine a genocide" both describe the exact same
# non-spatial reference, just with the words in a different order.
GPE_POLITICAL_CONTEXT_SIGNALS = [
    "genocide", "war crime", "war criminal", "crimes against",
    "ethnic cleansing", "apartheid", "complicit", "complicity",
    "accountability", "atrocit", "massacre", "occupation", "occupied",
    "colonial", "propagand", "ceasefire", "zionist",
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

    # Hard venue signal → accept. Excludes GPE (country/state/city names):
    # these signals are checked over a wide ±150-char window, not tied to
    # THIS specific entity, so a post organizing a real protest ("📍 outside
    # the NYT building") would otherwise bless every other entity mentioned
    # nearby too -- including a country name used only in political
    # commentary in the same short post (e.g. "Israel is starving the
    # people of Gaza" next to an unrelated "📍" pointing at the actual venue
    # elsewhere). GPE instead falls through to the stricter checks below.
    if entity_label != "GPE" and any(sig in window for sig in VENUE_SIGNALS):
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
        if any(sig in post_lower for sig in GPE_POLITICAL_CONTEXT_SIGNALS):
            logger.debug("Political-context signal suppressed GPE: %r", entity_text)
            return False
        # Same check, but for "genocide" specifically obfuscated to dodge
        # platform content moderation (seen in real data: "gen•cide",
        # "genøcide", "gen/ cide") -- a plain substring match above
        # misses these. "ø" (o-with-stroke) is itself a Unicode letter,
        # so \W wouldn't match it -- match ANY 0-3 characters between "gen"
        # and "cide" instead.
        if re.search(r"gen.{0,3}cide", post_lower):
            logger.debug("Obfuscated genocide signal suppressed GPE: %r", entity_text)
            return False
        if match and match.start() > 0 and post_text[match.start() - 1] == "-":
            # "Pro-Palestine protest", "Anti-Israel rally": the GPE name is
            # part of a hyphenated modifier describing the protest's CAUSE,
            # not a location reference at all -- spaCy's NER still spans
            # just "Palestine"/"Israel" here, dropping the "Pro-"/"Anti-"
            # prefix that changes its role in the sentence entirely.
            prefix = post_text[max(0, match.start() - 5):match.start() - 1].lower()
            if prefix.endswith("pro") or prefix.endswith("anti"):
                return False
        if match:
            # A country/state/city name used possessively ("Israel's
            # genocide", "Russia's military") is a political-actor
            # reference, not a place being visited -- reject regardless of
            # any preposition nearby, before the preposition check below
            # gets a chance to wrongly accept it via "...role IN Israel's...".
            after = post_text[match.end():match.end() + 3]
            if after[:1] in ("'", "’") and after[1:2].lower() == "s":
                return False
        # Otherwise accept GPE if a spatial preposition appears directly
        # before the entity (e.g. "protest at Times Square").
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
