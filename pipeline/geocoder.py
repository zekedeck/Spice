from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError, GeocoderUnavailable
import re
import time
import logging

from models import LocationCandidate
from pipeline.nlp import build_geocoding_query
from pipeline.database import get_cached_geocode, set_cached_geocode
from pipeline.llm_geotagging import resolve_entity_with_llm
from pipeline.collector import _NYC_KEYWORDS

logger = logging.getLogger(__name__)

_ADDRESS_TYPES = {
    "house", "road", "building", "amenity", "venue", "attraction",
    "tourism", "historic", "leisure", "place_of_worship", "park", "stadium",
    "square", "plaza", "monument", "memorial", "artwork", "viewpoint",
    "information", "picnic_site", "garden", "nature_reserve",
}
_CITY_TYPES = {
    "city", "town", "village", "suburb", "neighbourhood",
    "quarter", "borough", "municipality",
}
_STATE_TYPES = {
    "state", "region", "province", "administrative", "county",
}

NYC_MIN_LAT, NYC_MAX_LAT = 40.50, 40.92
NYC_MIN_LNG, NYC_MAX_LNG = -74.26, -73.70

_STOPWORDS = {"the", "of", "in", "at", "a", "an"}

# Name aliases, applied to both the query side and the matched-name side so the
# comparison is symmetric. Nominatim canonicalizes all of "New York City" /
# "NYC" / "NY" to the bare name "New York", so without this normalization every
# one of those queries is a false reject -- and "New York City" is by far the
# most frequent query in this dataset, so the old check fired on nearly every
# run. Longest patterns first; each is anchored on word boundaries.
_NAME_ALIASES = [
    (re.compile(r"\bcity of new york\b"), "new york"),
    (re.compile(r"\bnew york city\b"), "new york"),
    (re.compile(r"\bnew york state\b"), "new york"),
    (re.compile(r"\bn\.?y\.?c\.?\b"), "new york"),
    (re.compile(r"\bnyc\b"), "new york"),
    (re.compile(r"\bn\.?j\.?\b"), "new jersey"),
    (re.compile(r"\bn\.?y\.?\b"), "new york"),
]

# Administrative suffix words that describe an admin *level* rather than being
# part of a place's actual name ("Kings County", "New York City"). They may be
# dropped from the tail of a query only when the matched result is itself an
# administrative-level place (see _ADMIN_RESULT_TYPES) -- that is what keeps
# "Seneca County" from being reduced to "Seneca" and then matching the road
# "Seneca Avenue".
_ADMIN_SUFFIX_TOKENS = {
    "city", "county", "borough", "state", "province", "district",
    "parish", "municipality", "township",
}

_ADMIN_RESULT_TYPES = _CITY_TYPES | _STATE_TYPES | {"country"}


def _normalize_name(s: str) -> str:
    s = s.lower()
    for pattern, replacement in _NAME_ALIASES:
        s = pattern.sub(replacement, s)
    return s


def _query_tokens(s: str) -> set:
    words = re.findall(r"[a-z0-9]+", _normalize_name(s))
    return {w for w in words if w not in _STOPWORDS}


def _core_tokens_without_admin_suffix(core: str) -> set:
    """Token set for `core` with trailing administrative suffix words removed.

    Only *trailing* suffixes are dropped, and never the last remaining token.
    Trailing-only matters: "Borough Park" (a real Brooklyn neighbourhood) keeps
    both of its words, so it can't be loosened into matching "Park Slope".
    """
    words = [w for w in re.findall(r"[a-z0-9]+", _normalize_name(core))
             if w not in _STOPWORDS]
    while len(words) > 1 and words[-1] in _ADMIN_SUFFIX_TOKENS:
        words.pop()
    return set(words)


# Venue-type head nouns: a phrase ending in one of these, preceded by at
# least one modifier word, is treated as a specific venue-class compound
# ("Times Square", "Brooklyn Navy Yard", "Grand Central Terminal").
_VENUE_HEAD_NOUNS = {
    "square", "hall", "yard", "yards", "garden", "gardens", "bridge",
    "park", "statue", "terminal", "center", "centre", "stadium",
    "theater", "theatre", "museum", "plaza", "tower", "building",
    "station", "arena", "pier", "zoo", "aquarium", "library",
    "cathedral", "church", "temple", "palace", "market", "hotel",
}

# Bare jurisdiction/admin terms that should score low on their own, even
# though several of them (the boroughs) also appear in the NYC gazetteer
# below -- a bare borough name alone is not a specific pin-worthy place.
# Built from _normalize_name's own alias targets plus the boroughs, so
# "NYC"/"New York City"/"N.Y." all collapse the same way they do elsewhere
# in this module.
_BARE_JURISDICTION_TERMS = {
    "new york", "new jersey", "manhattan", "brooklyn", "queens", "bronx",
    "staten island",
}


def specificity(entity_phrase: str) -> float:
    """Pure-text specificity score for a geocoding entity phrase (no network
    or DB dependency). A future scoring signal only -- not wired into any
    accept/reject decision.

    Tiers (low to high):
      - 0.1: a bare jurisdiction/admin term ("New York", "NYC", "Brooklyn",
        "Manhattan" alone), reusing the same alias normalization and
        admin-suffix token list _name_matches_query already relies on
        rather than a second hand-rolled list.
      - 0.15 / 0.3: unrecognized phrase (single-word / multi-word), treated
        conservatively -- more specific than a bare jurisdiction but not a
        confirmed toponym or venue.
      - 0.5: a recognized specific-but-non-venue NYC toponym (neighbourhood
        or place name) via pipeline.collector's own gazetteer, e.g.
        "Astoria", "Bushwick", "Marble Hill".
      - 0.9: a venue-class compound -- a modifier plus a venue-type head
        noun, e.g. "Times Square", "Brooklyn Navy Yard", "City Hall".
    """
    normalized = _normalize_name(entity_phrase).strip()
    tokens = [w for w in re.findall(r"[a-z0-9']+", normalized) if w not in _STOPWORDS]

    if not tokens:
        return 0.0

    if normalized in _BARE_JURISDICTION_TERMS:
        return 0.1
    if len(tokens) > 1 and tokens[-1] in _ADMIN_SUFFIX_TOKENS:
        return 0.1

    if len(tokens) >= 2 and tokens[-1] in _VENUE_HEAD_NOUNS:
        return 0.9

    if normalized in _NYC_KEYWORDS:
        return 0.5

    return 0.3 if len(tokens) > 1 else 0.15


def _name_matches_query(query: str, display_name: str, address_type: str = "") -> tuple:
    """Guard against Nominatim's bounded=True force-matching the nearest
    textually-similar result inside the NYC bbox when the real place is far
    away (e.g. "Seneca County", ~300mi upstate, resolving to "Seneca Avenue,
    Queens").

    The query is split on commas: the first segment is the place name proper,
    and any later segments are *context* appended by build_geocoding_query or
    by the LLM "query" action (a nearby US state, a co-occurring GPE).

    - The core name must match the matched result's own name, which is
      display_name's first comma-separated segment (e.g. "Williamsburg Bridge"
      out of "Williamsburg Bridge, Lower East Side, Manhattan, ..."). Extra
      words on the matched side are fine, so query "Grand Central" still
      matches "Grand Central Terminal"; a missing query word means the match is
      probably coincidental.
    - Context segments are checked against the *whole* display_name rather than
      just its first segment, because that is where context like "New York" or
      "Manhattan" actually appears. Requiring context words in the first
      segment (the old behaviour) rejected essentially every clarified query,
      e.g. "Williamsburg Bridge, NY". Checking them against the full name still
      blocks the far-away class: "Portland, Oregon" cannot match
      "Portland Avenue, Brooklyn, New York, ...".
    - `address_type` is the matched result's Nominatim type/addresstype. When it
      is an administrative level (city/town/borough/county/state/...), a
      trailing admin suffix on the query is treated as satisfied by the result
      itself instead of having to appear literally in its name -- so
      "New York City" may match the city/state record named just "New York".
      When the type is unknown or non-administrative we do not loosen anything,
      which is what preserves the Seneca-style reject.

    Returns (matches, ratio): `matches` is the original boolean contract
    (unchanged); `ratio` is a continuous core-token overlap ratio -- the
    fraction of the query's own core tokens that are present in the matched
    result's primary-segment name (1.0 for a clean match, lower for a
    partial/coincidental one) -- exposed as a future scoring signal, not a
    new rejection.
    """
    segments = query.split(",")
    core = segments[0]
    context = segments[1:]

    core_tokens = _query_tokens(core)
    if not core_tokens:
        return True, 1.0

    primary_tokens = _query_tokens(display_name.split(",")[0])

    def _overlap_ratio(tokens: set) -> float:
        if not tokens:
            return 1.0
        return len(tokens & primary_tokens) / len(tokens)

    ratio = _overlap_ratio(core_tokens)
    core_ok = ratio >= 1.0

    if not core_ok and (address_type or "").lower() in _ADMIN_RESULT_TYPES:
        loosened_tokens = _core_tokens_without_admin_suffix(core)
        loosened_ratio = _overlap_ratio(loosened_tokens)
        if loosened_ratio >= 1.0:
            core_ok = True
            ratio = loosened_ratio
        else:
            ratio = max(ratio, loosened_ratio)

    if not core_ok:
        return False, ratio

    full_tokens = _query_tokens(display_name)
    for segment in context:
        segment_tokens = _query_tokens(segment)
        if segment_tokens and not segment_tokens.issubset(full_tokens):
            return False, ratio

    return True, ratio


def init_geocoder(user_agent="Spice/1.0 (NYC Bluesky geolocation map; contact: zeke.deck@gmail.com)"):
    return Nominatim(user_agent=user_agent, timeout=10)


def geocode_query(query: str, geocoder, conn) -> dict | None:
    cached = get_cached_geocode(conn, query)
    if cached is not None:
        cached_lat = float(cached["lat"])
        cached_lng = float(cached["lng"])
        if NYC_MIN_LAT <= cached_lat <= NYC_MAX_LAT and NYC_MIN_LNG <= cached_lng <= NYC_MAX_LNG:
            cached_matches, _cached_ratio = _name_matches_query(
                query, cached["display_name"], cached.get("address_type", "")
            )
            if cached_matches:
                return cached
    try:
        results = geocoder.geocode(
            query, addressdetails=True, language="en",
            exactly_one=False, limit=5,
            viewbox=[(NYC_MAX_LAT, NYC_MIN_LNG), (NYC_MIN_LAT, NYC_MAX_LNG)],
            bounded=True,
        )
        if not results:
            return None
        # Prefer venue/landmark address types first, then break ties by importance score
        def _rank(r):
            atype = r.raw.get("type", r.raw.get("addresstype", ""))
            type_bonus = 1 if atype in _ADDRESS_TYPES else 0
            address = r.raw.get("address", {})
            country_code = address.get("country_code", "").lower()
            us_bonus = 1 if country_code == "us" else 0
            importance = float(r.raw.get("importance", 0.0))
            return (type_bonus + us_bonus, importance)
        best = max(results, key=_rank)

        lat = float(best.latitude)
        lng = float(best.longitude)

        # Null Island
        if lat == 0.0 and lng == 0.0:
            logger.warning("Null Island result rejected for query %r", query)
            return None

        # Basic range sanity
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            logger.warning("Out-of-range coordinates rejected for query %r: %s, %s", query, lat, lng)
            return None

        if not (NYC_MIN_LAT <= lat <= NYC_MAX_LAT and NYC_MIN_LNG <= lng <= NYC_MAX_LNG):
            logger.warning("Outside NYC bbox rejected for query %r: %s, %s", query, lat, lng)
            return None

        # Importance floor
        if float(best.raw.get("importance", 0.0)) < 0.02:
            logger.warning("Importance below floor rejected for query %r", query)
            return None

        best_type = best.raw.get("type", best.raw.get("addresstype", ""))
        name_matches, name_match_ratio = _name_matches_query(query, best.address, best_type)
        if not name_matches:
            logger.warning(
                "Query/result name mismatch rejected for query %r: matched %r (match_ratio=%.2f)",
                query, best.address, name_match_ratio,
            )
            return None

        set_cached_geocode(conn, query, lat, lng, best.address, best.raw.get("type", best.raw.get("addresstype", "")), float(best.raw.get("importance", 0.0)))

        return {
            "lat": float(best.latitude),
            "lng": float(best.longitude),
            "display_name": best.address,
            "address_type": best.raw.get("type", best.raw.get("addresstype", "")),
            "importance": float(best.raw.get("importance", 0.0)),
            "raw": best.raw,
        }
    except (GeocoderTimedOut, GeocoderServiceError, GeocoderUnavailable) as e:
        logger.warning("Geocoding failed for query %r: %s", query, e)
        return None


def assign_precision(address_type: str, was_clarified: bool) -> str:
    if address_type in _ADDRESS_TYPES:
        return "address (clarified)" if was_clarified else "address"
    if address_type in _CITY_TYPES:
        return "city"
    if address_type in _STATE_TYPES:
        return "state"
    if address_type == "country":
        return "country"
    # Previously silently fell through to "city", which conflated a real
    # but unrecognized POI/address type (not yet in _ADDRESS_TYPES) with an
    # actual city-level admin-boundary match. Give it its own explicit,
    # clearly-named outcome instead so the two cases are never
    # indistinguishable downstream.
    return "unclassified"


# NYC's five boroughs, as named in post text / extracted entities.
_BOROUGH_NAMES = {"manhattan", "brooklyn", "queens", "bronx", "staten island"}

# Nominatim is NOT consistent about which `address` field carries a NYC
# result's county-level admin unit: empirically (checked live against
# nominatim.openstreetmap.org on 2026-10-13) it comes back as `county` for
# some boroughs (Bronx, Queens) and as `city_district` for others
# (Manhattan, Brooklyn), and is sometimes omitted entirely (Staten Island),
# with the borough's own name then only available via `suburb`. All three
# fields are checked, in order, below -- a single hand-picked field name
# would silently miss real results.
_COUNTY_NAME_TO_BOROUGH = {
    "new york county": "manhattan",
    "kings county": "brooklyn",
    "queens county": "queens",
    "bronx county": "bronx",
    "richmond county": "staten island",
}


def _resolved_borough(raw: dict | None) -> str | None:
    """Best-effort borough implied by a Nominatim result's own `address`
    breakdown, or None if it can't be determined (e.g. no raw data at all,
    as with an LLM "resolve" candidate that never queried Nominatim)."""
    if not raw:
        return None
    address = raw.get("address") or {}
    for key in ("county", "city_district", "suburb"):
        value = (address.get(key) or "").strip().lower()
        if not value:
            continue
        if value in _COUNTY_NAME_TO_BOROUGH:
            return _COUNTY_NAME_TO_BOROUGH[value]
        normalized = "bronx" if value == "the bronx" else value
        if normalized in _BOROUGH_NAMES:
            return normalized
    return None


def _mentioned_boroughs(entity_text: str, all_entity_pairs: list) -> set:
    """Boroughs named by *other* extracted entities from the same post
    (never the candidate's own entity text, so a candidate that IS a
    borough name never conflicts with itself)."""
    mentioned = set()
    for other_text, _label in all_entity_pairs:
        if other_text.strip().lower() == entity_text.strip().lower():
            continue
        lowered = other_text.lower()
        if "the bronx" in lowered:
            mentioned.add("bronx")
        for borough in _BOROUGH_NAMES:
            if borough in lowered:
                mentioned.add(borough)
    return mentioned


def _detect_borough_conflict(entity_text: str, all_entity_pairs: list, raw: dict | None) -> bool:
    """A signal, not a rejection: True when some other entity extracted from
    the same post names a NYC borough that differs from the one Nominatim's
    own address breakdown implies for this resolved result."""
    resolved = _resolved_borough(raw)
    if resolved is None:
        return False
    mentioned = _mentioned_boroughs(entity_text, all_entity_pairs)
    mentioned.discard(resolved)
    return bool(mentioned)


def resolve_post_locations(text: str, entity_pairs: list, geocoder, conn, all_entity_pairs: list = None, llm_client=None) -> list:
    if not entity_pairs:
        return []

    if all_entity_pairs is None:
        all_entity_pairs = entity_pairs

    candidates = []
    for entity_text, label in entity_pairs:
        llm_result = None
        if llm_client is not None:
            llm_result = resolve_entity_with_llm(llm_client, entity_text, label, text, conn)

        if llm_result is not None and llm_result.get("action") == "null":
            time.sleep(1)
            continue

        if llm_result is not None and llm_result.get("action") == "resolve":
            lat = llm_result["lat"]
            lng = llm_result["lng"]
            if NYC_MIN_LAT <= lat <= NYC_MAX_LAT and NYC_MIN_LNG <= lng <= NYC_MAX_LNG:
                candidate = LocationCandidate(
                    text=entity_text,
                    query_sent=entity_text,
                    lat=lat,
                    lng=lng,
                    precision="address",
                    display_name=entity_text,
                    source_label=label,
                    # No Nominatim query ever happens on this path, so there
                    # is no real importance score to report. Leaving this at
                    # the dataclass's neutral/zero default (rather than the
                    # old hardcoded 1.0) avoids a future scorer mistaking
                    # fabricated data for a genuine Nominatim importance;
                    # provenance below is how that scorer should instead
                    # recognize and weight this tier.
                    provenance="llm_resolve",
                    specificity_score=specificity(entity_text),
                )
                candidates.append(candidate)
            else:
                logger.warning("LLM-resolved coordinates outside NYC bbox rejected for entity %r: %s, %s", entity_text, lat, lng)
            time.sleep(1)
            continue

        if llm_result is not None and llm_result.get("action") == "query":
            query = llm_result["query"]
            was_clarified = True
            provenance = "llm_query"
        elif llm_result is not None and llm_result.get("action") == "accept":
            query = entity_text
            was_clarified = False
            provenance = "llm_accept"
        else:
            query, was_clarified = build_geocoding_query(entity_text, label, post_text=text, all_entity_pairs=all_entity_pairs)
            provenance = "rule"

        result = geocode_query(query, geocoder, conn)
        if result:
            precision = assign_precision(result["address_type"], was_clarified)
            candidate = LocationCandidate(
                text=entity_text,
                query_sent=query,
                lat=result["lat"],
                lng=result["lng"],
                precision=precision,
                display_name=result["display_name"],
                source_label=label,
                importance=result["importance"],
                address_type=result["address_type"],
                provenance=provenance,
                borough_conflict=_detect_borough_conflict(entity_text, all_entity_pairs, result.get("raw")),
                specificity_score=specificity(entity_text),
            )
            candidates.append(candidate)
        time.sleep(1)

    return candidates
