"""Legacy [CONFIRMED: ...] notes prefixes -> structured outcome."""

import pytest

from app.models.schemas import EpisodeOutcome
from app.services.labels import (
    has_legacy_prefix,
    parse_legacy_outcome,
    strip_legacy_prefixes,
)


@pytest.mark.parametrize(
    "notes, expected",
    [
        ("[CONFIRMED: FLOODED] 25cm dans le moulin", EpisodeOutcome.FLOODED),
        ("[CONFIRMED: FALSE POSITIVE] rien", EpisodeOutcome.FALSE_POSITIVE),
        ("[confirmed: false positive]", EpisodeOutcome.FALSE_POSITIVE),
        ("[CONFIRMED:FLOODED]", EpisodeOutcome.FLOODED),
        ("prefix in the middle [CONFIRMED: FLOODED] ok", EpisodeOutcome.FLOODED),
    ],
)
def test_groundtruth_parse_single_prefix(notes, expected):
    outcome, _ = parse_legacy_outcome(notes)
    assert outcome == expected


@pytest.mark.parametrize("notes", [None, "", "pas inonde", "[CONFIRMED] nope", "[NOTE: FLOODED]"])
def test_groundtruth_no_prefix_returns_none_and_keeps_notes(notes):
    outcome, cleaned = parse_legacy_outcome(notes)
    assert outcome is None
    assert cleaned == notes
    assert not has_legacy_prefix(notes)


def test_groundtruth_stacked_prefixes_leftmost_wins():
    # Legacy confirm prepended its prefix: the leftmost one is the latest decision.
    # This is the requalification case the old frontend could not read
    # (it tested FLOODED first).
    notes = (
        "[CONFIRMED: FALSE POSITIVE] [CONFIRMED: FLOODED] eau au seuil\n"
        "eau au seuil\n"
        "[CONFIRMED: FLOODED] eau au seuil\n"
        "eau au seuil"
    )
    outcome, cleaned = parse_legacy_outcome(notes)
    assert outcome == EpisodeOutcome.FALSE_POSITIVE
    assert cleaned == "eau au seuil"


def test_groundtruth_strip_keeps_distinct_lines():
    notes = "[CONFIRMED: FLOODED] ligne 1\nligne 2\n\nligne 3"
    assert strip_legacy_prefixes(notes) == "ligne 1\nligne 2\n\nligne 3"


def test_groundtruth_strip_prefix_only_returns_none():
    assert strip_legacy_prefixes("[CONFIRMED: FLOODED] ") is None
    assert parse_legacy_outcome("[CONFIRMED: FLOODED]") == (EpisodeOutcome.FLOODED, None)


def test_groundtruth_strip_without_prefix_is_identity():
    # Repeated lines are only collapsed when a legacy prefix was present
    notes = "monte\nmonte"
    assert strip_legacy_prefixes(notes) == notes
