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


def _query_tokens(s: str) -> set:
    words = re.findall(r"[a-z0-9]+", s.lower())
    return {w for w in words if w not in _STOPWORDS}


def _name_matches_query(query: str, display_name: str) -> bool:
    # display_name's first comma-separated segment is the matched entity's
    # own name (e.g. "Williamsburg Bridge" out of "Williamsburg Bridge,
    # Lower East Side, Manhattan, ..."). Every significant word in the
    # query must appear there -- extra words on the matched side are fine
    # (e.g. query "Grand Central" matching "Grand Central Terminal" should
    # still pass), but a missing query word means the match is likely
    # coincidental/wrong, not the actual place asked for.
    primary = display_name.split(",")[0]
    query_tokens = _query_tokens(query)
    if not query_tokens:
        return True
    primary_tokens = _query_tokens(primary)
    return query_tokens.issubset(primary_tokens)


def init_geocoder(user_agent="Spice/1.0 (NYC Bluesky geolocation map; contact: zeke.deck@gmail.com)"):
    return Nominatim(user_agent=user_agent, timeout=10)


def geocode_query(query: str, geocoder, conn) -> dict | None:
    cached = get_cached_geocode(conn, query)
    if cached is not None:
        cached_lat = float(cached["lat"])
        cached_lng = float(cached["lng"])
        if NYC_MIN_LAT <= cached_lat <= NYC_MAX_LAT and NYC_MIN_LNG <= cached_lng <= NYC_MAX_LNG:
            if _name_matches_query(query, cached["display_name"]):
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

        if not _name_matches_query(query, best.address):
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
