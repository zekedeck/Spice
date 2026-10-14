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

from unittest.mock import patch

import pytest

from pipeline.geocoder import (
    _name_matches_query,
    assign_precision,
    specificity,
    _detect_borough_conflict,
    resolve_post_locations,
)


def _matches(query, display_name, address_type=""):
    """Thin test-local helper: _name_matches_query now returns (matches,
    ratio) instead of a bare bool (see test_name_match_ratio below for
    direct ratio coverage). This keeps every pre-existing boolean-outcome
    assertion in this file unchanged under the new signature."""
    matches, _ratio = _name_matches_query(query, display_name, address_type)
    return matches


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
    assert _matches(query, display_name, address_type) is expected


# --- NYC / New York alias normalization -------------------------------------

@pytest.mark.parametrize(
    "query",
    ["New York City", "new york city", "NYC", "N.Y.C.", "NY", "N.Y.",
     "New York", "New York State", "City of New York"],
)
def test_new_york_aliases_accepted_against_state_record(query):
    display_name, address_type = NEW_YORK_STATE
    assert _matches(query, display_name, address_type) is True


@pytest.mark.parametrize(
    "query", ["New York City", "NYC", "NY", "New York"],
)
def test_new_york_aliases_accepted_against_city_record(query):
    display_name, address_type = NEW_YORK_CITY
    assert _matches(query, display_name, address_type) is True


def test_new_york_city_accepted_even_with_unknown_address_type():
    """Alias normalization alone must carry this case, so it also works for
    legacy geocode_cache rows that have an empty address_type."""
    assert _matches("New York City", "New York, United States", "") is True


# --- the far-away / bounded-search false-match class must stay blocked ------

def test_seneca_county_rejected_with_unknown_address_type():
    display_name, _ = SENECA_AVENUE
    assert _matches("Seneca County", display_name, "") is False


def test_seneca_county_rejected_when_enriched_with_state():
    """build_geocoding_query appends a nearby US state, so the real query is
    often 'Seneca County, New York'. Still a reject."""
    display_name, address_type = SENECA_AVENUE
    assert _matches("Seneca County, New York", display_name, address_type) is False


def test_admin_suffix_is_not_dropped_for_non_admin_results():
    """'county' may only be dropped when the match is itself an admin-level
    place. A road named 'Seneca Avenue' is not, so 'Seneca County' stays
    rejected even though 'Seneca' alone would match."""
    display_name, _ = SENECA_AVENUE
    assert _matches("Seneca County", display_name, "road") is False
    assert _matches("Seneca County", display_name, "residential") is False


def test_bare_seneca_still_matches_seneca_avenue():
    """Sanity check that the Seneca reject comes from the 'county' token and
    not from something incidental."""
    display_name, address_type = SENECA_AVENUE
    assert _matches("Seneca", display_name, address_type) is True


@pytest.mark.parametrize(
    "query",
    ["Albany County", "Erie County", "Monroe County", "Seneca Falls"],
)
def test_other_upstate_queries_rejected_against_seneca_avenue(query):
    display_name, address_type = SENECA_AVENUE
    assert _matches(query, display_name, address_type) is False


def test_ny_ny_rejected_against_unrelated_bronx_road():
    """'NY, NY' matching a Broadway address in the Bronx is a correct reject:
    the core name 'NY' -> 'New York' does not match the road 'Broadway'."""
    display_name, address_type = BROADWAY_BRONX
    assert _matches("NY, NY", display_name, address_type) is False


def test_far_away_city_with_wrong_state_context_rejected():
    """Context segments are checked against the full display_name, so a
    far-away state can't be force-matched onto a same-named NYC street."""
    display_name, address_type = PORTLAND_AVENUE
    assert _matches("Portland, Oregon", display_name, address_type) is False


def test_trailing_admin_suffix_only_so_borough_park_is_not_loosened():
    """'borough' leads rather than trails in 'Borough Park', so it is never
    dropped -- otherwise 'Borough Park' could be loosened to 'Park' and match
    the suburb 'Park Slope'."""
    display_name, address_type = PARK_SLOPE
    assert _matches("Borough Park", display_name, address_type) is False


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
    assert _matches(query, display_name, address_type) is True


def test_mismatched_borough_context_rejected():
    """Context that contradicts the match is still a reject."""
    display_name, address_type = GRAND_CENTRAL
    assert _matches("Grand Central, Brooklyn", display_name, address_type) is False


# --- degenerate input --------------------------------------------------------

@pytest.mark.parametrize("query", ["", "   ", "the", "of the"])
def test_queries_with_no_significant_tokens_are_accepted(query):
    display_name, address_type = TIMES_SQUARE
    assert _matches(query, display_name, address_type) is True


def test_missing_query_word_still_rejected():
    """The core guarantee of the original check: a significant query word that
    is absent from the matched name means the match is coincidental."""
    display_name, address_type = TIMES_SQUARE
    assert _matches("Herald Square", display_name, address_type) is False


# --- _name_matches_query's new continuous overlap ratio ---------------------

def test_name_match_ratio_high_for_clean_match():
    """"Grand Central" vs "Grand Central Terminal, ..." -- every core query
    token is present in the matched name, so the ratio should be a clean 1.0,
    same as the (still-accepting) boolean verdict."""
    display_name, address_type = GRAND_CENTRAL
    matches, ratio = _name_matches_query("Grand Central", display_name, address_type)
    assert matches is True
    assert ratio == 1.0


def test_name_match_ratio_low_for_seneca_county_false_match():
    """The Seneca County / Seneca Avenue false-match case: only "seneca" of
    the two core tokens ("seneca", "county") is present in the matched
    primary segment "Seneca Avenue", so the ratio should be well below the
    clean-match case even though both are < 1.0 "reject" outcomes."""
    display_name, address_type = SENECA_AVENUE
    matches, ratio = _name_matches_query("Seneca County", display_name, address_type)
    assert matches is False
    assert ratio < 1.0
    clean_matches, clean_ratio = _name_matches_query("Grand Central", *GRAND_CENTRAL)
    assert clean_matches is True
    assert ratio < clean_ratio


def test_name_match_ratio_full_for_bare_seneca():
    """Sanity check: the bare "Seneca" query (a real accept) should score a
    full 1.0 ratio, same as any other clean match."""
    display_name, address_type = SENECA_AVENUE
    matches, ratio = _name_matches_query("Seneca", display_name, address_type)
    assert matches is True
    assert ratio == 1.0


# --- assign_precision ---------------------------------------------------

def test_assign_precision_unrecognized_type_is_not_silently_city():
    """A real but unrecognized POI/address type must no longer be
    indistinguishable from a genuine city-level admin match."""
    result = assign_precision("some_unlisted_poi_type", False)
    assert result == "unclassified"
    assert result != "city"


@pytest.mark.parametrize(
    "address_type, was_clarified, expected",
    [
        ("amenity", False, "address"),
        ("amenity", True, "address (clarified)"),
        ("city", False, "city"),
        ("state", False, "state"),
        ("country", False, "country"),
    ],
)
def test_assign_precision_preexisting_branches_unchanged(address_type, was_clarified, expected):
    assert assign_precision(address_type, was_clarified) == expected


# --- specificity() -----------------------------------------------------

@pytest.mark.parametrize("phrase", ["New York", "NYC", "Brooklyn", "Manhattan"])
def test_specificity_bare_jurisdiction_terms_score_low(phrase):
    assert specificity(phrase) <= 0.2


@pytest.mark.parametrize("phrase", ["City Hall", "Times Square", "Brooklyn Navy Yard"])
def test_specificity_venue_compounds_score_high(phrase):
    assert specificity(phrase) >= 0.8


@pytest.mark.parametrize("phrase", ["Marble Hill", "Astoria", "Bushwick"])
def test_specificity_recognized_toponyms_score_mid_tier(phrase):
    score = specificity(phrase)
    assert 0.2 < score < 0.8


def test_specificity_mid_tier_strictly_between_jurisdiction_and_venue():
    jurisdiction_score = specificity("Brooklyn")
    toponym_score = specificity("Astoria")
    venue_score = specificity("Times Square")
    assert jurisdiction_score < toponym_score < venue_score


# --- borough-conflict signal ---------------------------------------------

def _raw_with_county(county_value: str, key: str = "county") -> dict:
    return {"address": {key: county_value}}


def test_borough_conflict_fires_when_entities_imply_different_borough():
    """Entities name Brooklyn, but the resolved result's own address data
    places it in New York County (Manhattan) -- a real conflict."""
    raw = _raw_with_county("New York County")
    all_entity_pairs = [("Some Spot", "FAC"), ("Brooklyn", "GPE")]
    assert _detect_borough_conflict("Some Spot", all_entity_pairs, raw) is True


def test_borough_conflict_does_not_fire_for_real_marble_hill_case():
    """Marble Hill is geographically Bronx-attached but administratively
    Manhattan/New York County. Verified live against Nominatim on
    2026-10-13: its address breakdown for "Marble Hill, New York" came back
    as address.city_district = "New York County" (addresstype
    "neighbourhood", suburb "Manhattan") -- NOT address.county, contradicting
    this task's brief, which named address.county specifically. The
    production _resolved_borough() checks county/city_district/suburb for
    exactly this reason. Entities naming Manhattan (the correct borough)
    must NOT trigger a conflict."""
    raw = _raw_with_county("New York County", key="city_district")
    all_entity_pairs = [("Marble Hill", "GPE"), ("Manhattan", "GPE")]
    assert _detect_borough_conflict("Marble Hill", all_entity_pairs, raw) is False


def test_borough_conflict_false_when_no_other_entities_mention_a_borough():
    raw = _raw_with_county("Kings County")
    all_entity_pairs = [("Some Spot", "FAC")]
    assert _detect_borough_conflict("Some Spot", all_entity_pairs, raw) is False


def test_borough_conflict_false_when_raw_has_no_county_data():
    all_entity_pairs = [("Some Spot", "FAC"), ("Queens", "GPE")]
    assert _detect_borough_conflict("Some Spot", all_entity_pairs, None) is False
    assert _detect_borough_conflict("Some Spot", all_entity_pairs, {}) is False


def test_borough_conflict_ignores_candidates_own_entity_text():
    """A candidate whose own entity text IS a borough name must never
    conflict with itself."""
    raw = _raw_with_county("Kings County")
    all_entity_pairs = [("Brooklyn", "GPE")]
    assert _detect_borough_conflict("Brooklyn", all_entity_pairs, raw) is False


# --- resolve_post_locations' LLM "resolve" path --------------------------

def test_llm_resolve_path_sets_provenance_and_does_not_fake_importance():
    """The LLM "resolve" action previously hardcoded importance=1.0 on every
    candidate, which a downstream scorer could mistake for a real Nominatim
    importance score. It must now leave importance at the dataclass's
    neutral/zero default and tag provenance="llm_resolve" instead."""
    llm_result = {
        "action": "resolve", "lat": 40.7794, "lng": -73.9632,
        "query": None, "confidence": 0.9,
    }
    with patch("pipeline.geocoder.resolve_entity_with_llm", return_value=llm_result), \
         patch("pipeline.geocoder.time.sleep"):
        candidates = resolve_post_locations(
            "at the met today", [("The Met", "FAC")],
            geocoder=object(), conn=object(), llm_client=object(),
        )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.provenance == "llm_resolve"
    assert candidate.importance == 0.0
    assert candidate.lat == 40.7794
    assert candidate.lng == -73.9632
    assert candidate.address_type == ""
    # specificity_score is still computed on this path (pure text, no
    # network dependency).
    assert candidate.specificity_score == specificity("The Met")
