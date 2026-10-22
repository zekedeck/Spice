"""Tests for pipeline.scorer.score_candidate / apply_confidence_threshold.

Run from the repo root with:  python -m pytest tests/ -q

The formula replaces the old word-count/emoji/preposition-window heuristic
with four signals, in order: provenance tier, Nominatim address_type tier,
specificity_score (scaled), and a borough_conflict halving. These tests
pin down each lever on its own, plus the two worked examples from the
panel's validation report (a bare "New York" llm_resolve candidate landing
near 0.38, below the 0.40 threshold; a real-venue llm_resolve candidate
landing near 0.62, above it).
"""

from pipeline.scorer import score_candidate, apply_confidence_threshold, CONFIDENCE_THRESHOLD
from models import LocationCandidate


def _candidate(**overrides):
    defaults = dict(
        text="Union Square",
        query_sent="Union Square, New York",
        lat=40.7359,
        lng=-73.9911,
        precision="address",
        display_name="Union Square, Manhattan, New York",
        address_type="",
        provenance="",
        borough_conflict=False,
        specificity_score=0.0,
    )
    defaults.update(overrides)
    return LocationCandidate(**defaults)


def test_grounded_venue_scores_high():
    # rule/llm_accept/llm_query all count as "grounded" (base 0.55); a real
    # venue/POI address_type adds the full +0.25 bonus; specificity adds its
    # scaled share on top. Kept below 1.0 so the clamp doesn't mask the
    # per-term arithmetic being checked here.
    c = _candidate(provenance="rule", address_type="amenity", specificity_score=0.5)
    score = score_candidate(c)
    assert score == round(0.55 + 0.25 + 0.5 * 0.3, 3)
    assert score > 0.9


def test_llm_resolve_bare_specificity_scores_below_threshold():
    # llm_resolve never queries Nominatim, so address_type is empty (no
    # bonus/penalty) and only the bare-jurisdiction specificity tier (0.1)
    # applies -- this is the "New York" case, and it must land under 0.40.
    c = _candidate(provenance="llm_resolve", address_type="", specificity_score=0.1)
    score = score_candidate(c)
    assert score < CONFIDENCE_THRESHOLD


def test_borough_conflict_halves_the_score():
    # Halving applies to the raw (pre-clamp) score, so use inputs that don't
    # saturate at 1.0 -- otherwise the clamp would mask the halving itself.
    c = _candidate(provenance="rule", address_type="amenity", specificity_score=0.5, borough_conflict=False)
    baseline = score_candidate(c)

    c_conflict = _candidate(provenance="rule", address_type="amenity", specificity_score=0.5, borough_conflict=True)
    conflicted = score_candidate(c_conflict)

    assert conflicted == round(baseline * 0.5, 3)


def test_worked_example_bare_new_york_llm_resolve():
    # Panel's validation example: a bare "New York" candidate via the
    # ungrounded llm_resolve path lands near 0.38, below threshold.
    c = _candidate(text="New York", provenance="llm_resolve", address_type="", specificity_score=0.1)
    score = score_candidate(c)
    assert abs(score - 0.38) < 0.01
    assert score < CONFIDENCE_THRESHOLD


def test_worked_example_real_venue_llm_resolve():
    # Panel's validation example: a real venue name resolved via
    # llm_resolve (so still ungrounded/no address_type) lands near 0.62,
    # above threshold, driven entirely by the venue-tier specificity score.
    c = _candidate(text="Brooklyn Navy Yard", provenance="llm_resolve", address_type="", specificity_score=0.9)
    score = score_candidate(c)
    assert abs(score - 0.62) < 0.01
    assert score >= CONFIDENCE_THRESHOLD


def test_unknown_provenance_is_conservative_middle_tier():
    c = _candidate(provenance="", address_type="", specificity_score=0.0)
    assert score_candidate(c) == 0.45


def test_state_type_grounded_penalty():
    c = _candidate(provenance="llm_query", address_type="state", specificity_score=0.1)
    score = score_candidate(c)
    assert score == round(min(1.0, 0.55 - 0.15 + 0.1 * 0.3), 3)


def test_city_type_grounded_bonus():
    c = _candidate(provenance="llm_accept", address_type="neighbourhood", specificity_score=0.5)
    score = score_candidate(c)
    assert score == round(min(1.0, 0.55 + 0.15 + 0.5 * 0.3), 3)


def test_score_is_clamped_to_one():
    c = _candidate(provenance="rule", address_type="amenity", specificity_score=1.0)
    assert score_candidate(c) <= 1.0


def test_apply_confidence_threshold_filters_and_annotates():
    kept = _candidate(provenance="rule", address_type="amenity", specificity_score=0.9)
    dropped = _candidate(text="New York", provenance="llm_resolve", address_type="", specificity_score=0.1)

    result = apply_confidence_threshold([kept, dropped], post_text="irrelevant", all_entity_pairs=[])

    assert result == [kept]
    assert kept.confidence >= CONFIDENCE_THRESHOLD
    assert dropped.confidence < CONFIDENCE_THRESHOLD
