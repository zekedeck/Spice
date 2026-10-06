import math
import logging

logger = logging.getLogger(__name__)


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def filter_by_radius(candidates: list, anchor_center: tuple, radius_miles: float) -> list:
    if not candidates:
        return []

    anchor_lat, anchor_lng = anchor_center
    filtered = []

    for c in candidates:
        dist = haversine_miles(anchor_lat, anchor_lng, c.lat, c.lng)
        if dist <= radius_miles:
            filtered.append(c)
        else:
            logger.debug("Discarding '%s' — %.1f miles from anchor (limit: %.1f)", c.text, dist, radius_miles)

    logger.info("Radius filter: %d/%d candidates within %.1f miles", len(filtered), len(candidates), radius_miles)
    return filtered
