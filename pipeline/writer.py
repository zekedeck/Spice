import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def post_to_features(post) -> list:
    total = len(post.candidates)
    features = []

    for index, candidate in enumerate(post.candidates, start=1):
        post_label = f"{post.post_url} ({index} of {total})"
        other_locations = ", ".join(
            c.text for c in post.candidates if c.text != candidate.text
        )

        properties = {
            "post_uri": post.post_uri,
            "post_label": post_label,
            "handle": post.handle,
            "did": post.did,
            "text": post.text,
            "created_at": post.created_at,
            "source": post.post_url,
            "mapped_location": candidate.display_name,
            "precision_level": candidate.precision,
            "location_text": candidate.text,
            "confidence": candidate.confidence,
        }

        if other_locations:
            properties["other_locations"] = other_locations

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [candidate.lng, candidate.lat],
            },
            "properties": properties,
        })

    return features


def build_feature_collection(geo_posts: list, anchor_address: str = "", anchor_resolved: str = "", radius_miles: float = 0.0) -> dict:
    features = []
    for post in geo_posts:
        features.extend(post_to_features(post))

    return {
        "type": "FeatureCollection",
        "properties": {
            "anchor_address": anchor_address,
            "anchor_resolved": anchor_resolved,
            "radius_miles": radius_miles,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "total_pins": len(features),
        },
        "features": features,
    }


def write_geojson(collection: dict, output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(collection, f, indent=2, ensure_ascii=False)
    logger.info("Wrote %d features to %s", len(collection["features"]), output_path)
