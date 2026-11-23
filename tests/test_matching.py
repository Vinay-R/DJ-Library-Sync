from dataclasses import replace

import pytest

from dj_sync.track_matcher import match_track


def test_isrc_metadata_and_separate_version_agree(source, target):
    match = match_track(source, [target])
    assert match.status == "matched"
    assert match.selected_id == "t1"
    assert match.candidates[0].score == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "Radio Edit", "duration_ms": 180000},
        {"version": "Other Producer Remix"},
        {"version": "Dub"},
        {"version": "VIP"},
        {"version": ""},
        {"duration_ms": 380000},
        {"duration_ms": None},
        {"artists": ("Different Artist", "Artist")},
        {"title": "A Completely Different Song"},
        {"available": False},
        {"explicit": True},
        {"isrc": "USAAA2600002"},
    ],
)
def test_isrc_never_overrides_contradictions(source, target, changes):
    result = match_track(source, [replace(target, **changes)])
    assert result.status != "matched"
    assert result.selected_id is None


def test_duplicate_isrc_candidates_pick_one_identical_release(source, target):
    # Same ISRC, title, version, explicit flag and length: the same recording.
    result = match_track(source, [replace(target, id="t2"), target])
    assert (result.status, result.selected_id) == ("matched", "t1")
    # A different length between the duplicates still needs review.
    longer = replace(target, id="t2", duration_ms=target.duration_ms + 4000)
    shorter = replace(target, duration_ms=target.duration_ms - 3000)
    assert match_track(source, [shorter, longer]).status == "ambiguous"


def test_same_candidate_repeated_not_ambiguous(source, target):
    assert match_track(source, [target, target]).status == "matched"


def test_metadata_only_match(source, target):
    assert match_track(replace(source, isrc=None), [target]).status == "matched"


def test_accent_case_and_punctuation(source, target):
    source = replace(source, title="NÍGHT DRIVE (Extended Mix)")
    assert match_track(source, [target]).status == "matched"


def test_distinct_named_remixes(source, target):
    source = replace(source, title="Night Drive (Alice Remix)")
    target = replace(target, version="Bob Remix")
    assert match_track(source, [target]).status == "ambiguous"


def test_unavailable_and_no_results(source):
    # Spotify availability no longer short-circuits matching; TIDAL is searched anyway.
    assert match_track(replace(source, available=False), []).status == "unmatched"
    assert match_track(source, []).status == "unmatched"


def test_named_inline_remix_cannot_fuzzy_match_another_remixer(source, target):
    source = replace(source, title="A Very Long Night Drive Title Alice Remix")
    target = replace(target, title="A Very Long Night Drive Title Bob Remix", version="")
    assert match_track(source, [target]).selected_id is None


def test_release_metadata_resolves_only_a_unique_compatible_isrc_tie(source, target):
    source = replace(source, album_title="Original Single", album_release_date="2025-07-18")
    original = replace(target, album_title="Original Single", album_release_date="2025-07-18")
    reissue = replace(target, id="t2", album_title="Later Album", album_release_date="2026-01-01")
    result = match_track(source, [original, reissue])
    assert result.selected_id == original.id
    assert "release date" in result.reason
    # Identical duplicates of the same release now resolve to the lowest catalog ID.
    assert match_track(source, [original, replace(original, id="t3")]).selected_id == "t1"
    assert match_track(replace(source, isrc=None), [original, reissue]).status == "ambiguous"
    # A partial Spotify date still prefers the release with the same album title.
    partial = match_track(replace(source, album_release_date="2025"), [original, reissue])
    assert (partial.selected_id, "Same recording" in partial.reason) == ("t1", True)
    assert (
        match_track(source, [replace(original, version="Radio Edit"), reissue]).selected_id
        != original.id
    )


def test_with_credit_is_treated_like_feat_only_for_credited_artists(target):
    from dj_sync.models import Track
    from dj_sync.track_matcher import identity

    spotify = Track(
        "spotify",
        "x",
        "Can’t Stop (with Mira Vale & Otto Brand)",
        ("Kilo Reed", "Mira Vale", "Otto Brand"),
        168253,
    )
    assert identity(spotify)[0] == "can t stop"
    # "(With Love)" names no credited artist, so it stays part of the title.
    song = Track("spotify", "y", "Letters (With Love)", ("Someone",), 200000)
    assert identity(song)[0] == "letters with love"


def test_underscores_compare_like_spaces():
    from dj_sync.track_matcher import normalize

    assert normalize("zi$$_go.wav") == normalize("zi$$ go.wav") == "zi go wav"
