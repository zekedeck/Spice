from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError, GeocoderUnavailable
import time
import logging

from models import LocationCandidate
from pipeline.nlp import build_geocoding_query

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


def init_geocoder(user_agent="geo-tagger-2/1.0"):
    return Nominatim(user_agent=user_agent, timeout=10)


def geocode_query(query: str, geocoder) -> dict | None:
    try:
        results = geocoder.geocode(
            query, addressdetails=True, language="en",
            exactly_one=False, limit=5,
        )
        if not results:
            return None
        # Prefer venue/landmark address types first, then break ties by importance score
        def _rank(r):
            atype = r.raw.get("type", r.raw.get("addresstype", ""))
            type_bonus = 1 if atype in _ADDRESS_TYPES else 0
            return (type_bonus, float(r.raw.get("importance", 0.0)))
        best = max(results, key=_rank)
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


def resolve_post_locations(text: str, entity_pairs: list, geocoder, all_entity_pairs: list = None) -> list:
    if not entity_pairs:
        return []

    if all_entity_pairs is None:
        all_entity_pairs = entity_pairs

    candidates = []
    for entity_text, label in entity_pairs:
        query, was_clarified = build_geocoding_query(entity_text, label, post_text=text, all_entity_pairs=all_entity_pairs)
        result = geocode_query(query, geocoder)
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
