from dataclasses import replace

import pytest

from dj_sync.db import Ledger, cache_key
from dj_sync.errors import SyncError
from dj_sync.models import PlaylistItem, Snapshot
from dj_sync.pipeline import run_poc
from dj_sync.planner import plan_additions
from dj_sync.reports import write_report
from dj_sync.track_matcher import match_track


class SpotifyStub:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def sample(self, playlist_id, limit):
        return self.snapshot


class TidalStub:
    def __init__(self, target):
        self.target, self.calls, self.fail = target, 0, False

    def candidates(self, source):
        self.calls += 1
        if self.fail:
            raise SyncError("tidal: network request failed after retries.")
        return [self.target]


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "data" / "ledger.sqlite3")
    yield db
    db.close()


def snapshot(source, ids=("s1",), complete=True, name="Test"):
    return Snapshot(
        "playlist1",
        name,
        "v1",
        tuple(PlaylistItem(i, replace(source, id=track_id)) for i, track_id in enumerate(ids)),
        complete,
    )


def test_run_twice_no_duplicate_tracks_memberships_or_plan(ledger, source, target, tmp_path):
    spotify = SpotifyStub(snapshot(source, ("s1", "s1")))
    tidal = TidalStub(target)
    first = run_poc(spotify, tidal, ledger, "playlist1", "US", "DJ")
    second = run_poc(spotify, tidal, ledger, "playlist1", "US", "DJ")
    assert first["plan"]["proposed_track_ids"] == second["plan"]["proposed_track_ids"] == ["t1"]
    assert tidal.calls == 1
    assert second["counts"] == {"matched": 2}
    assert all(row["cached"] for row in second["rows"])
    assert ledger.connection.execute("SELECT count(*) FROM tracks").fetchone()[0] == 1
    # Source occurrences/order preserved; destination proposal intentionally deduplicated.
    assert ledger.connection.execute("SELECT count(*) FROM desired_memberships").fetchone()[0] == 2
    assert ledger.connection.execute("SELECT count(*) FROM service_tracks").fetchone()[0] == 2
    assert ledger.connection.execute("SELECT count(*) FROM local_assets").fetchone()[0] == 0
    assert (
        ledger.connection.execute("SELECT stage FROM sync_status").fetchone()[0] == "catalog_match"
    )
    path = write_report(tmp_path / "reports", second)
    assert "External writes: **0**" in path.read_text()
    assert path.with_suffix(".json").exists()
    assert path.stat().st_mode & 0o777 == 0o600


def test_sample_does_not_erase_complete_memberships(ledger, source):
    ledger.snapshot(snapshot(source, ("s1", "s2", "s3")), "DJ")
    diff, _ = ledger.snapshot(snapshot(source, ("s1",), complete=False), "DJ")
    assert not diff["removals_evaluated"]
    assert diff["removed_ids"] == []
    assert ledger.connection.execute("SELECT count(*) FROM desired_memberships").fetchone()[0] == 3


def test_complete_diffs_rename_add_remove_reorder(ledger, source):
    ledger.snapshot(snapshot(source, ("s1", "s2")), "DJ")
    diff, _ = ledger.snapshot(snapshot(source, ("s2", "s3"), name="Renamed"), "DJ")
    assert diff["renamed_from"] == "Test"
    assert diff["added_ids"] == ["s3"]
    assert diff["removed_ids"] == ["s1"]
    diff, _ = ledger.snapshot(snapshot(source, ("s3", "s2")), "DJ")
    assert diff["order_changed"]


def test_null_items_suppress_removals(ledger, source):
    ledger.snapshot(snapshot(source), None)
    incomplete_identity = Snapshot(
        "playlist1", "Test", "v2", (PlaylistItem(0, None, "missing"),), True
    )
    diff, _ = ledger.snapshot(incomplete_identity, None)
    assert not diff["removals_evaluated"]


def test_shared_track_across_playlists(ledger, source):
    ledger.snapshot(snapshot(source), "DJ")
    ledger.snapshot(replace(snapshot(source), id="playlist2"), "Vibes")
    assert ledger.connection.execute("SELECT count(*) FROM tracks").fetchone()[0] == 1
    assert ledger.connection.execute("SELECT count(*) FROM desired_memberships").fetchone()[0] == 2


def test_partial_failure_retries_without_losing_success(ledger, source, target):
    spotify = SpotifyStub(snapshot(source, ("s1", "s2")))

    class PartialTidal(TidalStub):
        def candidates(self, source):
            if source.id == "s2" and self.fail:
                raise SyncError("temporary failure")
            return super().candidates(source)

    tidal = PartialTidal(target)
    tidal.fail = True

    # Fail only the second track.
    def selective(source):
        tidal.calls += 1
        if source.id == "s2" and tidal.fail:
            raise SyncError("temporary failure")
        return [target]

    tidal.candidates = selective
    first = run_poc(spotify, tidal, ledger, "playlist1", "US")
    assert first["counts"] == {"matched": 1, "error": 1}
    tidal.fail = False
    second = run_poc(spotify, tidal, ledger, "playlist1", "US")
    assert second["counts"] == {"matched": 2}
    assert second["rows"][0]["cached"]
    assert not second["rows"][1]["cached"]
    assert tidal.calls == 3


def test_cache_invalidated_by_metadata_country_or_ttl(ledger, source, target):
    _, ids = ledger.snapshot(snapshot(source), None)
    run_id = ledger.begin("playlist1")
    key = cache_key(source, "US")
    ledger.checkpoint(run_id, ids[source.id], "US", key, match_track(source, [target]))
    assert ledger.cached(ids[source.id], key, "US")
    assert ledger.cached(ids[source.id], key, "GB") is None
    changed = cache_key(replace(source, title="Other Song"), "US")
    assert ledger.cached(ids[source.id], changed, "US") is None
    ledger.connection.execute("UPDATE matches SET updated=0")
    ledger.connection.commit()
    assert ledger.cached(ids[source.id], key, "US") is None


def test_interrupted_run_recovery(ledger):
    old = ledger.begin("p")
    new = ledger.begin("p")
    assert (
        ledger.connection.execute("SELECT status FROM runs WHERE id=?", (old,)).fetchone()[0]
        == "interrupted"
    )
    assert (
        ledger.connection.execute("SELECT status FROM runs WHERE id=?", (new,)).fetchone()[0]
        == "running"
    )


def test_transaction_rolls_back_partial_snapshot(ledger, source):
    ledger.snapshot(snapshot(source), "DJ")
    original = ledger._track

    def fail(track):
        original(track)
        raise RuntimeError("simulated interruption")

    ledger._track = fail
    with pytest.raises(RuntimeError):
        ledger.snapshot(snapshot(source, ("s2",), name="Changed"), "DJ")
    assert ledger.connection.execute("SELECT name FROM spotify_playlists").fetchone()[0] == "Test"
    assert ledger.connection.execute("SELECT count(*) FROM tracks").fetchone()[0] == 1


def test_destination_diff_is_idempotent_and_never_duplicates():
    first = plan_additions(["a", "a", "b", "c"], ["b"])
    assert first == ["a", "c"]
    assert plan_additions(["a", "a", "b", "c"], ["b", *first]) == []


def test_refresh_error_does_not_reuse_old_match(ledger, source, target):
    spotify, tidal = SpotifyStub(snapshot(source)), TidalStub(target)
    run_poc(spotify, tidal, ledger, "playlist1", "US")
    tidal.fail = True
    failed = run_poc(spotify, tidal, ledger, "playlist1", "US", refresh=True)
    assert failed["counts"] == {"error": 1}
    tidal.fail = False
    recovered = run_poc(spotify, tidal, ledger, "playlist1", "US")
    assert not recovered["rows"][0]["cached"]
    assert tidal.calls == 3


def test_rate_limit_defers_remaining_tracks(ledger, source, target):
    from dj_sync.errors import ApiError

    spotify, tidal = SpotifyStub(snapshot(source, ("s1", "s2", "s3"))), TidalStub(target)

    def limited(source):
        tidal.calls += 1
        raise ApiError("tidal", 429)

    tidal.candidates = limited
    report = run_poc(spotify, tidal, ledger, "playlist1", "US")
    assert report["counts"] == {"error": 3}
    assert tidal.calls == 1
