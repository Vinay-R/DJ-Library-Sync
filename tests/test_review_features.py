"""Identical-recording release choice, fallback search, unavailable sources, manual review."""

import json
from dataclasses import replace

import pytest

from dj_sync.db import Ledger
from dj_sync.errors import SyncError
from dj_sync.models import PlaylistItem, Snapshot
from dj_sync.pipeline import run_playlist
from dj_sync.review import import_decisions, review_items, simple_query
from dj_sync.track_matcher import match_track


@pytest.fixture
def song(source):
    return replace(source, album_title="Slow Sundays", album_release_date="2024-03-15")


def release(target, id, album, date, **changes):
    return replace(target, id=id, album_title=album, album_release_date=date, **changes)


# ---------- #1 identical recording on several releases ----------


def test_identical_recording_prefers_spotify_album_then_lowest_id(song, target):
    candidates = [
        release(target, "349713361", "Slow Sundays", "2024-03-15"),
        release(target, "349713359", "Slow Sundays", "2024-03-15"),
        release(target, "418773281", "Catwalk Phonk Vibes", "2025-02-21"),
    ]
    match = match_track(song, candidates)
    assert match.status == "matched"
    assert match.selected_id == "349713359"
    assert "Same recording" in match.reason


def test_identical_recording_otherwise_prefers_earliest_release(song, target):
    candidates = [
        release(target, "1", "Beach Club Hits", "2025-09-19"),
        release(target, "2", "Original EP", "2023-01-06"),
        release(target, "3", "No Date Comp", ""),
    ]
    match = match_track(replace(song, album_title="Something Else"), candidates)
    assert (match.status, match.selected_id) == ("matched", "2")


def test_mixed_explicit_flags_stay_ambiguous(song, target):
    candidates = [
        release(target, "1", "A", "2024-01-01", explicit=True),
        release(target, "2", "B", "2024-01-02", explicit=False),
    ]
    assert match_track(replace(song, explicit=None), candidates).status == "ambiguous"


def test_materially_different_lengths_stay_ambiguous(song, target):
    long_song = replace(song, duration_ms=420000)
    candidates = [
        release(target, "1", "A", "2024-01-01", duration_ms=416000),
        release(target, "2", "B", "2024-01-02", duration_ms=424000),
    ]
    assert match_track(long_song, candidates).status == "ambiguous"


def test_ties_without_a_shared_isrc_stay_ambiguous(song, target):
    candidates = [
        release(target, "1", "A", "2024-01-01", isrc=None),
        release(target, "2", "B", "2024-01-02", isrc=None),
    ]
    assert match_track(replace(song, isrc=None), candidates).status == "ambiguous"


# ---------- #3 Spotify-unavailable tracks are still matched ----------


def test_spotify_unavailable_track_is_matched_normally(source, target):
    match = match_track(replace(source, available=False), [target])
    assert (match.status, match.selected_id) == ("matched", "t1")


# ---------- pipeline: #2 fallback, #3 search, retry and manual decisions ----------


class Spotify:
    def __init__(self, items):
        self.snap = Snapshot("playlist1", "Test", "v1", tuple(items), True)

    def full(self, playlist_id):
        return self.snap

    def sample(self, playlist_id, limit):
        return self.snap


class Catalog:
    def __init__(self, primary, fallback=()):
        self.primary, self.fallback = list(primary), list(fallback)
        self.calls, self.fallback_calls = 0, 0

    def candidates(self, source):
        self.calls += 1
        return self.primary

    def fallback_candidates(self, source):
        self.fallback_calls += 1
        return self.fallback


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.sqlite3")
    yield db
    db.close()


def test_fallback_search_runs_only_when_nothing_is_eligible(ledger, source, target):
    items = [PlaylistItem(0, source)]
    rescued = Catalog([], [target])
    report = run_playlist(Spotify(items), rescued, ledger, "playlist1", "US", "DJ")
    assert report["rows"][0]["match"]["selected_id"] == "t1"
    assert rescued.fallback_calls == 1
    direct = Catalog([target], [target])
    run_playlist(Spotify(items), direct, ledger, "playlist1", "US", "DJ", refresh=True)
    assert direct.fallback_calls == 0


def test_unavailable_spotify_item_is_searched_and_keeps_its_flag(ledger, source, target):
    item = PlaylistItem(0, replace(source, available=False), "unavailable_in_spotify")
    catalog = Catalog([target])
    report = run_playlist(Spotify([item]), catalog, ledger, "playlist1", "US", "DJ")
    row = report["rows"][0]
    assert catalog.calls == 1
    assert row["match"]["status"] == "matched"
    assert row["issue"] == "unavailable_in_spotify"


def test_retry_review_rechecks_only_unresolved_cached_results(ledger, source, target):
    other = replace(source, id="s2", title="Different")
    items = [PlaylistItem(0, source), PlaylistItem(1, other)]
    catalog = Catalog([target])
    run_playlist(Spotify(items), catalog, ledger, "playlist1", "US", "DJ")
    assert catalog.calls == 2
    run_playlist(Spotify(items), catalog, ledger, "playlist1", "US", "DJ", retry_review=True)
    assert catalog.calls == 3  # only the unresolved "Different" track was searched again


def test_manual_match_and_no_match_override_search(ledger, source, target):
    other = replace(source, id="s2", title="Other")
    items = [PlaylistItem(0, source), PlaylistItem(1, other)]
    catalog = Catalog([])
    run_playlist(Spotify(items), catalog, ledger, "playlist1", "US", "DJ")
    counts = import_decisions(
        ledger,
        [
            {"spotify_id": "s1", "decision": "match", "tidal_id": "777"},
            {"spotify_id": "s2", "decision": "no_match"},
        ],
        "US",
    )
    assert counts == {"match": 1, "no_match": 1}
    before = catalog.calls
    report = run_playlist(Spotify(items), catalog, ledger, "playlist1", "US", "DJ")
    assert catalog.calls == before
    first, second = (r["match"] for r in report["rows"])
    assert (first["status"], first["selected_id"]) == ("matched", "777")
    assert first["reason"].startswith("Manual review")
    assert (second["status"], second["reason"][:13]) == ("unmatched", "Manual review")


@pytest.mark.parametrize(
    "decision",
    [
        {"spotify_id": "unknown", "decision": "no_match"},
        {"spotify_id": "s1", "decision": "match", "tidal_id": "not-an-id"},
        {"spotify_id": "s1", "decision": "maybe"},
    ],
)
def test_invalid_decisions_are_rejected_without_partial_writes(ledger, source, decision):
    run_playlist(Spotify([PlaylistItem(0, source)]), Catalog([]), ledger, "playlist1", "US")
    good = {"spotify_id": "s1", "decision": "no_match"}
    with pytest.raises(SyncError):
        import_decisions(ledger, [good, decision], "US")
    assert ledger.connection.execute("SELECT count(*) FROM manual_matches").fetchone()[0] == 0


def test_tidal_urls_are_accepted_as_manual_ids(ledger, source):
    run_playlist(Spotify([PlaylistItem(0, source)]), Catalog([]), ledger, "playlist1", "US")
    decision = {
        "spotify_id": "s1",
        "decision": "match",
        "tidal_id": "https://tidal.com/browse/track/12345?u",
    }
    import_decisions(ledger, [decision], "US")
    row = ledger.connection.execute("SELECT tidal_id FROM manual_matches").fetchone()
    assert row[0] == "12345"


def test_review_items_group_unresolved_tracks_across_playlists(ledger, source, target):
    near = replace(target, id="t9", title="Night Drive", version="Radio Edit")
    items = [PlaylistItem(0, source), PlaylistItem(1, replace(source, id="s2", title="Hit"))]
    catalog = Catalog([near])
    run_playlist(Spotify(items), catalog, ledger, "playlist1", "US", "DJ")
    data = review_items(ledger, {"playlist1": "DJ"})
    assert {i["spotify"]["id"] for i in data["items"]} == {"s1", "s2"}
    first = next(i for i in data["items"] if i["spotify"]["id"] == "s1")
    assert first["playlists"] == ["DJ / Test"]
    assert first["candidates"][0]["id"] == "t9"
    assert "url" in first["candidates"][0]
    import_decisions(ledger, [{"spotify_id": "s1", "decision": "no_match"}], "US")
    remaining = review_items(ledger, {"playlist1": "DJ"})["items"]
    assert [i["spotify"]["id"] for i in remaining] == ["s2"]
    json.dumps(data)  # page data must be serializable


@pytest.mark.parametrize(
    "title, artists, expected",
    [
        ("Can’t Stop (with Mira Vale & Otto Brand)", ["Kilo Reed"], "Can't Stop Kilo Reed"),
        ("Hold Me - Dusk City Remix", ["Ferro 007"], "Hold Me Ferro 007"),
        ("day aft3r [the_blues]", ["Wavelorn"], "day aft3r Wavelorn"),
        ("Shape", ["QX-12", "LoTide"], "Shape QX-12"),
    ],
)
def test_simple_query_is_title_and_lead_artist(title, artists, expected):
    assert simple_query(title, artists) == expected


# ---------- "already in Music.app" and "skip" decisions ----------


def test_local_and_skip_decisions_settle_tracks_without_tidal(ledger, source, target):
    from dj_sync.planner import mapped_playlist_plan

    items = [
        PlaylistItem(0, source),
        PlaylistItem(1, replace(source, id="s2", title="Owned")),
        PlaylistItem(2, replace(source, id="s3", title="Meh")),
        PlaylistItem(3, replace(source, id="s4", title="Open")),
    ]
    catalog = Catalog([target])
    run_playlist(Spotify(items), catalog, ledger, "playlist1", "US", "DJ")
    counts = import_decisions(
        ledger,
        [
            {"spotify_id": "s2", "decision": "local"},
            {"spotify_id": "s3", "decision": "skip", "tidal_id": "ignored"},
        ],
        "US",
    )
    assert counts == {"local": 1, "skip": 1}
    before = catalog.calls
    report = run_playlist(Spotify(items), catalog, ledger, "playlist1", "US", "DJ")
    assert catalog.calls == before  # decided and cached tracks are not searched again
    status = {r["source"]["id"]: r["match"]["status"] for r in report["rows"]}
    assert status == {"s1": "matched", "s2": "local", "s3": "skipped", "s4": status["s4"]}
    assert status["s4"] not in {"matched", "local", "skipped"}
    reasons = {r["source"]["id"]: r["match"]["reason"] for r in report["rows"]}
    assert reasons["s2"] == "Manual review: already in Music.app"
    assert reasons["s3"] == "Manual review: skipped by user"
    plan = mapped_playlist_plan(report["rows"], report["diff"], None)
    assert plan["proposed_track_ids"] == ["t1"]
    assert plan["unresolved_positions"] == [3]
    remaining = review_items(ledger, {"playlist1": "DJ"})["items"]
    assert [i["spotify"]["id"] for i in remaining] == ["s4"]


def test_settled_tracks_are_not_counted_as_needing_review(tmp_path):
    from dj_sync.reports import write_summary

    summary = {
        "id": "x",
        "country": "US",
        "playlists": [
            {
                "id": "p",
                "group": "DJ",
                "status": "completed",
                "name": "P",
                "items": 4,
                "counts": {"matched": 1, "local": 1, "skipped": 1, "ambiguous": 1},
                "added_since_last": 0,
                "removed_since_last": 0,
            }
        ],
        "totals": {
            "playlists": 1,
            "row_counts": {},
            "unique_spotify_tracks": 4,
            "tracks_in_multiple_playlists": 0,
            "proposed_tidal_ids": 1,
        },
    }
    row = [
        line
        for line in write_summary(tmp_path, summary).read_text().splitlines()
        if "| P |" in line
    ]
    assert row and row[0].split("|")[6].strip() == "1"


def test_clear_reopens_a_local_or_skipped_track(ledger, source):
    run_playlist(Spotify([PlaylistItem(0, source)]), Catalog([]), ledger, "playlist1", "US", "DJ")
    import_decisions(ledger, [{"spotify_id": "s1", "decision": "skip"}], "US")
    assert review_items(ledger, {"playlist1": "DJ"})["items"] == []
    import_decisions(ledger, [{"spotify_id": "s1", "decision": "clear"}], "US")
    assert ledger.manual("s1", "US") is None
