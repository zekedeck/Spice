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
class IngestedPost:
    """The ingestion contract every collector module writes through.

    A collector module (pipeline/collector.py's firehose listener,
    pipeline/collector_accountlist.py's REST poller, or any future
    source) builds one of these per candidate post and hands it to
    pipeline.database.insert_post(). Nothing downstream of the `posts`
    table -- pipeline/nlp.py, geocoder.py, llm_geotagging.py, scorer.py,
    worker.py -- cares which collector produced a row; they all operate
    purely on `text` and entities extracted from it. A new collector
    module only ever needs to conform to this contract; it never needs to
    touch downstream code.

    Fields:
      source: short string identifying which collector module wrote this
        row, e.g. "bluesky_firehose", "bluesky_accountlist". Stored
        verbatim in posts.source.
      author_id: a collector-module-specific stable identifier for the
        account that made the post (e.g. a Bluesky DID). Opaque to
        everything downstream -- never parsed or assumed to have any
        particular format by anything outside the collector that set it.
        Stored in posts.did (that column's name predates this generic
        contract; it is not Bluesky-specific in meaning).
      handle: human-readable handle/username for the author, used for
        display and for building post_url.
      post_uri: a collector-module-specific stable identifier for the
        post itself. Must be unique per post *within that source* --
        posts.post_uri is UNIQUE across the whole table. Does not need to
        be an AT-URI for non-Bluesky sources, just stable and unique.
      post_url: human-clickable URL for the post.
      text: the post's text content -- the only field anything
        downstream of the posts table actually reads.
      created_at: ISO-8601 timestamp string of when the post was
        created (not when it was collected).
    """

    source: str
    author_id: str
    handle: str
    post_uri: str
    post_url: str
    text: str
    created_at: str


@dataclass
class GeoPost:
    post_uri: str
    handle: str
    did: str
    text: str
    created_at: str
    post_url: str
    candidates: list = field(default_factory=list)
