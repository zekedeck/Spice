"""Tests for pipeline.geocoder._name_matches_query.

Run from the repo root with:  python -m pytest tests/ -q

This guard exists because Nominatim's bounded=True force-matches the nearest
textually-similar result inside the NYC bbox even when the real place is far
away. The tests below pin down both halves of the contract:

  * it must NOT reject legitimate NYC matches (the false-reject class that
    emptied the map -- "New York City" is the most common query in this
    dataset), and
  * it must STILL reject the far-away class it was written for ("Seneca
    County", ~300mi upstate, matching "Seneca Avenue, Queens").
"""

import pytest

from pipeline.geocoder import _name_matches_query


# Realistic Nominatim display_name values, with the type/addresstype that
# Nominatim actually reports for each.
NEW_YORK_STATE = ("New York, United States", "state")
NEW_YORK_CITY = ("City of New York, New York, United States", "city")
SENECA_AVENUE = (
    "Seneca Avenue, Ridgewood, Queens, New York, United States",
    "residential",
)
GRAND_CENTRAL = (
    "Grand Central Terminal, East Midtown, Manhattan, New York County, "
    "City of New York, New York, 10017, United States",
    "station",
)
WILLIAMSBURG_BRIDGE = (
    "Williamsburg Bridge, Lower East Side, Manhattan, New York County, "
    "City of New York, New York, 10002, United States",
    "bridge",
)
TIMES_SQUARE = (
    "Times Square, Manhattan, New York County, City of New York, "
    "New York, 10036, United States",
    "square",
)
BROADWAY_BRONX = (
    "Broadway, The Bronx, Bronx County, New York, 10471, United States",
    "secondary",
)
PORTLAND_AVENUE = (
    "Portland Avenue, Fort Greene, Brooklyn, Kings County, "
    "City of New York, New York, 11205, United States",
    "residential",
)
PARK_SLOPE = (
    "Park Slope, Brooklyn, Kings County, City of New York, "
    "New York, United States",
    "suburb",
)


# --- the cases named in the brief -------------------------------------------

@pytest.mark.parametrize(
    "query, result, expected",
    [
        # False rejects that were silently emptying the map.
        ("New York City", NEW_YORK_STATE, True),
        ("NYC", NEW_YORK_STATE, True),
        # Must still block the far-away/bounded-search false-match class.
        ("Seneca County", SENECA_AVENUE, False),
        # Pre-existing intended behaviour: extra words on the matched side are
        # fine (documented in the function's own comment).
        ("Grand Central", GRAND_CENTRAL, True),
        ("Williamsburg Bridge", WILLIAMSBURG_BRIDGE, True),
        ("Times Square", TIMES_SQUARE, True),
    ],
)
def test_brief_cases(query, result, expected):
    display_name, address_type = result
    assert _name_matches_query(query, display_name, address_type) is expected


# --- NYC / New York alias normalization -------------------------------------

@pytest.mark.parametrize(
    "query",
    ["New York City", "new york city", "NYC", "N.Y.C.", "NY", "N.Y.",
     "New York", "New York State", "City of New York"],
)
def test_new_york_aliases_accepted_against_state_record(query):
    display_name, address_type = NEW_YORK_STATE
    assert _name_matches_query(query, display_name, address_type) is True


@pytest.mark.parametrize(
    "query", ["New York City", "NYC", "NY", "New York"],
)
def test_new_york_aliases_accepted_against_city_record(query):
    display_name, address_type = NEW_YORK_CITY
    assert _name_matches_query(query, display_name, address_type) is True


def test_new_york_city_accepted_even_with_unknown_address_type():
    """Alias normalization alone must carry this case, so it also works for
    legacy geocode_cache rows that have an empty address_type."""
    assert _name_matches_query("New York City", "New York, United States", "") is True


# --- the far-away / bounded-search false-match class must stay blocked ------

def test_seneca_county_rejected_with_unknown_address_type():
    display_name, _ = SENECA_AVENUE
    assert _name_matches_query("Seneca County", display_name, "") is False


def test_seneca_county_rejected_when_enriched_with_state():
    """build_geocoding_query appends a nearby US state, so the real query is
    often 'Seneca County, New York'. Still a reject."""
    display_name, address_type = SENECA_AVENUE
    assert _name_matches_query("Seneca County, New York", display_name, address_type) is False


def test_admin_suffix_is_not_dropped_for_non_admin_results():
    """'county' may only be dropped when the match is itself an admin-level
    place. A road named 'Seneca Avenue' is not, so 'Seneca County' stays
    rejected even though 'Seneca' alone would match."""
    display_name, _ = SENECA_AVENUE
    assert _name_matches_query("Seneca County", display_name, "road") is False
    assert _name_matches_query("Seneca County", display_name, "residential") is False


def test_bare_seneca_still_matches_seneca_avenue():
    """Sanity check that the Seneca reject comes from the 'county' token and
    not from something incidental."""
    display_name, address_type = SENECA_AVENUE
    assert _name_matches_query("Seneca", display_name, address_type) is True


@pytest.mark.parametrize(
    "query",
    ["Albany County", "Erie County", "Monroe County", "Seneca Falls"],
)
def test_other_upstate_queries_rejected_against_seneca_avenue(query):
    display_name, address_type = SENECA_AVENUE
    assert _name_matches_query(query, display_name, address_type) is False


def test_ny_ny_rejected_against_unrelated_bronx_road():
    """'NY, NY' matching a Broadway address in the Bronx is a correct reject:
    the core name 'NY' -> 'New York' does not match the road 'Broadway'."""
    display_name, address_type = BROADWAY_BRONX
    assert _name_matches_query("NY, NY", display_name, address_type) is False


def test_far_away_city_with_wrong_state_context_rejected():
    """Context segments are checked against the full display_name, so a
    far-away state can't be force-matched onto a same-named NYC street."""
    display_name, address_type = PORTLAND_AVENUE
    assert _name_matches_query("Portland, Oregon", display_name, address_type) is False


def test_trailing_admin_suffix_only_so_borough_park_is_not_loosened():
    """'borough' leads rather than trails in 'Borough Park', so it is never
    dropped -- otherwise 'Borough Park' could be loosened to 'Park' and match
    the suburb 'Park Slope'."""
    display_name, address_type = PARK_SLOPE
    assert _name_matches_query("Borough Park", display_name, address_type) is False


# --- clarified/enriched queries (the other silent false-reject class) -------

@pytest.mark.parametrize(
    "query, result",
    [
        ("Williamsburg Bridge, NY", WILLIAMSBURG_BRIDGE),
        ("Williamsburg Bridge, New York", WILLIAMSBURG_BRIDGE),
        ("Grand Central, NY", GRAND_CENTRAL),
        ("Grand Central, Manhattan", GRAND_CENTRAL),
        ("Times Square, New York", TIMES_SQUARE),
        ("Times Square, NYC", TIMES_SQUARE),
    ],
)
def test_state_or_gpe_enriched_queries_accepted(query, result):
    """build_geocoding_query appends ', <state>' or ', <co-occurring GPE>'.
    That context almost never appears in display_name's FIRST segment, so the
    old primary-segment-only check rejected nearly every clarified query."""
    display_name, address_type = result
    assert _name_matches_query(query, display_name, address_type) is True


def test_mismatched_borough_context_rejected():
    """Context that contradicts the match is still a reject."""
    display_name, address_type = GRAND_CENTRAL
    assert _name_matches_query("Grand Central, Brooklyn", display_name, address_type) is False


# --- degenerate input --------------------------------------------------------

@pytest.mark.parametrize("query", ["", "   ", "the", "of the"])
def test_queries_with_no_significant_tokens_are_accepted(query):
    display_name, address_type = TIMES_SQUARE
    assert _name_matches_query(query, display_name, address_type) is True


def test_missing_query_word_still_rejected():
    """The core guarantee of the original check: a significant query word that
    is absent from the matched name means the match is coincidental."""
    display_name, address_type = TIMES_SQUARE
    assert _name_matches_query("Herald Square", display_name, address_type) is False
