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


@dataclass
class GeoPost:
    post_uri: str
    handle: str
    did: str
    text: str
    created_at: str
    post_url: str
    candidates: list = field(default_factory=list)
