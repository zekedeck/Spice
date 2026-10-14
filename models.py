from dataclasses import dataclass, field


@dataclass
class LocationCandidate:
    text: str
    query_sent: str
    lat: float
    lng: float
    precision: str
    display_name: str
    source_label: str = ""
    importance: float = 0.0
    confidence: float = 0.0
    # Raw Nominatim type/addresstype for this match (not the derived
    # precision bucket from assign_precision) -- e.g. "park", "residential".
    # Empty for candidates that never went through Nominatim (llm_resolve).
    address_type: str = ""
    # Which path in resolve_post_locations produced this candidate: one of
    # "rule", "llm_resolve", "llm_query", "llm_accept". Lets a future scorer
    # treat each tier on its own terms instead of conflating them.
    provenance: str = ""
    # True when another entity extracted from the same post names a
    # different NYC borough than the one implied by this result's own
    # Nominatim address breakdown. A signal for future scoring, not a
    # rejection.
    borough_conflict: bool = False
    # Pure-text specificity score for the entity phrase (see
    # pipeline.geocoder.specificity): low for a bare jurisdiction/admin
    # term, mid for a recognized NYC toponym, high for a venue-class
    # compound.
    specificity_score: float = 0.0


@dataclass
class GeoPost:
    post_uri: str
    handle: str
    did: str
    text: str
    created_at: str
    post_url: str
    candidates: list = field(default_factory=list)
