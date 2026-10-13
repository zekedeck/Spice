from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError, GeocoderUnavailable
import re
import time
import logging

from models import LocationCandidate
from pipeline.nlp import build_geocoding_query
from pipeline.database import get_cached_geocode, set_cached_geocode
from pipeline.llm_geotagging import resolve_entity_with_llm

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


def _name_matches_query(query: str, display_name: str, address_type: str = "") -> bool:
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
    """
    segments = query.split(",")
    core = segments[0]
    context = segments[1:]

    core_tokens = _query_tokens(core)
    if not core_tokens:
        return True

    primary_tokens = _query_tokens(display_name.split(",")[0])
    core_ok = core_tokens.issubset(primary_tokens)

    if not core_ok and (address_type or "").lower() in _ADMIN_RESULT_TYPES:
        core_ok = _core_tokens_without_admin_suffix(core).issubset(primary_tokens)

    if not core_ok:
        return False

    full_tokens = _query_tokens(display_name)
    for segment in context:
        segment_tokens = _query_tokens(segment)
        if segment_tokens and not segment_tokens.issubset(full_tokens):
            return False

    return True


def init_geocoder(user_agent="Spice/1.0 (NYC Bluesky geolocation map; contact: zeke.deck@gmail.com)"):
    return Nominatim(user_agent=user_agent, timeout=10)


def geocode_query(query: str, geocoder, conn) -> dict | None:
    cached = get_cached_geocode(conn, query)
    if cached is not None:
        cached_lat = float(cached["lat"])
        cached_lng = float(cached["lng"])
        if NYC_MIN_LAT <= cached_lat <= NYC_MAX_LAT and NYC_MIN_LNG <= cached_lng <= NYC_MAX_LNG:
            if _name_matches_query(query, cached["display_name"], cached.get("address_type", "")):
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
        if not _name_matches_query(query, best.address, best_type):
            logger.warning("Query/result name mismatch rejected for query %r: matched %r", query, best.address)
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
    return "city"


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
                    importance=1.0,
                )
                candidates.append(candidate)
            else:
                logger.warning("LLM-resolved coordinates outside NYC bbox rejected for entity %r: %s, %s", entity_text, lat, lng)
            time.sleep(1)
            continue

        if llm_result is not None and llm_result.get("action") == "query":
            query = llm_result["query"]
            was_clarified = True
        elif llm_result is not None and llm_result.get("action") == "accept":
            query = entity_text
            was_clarified = False
        else:
            query, was_clarified = build_geocoding_query(entity_text, label, post_text=text, all_entity_pairs=all_entity_pairs)

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
            )
            candidates.append(candidate)
        time.sleep(1)

    return candidates
