"""Phase 2 read-only behavior: full snapshots, mapped multi-playlist runs, mapping worksheet."""

import json
import time
from dataclasses import replace

import httpx
import pytest

from dj_sync.cli import main, parser
from dj_sync.db import Ledger, cache_key
from dj_sync.errors import ApiError, SyncError
from dj_sync.http import ReadClient
from dj_sync.models import PlaylistItem, Snapshot
from dj_sync.pipeline import run_mapped, run_playlist
from dj_sync.reports import mapping_worksheet, write_report
from dj_sync.spotify_client import SpotifyClient
from dj_sync.track_matcher import match_track

PLAYLIST = "a" * 22


def api(service, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ReadClient(
        service, "https://api.spotify.com/v1/", lambda force: "t", client, 0, lambda _: None
    )


def spotify_track(n):
    return {
        "type": "track",
        "id": f"s{n}",
        "name": f"Song {n}",
        "artists": [{"name": "Artist"}],
        "duration_ms": 200000,
        "external_ids": {"isrc": f"ISRC{n}"},
        "is_playable": True,
    }


# ---------- Spotify full snapshots ----------


def full_handler(total, snapshots=None, page_cap=50, stop_early=False):
    calls = []
    snaps = iter(snapshots or [])

    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/items"):
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            calls.append((offset, limit))
            count = 0 if stop_early and offset else min(limit, page_cap, total - offset)
            following = offset + count < total
            return httpx.Response(
                200,
                json={
                    "items": [{"item": spotify_track(i)} for i in range(offset, offset + count)],
                    "total": total,
                    "next": "https://api.spotify.com/v1/next" if following else None,
                },
            )
        return httpx.Response(200, json={"name": "Big", "snapshot_id": next(snaps, "v1")})

    return handler, calls


def test_full_snapshot_reads_every_item_and_is_complete():
    handler, calls = full_handler(123)
    result = SpotifyClient(api("spotify", handler)).full(PLAYLIST)
    assert result.complete
    assert [i.position for i in result.items] == list(range(123))
    assert result.items[-1].track.id == "s122"
    assert calls == [(0, 50), (50, 50), (100, 50)]


def test_full_snapshot_of_empty_playlist_is_complete():
    handler, calls = full_handler(0)
    result = SpotifyClient(api("spotify", handler)).full(PLAYLIST)
    assert result.complete and result.items == ()
    assert calls == [(0, 50)]


def test_full_snapshot_rejects_concurrent_edit():
    handler, _ = full_handler(60, snapshots=["v1", "v2"])
    with pytest.raises(SyncError, match="changed"):
        SpotifyClient(api("spotify", handler)).full(PLAYLIST)


def test_full_snapshot_rejects_truncated_pagination():
    handler, _ = full_handler(120, stop_early=True)
    with pytest.raises(SyncError, match="pagination ended"):
        SpotifyClient(api("spotify", handler)).full(PLAYLIST)


def test_sample_still_limited_to_ten():
    handler, _ = full_handler(30)
    with pytest.raises(SyncError):
        SpotifyClient(api("spotify", handler)).sample(PLAYLIST, 11)


def test_current_user_id():
    def handler(request):
        assert request.url.path == "/v1/me"
        return httpx.Response(200, json={"id": "me123", "display_name": "Me"})

    assert SpotifyClient(api("spotify", handler)).user_id() == "me123"


# ---------- Ledger cache policy ----------


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "data" / "ledger.sqlite3")
    yield db
    db.close()


def one_track_snapshot(source, playlist="playlist1", ids=("s1",), name="Test"):
    return Snapshot(
        playlist,
        name,
        "v1",
        tuple(PlaylistItem(i, replace(source, id=t)) for i, t in enumerate(ids)),
        True,
    )


def test_confident_match_cached_for_thirty_days_but_review_items_for_one(ledger, source, target):
    _, ids = ledger.snapshot(one_track_snapshot(source, ids=("s1", "s2")), None)
    run_id = ledger.begin("playlist1")
    key1 = cache_key(source, "US")
    key2 = cache_key(replace(source, id="s2"), "US")
    ledger.checkpoint(run_id, ids["s1"], "US", key1, match_track(source, [target]))
    ledger.checkpoint(run_id, ids["s2"], "US", key2, match_track(source, []))
    two_days_ago = time.time() - 2 * 86400
    ledger.connection.execute("UPDATE matches SET updated=?", (two_days_ago,))
    ledger.connection.commit()
    assert ledger.cached(ids["s1"], key1, "US")["status"] == "matched"
    assert ledger.cached(ids["s2"], key2, "US") is None
    ledger.connection.execute("UPDATE matches SET updated=?", (time.time() - 31 * 86400,))
    ledger.connection.commit()
    assert ledger.cached(ids["s1"], key1, "US") is None


# ---------- Pipeline ----------


class SpotifyStub:
    def __init__(self, snapshots, unreadable=(), fail_status=403):
        self.snapshots, self.unreadable, self.fail_status = snapshots, set(unreadable), fail_status
        self.full_calls, self.sample_calls = [], []

    def full(self, playlist_id):
        self.full_calls.append(playlist_id)
        if playlist_id in self.unreadable:
            raise ApiError("spotify", self.fail_status)
        return self.snapshots[playlist_id]

    def sample(self, playlist_id, limit):
        self.sample_calls.append((playlist_id, limit))
        return self.snapshots[playlist_id]


class TidalStub:
    def __init__(self, target):
        self.target, self.calls = target, []
        self.limited = False

    def candidates(self, source):
        self.calls.append(source.id)
        if self.limited:
            raise ApiError("tidal", 429)
        return [replace(self.target, id="t-" + source.id)]


def test_run_playlist_full_scope_uses_full_snapshot_and_destination_plan(ledger, source, target):
    snap = one_track_snapshot(source, ids=tuple(f"s{i}" for i in range(15)))
    spotify = SpotifyStub({"playlist1": snap})
    report = run_playlist(spotify, TidalStub(target), ledger, "playlist1", "US", "DJ", None)
    assert spotify.full_calls == ["playlist1"] and spotify.sample_calls == []
    assert report["playlist"]["complete"] is True
    assert report["diff"]["scope"] == "complete"
    plan = report["plan"]
    assert plan["target"] == "tidal_playlist_for_mapped_source"
    assert plan["tidal_playlist_id"] is None
    assert len(plan["proposed_track_ids"]) == 15
    assert plan["writes_performed"] == 0
    assert ledger.connection.execute("SELECT count(*) FROM desired_memberships").fetchone()[0] == 15


def test_full_report_markdown_lists_only_review_evidence(ledger, source, target, tmp_path):
    snap = one_track_snapshot(source, ids=("s1", "s2"))
    tidal = TidalStub(target)
    original = tidal.candidates
    tidal.candidates = lambda s: [] if s.id == "s2" else original(s)
    report = run_playlist(SpotifyStub({"playlist1": snap}), tidal, ledger, "playlist1", "US", "DJ")
    text = write_report(tmp_path / "reports", report).read_text()
    assert "### Item 2" in text
    assert "### Item 1" not in text


def test_mapped_run_shares_tracks_and_skips_unreadable(ledger, source, target, tmp_path):
    spotify = SpotifyStub(
        {
            "playlist1": one_track_snapshot(source, "playlist1", ("s1", "s2"), "One"),
            "playlist2": one_track_snapshot(source, "playlist2", ("s2", "s3"), "Two"),
        },
        unreadable={"playlist3"},
    )
    tidal = TidalStub(target)
    mappings = {"playlist1": "DJ", "playlist3": "Vibes", "playlist2": "Vibes"}
    summary = run_mapped(spotify, tidal, ledger, mappings, "US", tmp_path / "reports")
    assert sorted(tidal.calls) == ["s1", "s2", "s3"]  # shared track matched once
    status = {p["id"]: p["status"] for p in summary["playlists"]}
    assert status == {"playlist1": "completed", "playlist2": "completed", "playlist3": "unreadable"}
    assert summary["totals"]["unique_spotify_tracks"] == 3
    assert summary["totals"]["tracks_in_multiple_playlists"] == 1
    assert summary["writes_performed"] == 0
    assert ledger.connection.execute("SELECT count(*) FROM tracks").fetchone()[0] == 3
    md = (tmp_path / "reports" / f"sync-{summary['id']}.md").read_text()
    assert "Two" in md and "unreadable" in md


def test_mapped_run_stops_when_catalog_rate_limited(ledger, source, target, tmp_path):
    spotify = SpotifyStub(
        {
            "playlist1": one_track_snapshot(source, "playlist1", ("s1", "s2")),
            "playlist2": one_track_snapshot(source, "playlist2", ("s3",)),
        }
    )
    tidal = TidalStub(target)
    tidal.limited = True
    summary = run_mapped(
        spotify, tidal, ledger, {"playlist1": "DJ", "playlist2": "DJ"}, "US", tmp_path
    )
    assert len(tidal.calls) == 1
    status = {p["id"]: p["status"] for p in summary["playlists"]}
    assert status == {"playlist1": "completed_with_errors", "playlist2": "deferred"}
    assert spotify.full_calls == ["playlist1"]


def test_mapped_run_stops_on_spotify_auth_failure(ledger, source, target, tmp_path):
    spotify = SpotifyStub({}, unreadable={"playlist1"}, fail_status=401)
    with pytest.raises(ApiError):
        run_mapped(spotify, TidalStub(target), ledger, {"playlist1": "DJ"}, "US", tmp_path)


def test_mapped_run_requires_mappings(ledger, source, target, tmp_path):
    with pytest.raises(SyncError, match="No Spotify playlists are mapped"):
        run_mapped(SpotifyStub({}), TidalStub(target), ledger, {}, "US", tmp_path)


def test_mapped_run_is_idempotent(ledger, source, target, tmp_path):
    spotify = SpotifyStub({"playlist1": one_track_snapshot(source, ids=("s1", "s2"))})
    tidal = TidalStub(target)
    first = run_mapped(spotify, tidal, ledger, {"playlist1": "DJ"}, "US", tmp_path)
    second = run_mapped(spotify, tidal, ledger, {"playlist1": "DJ"}, "US", tmp_path)
    assert len(tidal.calls) == 2
    assert first["totals"]["proposed_tidal_ids"] == second["totals"]["proposed_tidal_ids"] == 2
    assert second["playlists"][0]["cached"] == 2
    assert ledger.connection.execute("SELECT count(*) FROM desired_memberships").fetchone()[0] == 2


# ---------- Mapping worksheet ----------


def test_worksheet_never_assigns_groups_and_separates_unreadable(tmp_path):
    review = [
        {"id": "a" * 22, "name": "Mine # weird", "group": None, "readable": True, "items": 12},
        {"id": "b" * 22, "name": "Mapped", "group": "DJ", "readable": True, "items": 3},
        {"id": "c" * 22, "name": "Followed", "group": None, "readable": False, "items": 40},
    ]
    text = mapping_worksheet(review)
    assert "#  " + "a" * 22 + ': ""' in text
    assert "b" * 22 + ": DJ" in text
    unreadable = text.split("Not readable")[1]
    assert "c" * 22 in unreadable
    assert "DJ\n" not in text.split("Already mapped")[0].split("Not readable")[0]


# ---------- CLI arguments ----------


def test_sync_requires_exactly_one_target():
    with pytest.raises(SystemExit):
        parser().parse_args(["sync"])
    with pytest.raises(SystemExit):
        parser().parse_args(["sync", "--playlist", "a" * 22, "--mapped"])
    assert parser().parse_args(["sync", "--mapped"]).mapped
    assert parser().parse_args(["sync", "--playlist", "a" * 22, "--full"]).full


def test_limit_conflicts_with_full_scope(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("playlists: {}\n")
    assert main(["--config", str(path), "sync", "--playlist", "a" * 22, "--full", "--limit", "5"])
    assert "--limit" in capsys.readouterr().err


def test_json_summary_round_trips(ledger, source, target, tmp_path):
    spotify = SpotifyStub({"playlist1": one_track_snapshot(source)})
    summary = run_mapped(spotify, TidalStub(target), ledger, {"playlist1": "DJ"}, "US", tmp_path)
    saved = json.loads((tmp_path / f"sync-{summary['id']}.json").read_text())
    assert saved["totals"] == summary["totals"]


# ---------- ISRC normalization (live HTTP 400 regression) ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("usdy41650617", "USDY41650617"),
        ("GB-SMU-33-26404", "GBSMU3326404"),
        (" USAAA2600001 ", "USAAA2600001"),
        ("not-an-isrc", None),
        ("", None),
        (None, None),
    ],
)
def test_isrc_normalization(raw, expected):
    from dj_sync.models import normalize_isrc

    assert normalize_isrc(raw) == expected


def test_spotify_lowercase_isrc_reaches_tidal_in_canonical_form():
    def handler(request):
        if request.url.path.endswith("/items"):
            track = {**spotify_track(1), "external_ids": {"isrc": "usdy41650617"}}
            bad = {**spotify_track(2), "external_ids": {"isrc": "junk"}}
            return httpx.Response(
                200, json={"items": [{"item": track}, {"item": bad}], "total": 2, "next": None}
            )
        return httpx.Response(200, json={"name": "P", "snapshot_id": "1"})

    items = SpotifyClient(api("spotify", handler)).full(PLAYLIST).items
    assert items[0].track.isrc == "USDY41650617"
    assert items[1].track.isrc is None


def test_tidal_skips_isrc_filter_when_isrc_missing(source):
    from dj_sync.tidal_client import TidalClient

    seen = []

    def handler(request):
        seen.append(request.url.path)
        assert request.url.path != "/v2/tracks", "No ISRC lookup without a valid ISRC"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"type": "searchResults", "id": "q", "relationships": {"tracks": {"data": []}}}
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tidal = TidalClient(
        ReadClient(
            "tidal", "https://openapi.tidal.com/v2/", lambda f: "t", client, 0, lambda _: None
        ),
        "US",
    )
    assert tidal.candidates(replace(source, isrc=None)) == []
    assert seen == ["/v2/searchResults"]
