import re
import unicodedata
import spacy
import logging

logger = logging.getLogger(__name__)

# US state names for geocoding context enrichment
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
}
_US_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


def strip_emojis(text: str) -> str:
    return "".join(c for c in text if unicodedata.category(c) != "So").strip()


def has_emoji(text: str) -> bool:
    return any(unicodedata.category(c) == "So" for c in text)


def load_nlp_model():
    return spacy.load("en_core_web_sm")


def extract_emoji_locations(text: str) -> list:
    seen = {}
    patterns = [
        r"📍[^📍\n]*?(\d+\s+\w+[\w\s]+(?:Ave|St|Blvd|Rd|Dr|Ln|Way|Pl|Sq|Square|Avenue|Street|Boulevard|Road|Drive|Lane|Place)[^\n]*)",
        r"📍:?\s*([^\n]+)",
        r"📍[^(]*\(([^)]+)\)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            val = match.group(1).strip()
            if val and val not in seen:
                seen[val] = "EMOJI"
    return [(k, "EMOJI") for k in seen]


def _leftmost_modifier_index(token) -> int:
    """Walk left absorbing compound/amod/nmod children to find true start of noun phrase."""
    start = token.i
    for child in token.lefts:
        if child.dep_ in {"compound", "amod", "nmod", "npadvmod", "poss", "nummod"}:
            start = min(start, _leftmost_modifier_index(child))
    return start


def expand_entity(ent) -> str:
    """
    Expand entity span using dependency tree to capture full name.
    Example: spaCy tags 'Air Force' but dependency tree has 'Texas' as compound →
    returns 'Texas Air Force base'.
    """
    doc = ent.doc
    sent = ent.sent
    new_start = _leftmost_modifier_index(ent.root)

    # Also walk right for trailing head nouns (e.g. 'base' in 'Air Force base')
    new_end = ent.end
    for child in ent.root.rights:
        if child.dep_ in {"compound", "nmod"} and child.pos_ in {"NOUN", "PROPN"}:
            new_end = max(new_end, child.i + 1)

    # Clamp to sentence boundaries and never shrink original span
    new_start = max(min(new_start, ent.start), sent.start)
    new_end = min(max(new_end, ent.end), sent.end)

    return doc[new_start:new_end].text.strip()


def extract_locations(doc_text: str, nlp) -> tuple:
    """
    Returns (entity_pairs, doc).
    entity_pairs is a list of (text, label) with dependency-tree span expansion applied.
    """
    doc = nlp(doc_text)
    seen = set()
    result = []

    _SPATIAL_PREPS = {"at", "in", "near", "outside", "visiting", "inside", "around"}
    _MEASUREMENT_RE = re.compile(r'^\d[\d.,]*\s*(km|mi|miles|m|ft|feet|meters|yards|yd)$', re.IGNORECASE)

    for ent in doc.ents:
        label = ent.label_

        if label not in {"GPE", "LOC", "FAC", "ORG"}:
            continue
        if ent.text in seen:
            continue
        seen.add(ent.text)

        # Skip measurement strings like "15.2 km", "500 meters"
        if _MEASUREMENT_RE.match(ent.text.strip()):
            logger.debug("Skipping measurement entity: %r", ent.text)
            continue

        # ORG entities: only include when directly preceded by a spatial preposition
        # (e.g. "at Texas Air Force", "at Fillmore East")
        if label == "ORG":
            prev_tokens = [doc[i].lower_ for i in range(max(0, ent.start - 3), ent.start)]
            if not any(t in _SPATIAL_PREPS for t in prev_tokens):
                continue
            label = "FAC"  # treat as facility for downstream processing

        # Attempt span expansion via dependency tree
        expanded = expand_entity(ent)
        cleaned = strip_emojis(expanded) if expanded else strip_emojis(ent.text)
        if not cleaned:
            continue

        result.append((cleaned, label))

    return result, doc


def _extract_nearby_us_state(entity_text: str, post_text: str) -> str:
    """Find a US state name or abbreviation within ±120 chars of entity_text in post_text."""
    lower_post = post_text.lower()
    idx = lower_post.find(entity_text.lower())
    if idx < 0:
        return ""
    start = max(0, idx - 120)
    end = min(len(post_text), idx + len(entity_text) + 120)
    window_lower = lower_post[start:end]
    window_orig = post_text[start:end]

    for state in _US_STATES:
        if re.search(r'\b' + re.escape(state) + r'\b', window_lower):
            return state.title()

    for abbrev in _US_STATE_ABBREVS:
        if re.search(r'\b' + re.escape(abbrev) + r'\b', window_orig):
            return abbrev

    return ""


def _extract_co_entity_gpe(entity_text: str, all_entity_pairs: list) -> str:
    """Return a co-occurring GPE entity from the same post (skip entity itself)."""
    if not all_entity_pairs:
        return ""
    for text, label in all_entity_pairs:
        if label == "GPE" and text.lower() != entity_text.lower():
            return text
    return ""


def build_geocoding_query(
    entity_text: str,
    entity_label: str,
    post_text: str = "",
    all_entity_pairs: list = None,
) -> tuple:
    """
    Build a geocoding query, optionally enriched with geographic context.
    Priority: nearby US state → co-entity GPE → bare entity text.
    Returns (query_string, was_clarified).
    """
    if all_entity_pairs is None:
        all_entity_pairs = []

    # Priority 1: US state mentioned near the entity in post text
    if post_text and entity_label != "GPE":
        nearby_state = _extract_nearby_us_state(entity_text, post_text)
        if nearby_state:
            logger.debug("Enriching query %r with nearby state: %s", entity_text, nearby_state)
            return (f"{entity_text}, {nearby_state}", True)

    # Priority 2: co-occurring GPE entity in the same post
    if entity_label != "GPE":
        co_gpe = _extract_co_entity_gpe(entity_text, all_entity_pairs)
        if co_gpe:
            logger.debug("Enriching query %r with co-entity GPE: %s", entity_text, co_gpe)
            return (f"{entity_text}, {co_gpe}", True)

    return (entity_text, False)
