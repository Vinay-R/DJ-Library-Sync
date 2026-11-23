"""Additive TIDAL playlists for mapped Spotify playlists: create once, add missing, verify."""

import json
import time

import httpx
import pytest
from test_tidal_test_playlist import Remote, Token

from dj_sync.cli import parser
from dj_sync.db import Ledger
from dj_sync.errors import ApiError, SyncError
from dj_sync.tidal_sync import (
    apply_mapped_playlist,
    load_mapped_plans,
    sync_mapped_playlists,
    tidal_name,
)
from dj_sync.tidal_test_playlist import TestPlaylistAPI

SOURCE = "s" * 22


class BatchRemote(Remote):
    def __init__(self):
        super().__init__()
        self.batch_sizes = []
        self.page_size = 20

    def handle(self, request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/relationships/items"):
            self.batch_sizes.append(len(json.loads(request.content)["data"]))
        if request.method == "GET" and path.endswith("/relationships/items"):
            # Paginate like TIDAL so readback must follow cursors.
            id = path.split("/")[3]
            start = int(request.url.params.get("page[cursor]", "0"))
            items = self.items[id]
            if self.bad_readback and items:
                items = items[:-1]
            page = items[start : start + self.page_size]
            body = {"data": [{"type": "tracks", "id": t} for t in page]}
            if start + self.page_size < len(items):
                body["links"] = {
                    "next": f"/v2/playlists/{id}/relationships/items"
                    f"?page[cursor]={start + self.page_size}"
                }
            self.calls.append(request)
            return httpx.Response(200, json=body)
        return super().handle(request)

    def api(self, apply=True, token=None):
        return TestPlaylistAPI(
            token or Token(),
            httpx.Client(transport=httpx.MockTransport(self.handle)),
            0,
            "US",
            apply=apply,
            max_batch=50,
        )

    def posts(self):
        return [c for c in self.calls if c.method == "POST"]


def make_plan(ids, name="Floor Fillers", group="DJ", source=SOURCE):
    return {
        "source_id": source,
        "source_name": name,
        "group": group,
        "snapshot_id": "snap",
        "report_run_id": "run",
        "name": tidal_name(group, name),
        "track_ids": list(ids),
        "unresolved": 1,
    }


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.sqlite3")
    yield db
    db.close()


def ids(n, start=0):
    return [str(1000 + i) for i in range(start, start + n)]


def test_name_uses_group_prefix_and_fits_tidal_limit():
    assert tidal_name("Vibes", "Jazz Picks") == "Vibes / Jazz Picks"
    assert len(tidal_name("DJ", "x" * 400)) == 250
    assert tidal_name("DJ", "  Night   Shift ") == "DJ / Night Shift"


def test_first_apply_creates_unlisted_playlist_and_adds_in_batches_of_50(tmp_path):
    remote = BatchRemote()
    result = apply_mapped_playlist(remote.api(), make_plan(ids(120)), tmp_path)
    created = [c for c in remote.posts() if c.url.path == "/v2/playlists"]
    assert len(created) == 1
    attrs = json.loads(created[0].content)["data"]["attributes"]
    assert attrs["name"] == "DJ / Floor Fillers"
    assert attrs["accessType"] == "UNLISTED"
    assert SOURCE in attrs["description"]
    assert remote.batch_sizes == [50, 50, 20]
    assert result["added_count"] == 120 and result["after_count"] == 120
    assert remote.items[result["playlist_id"]] == ids(120)  # Spotify order preserved
    assert "existing" in remote.playlists and remote.items["existing"] == ["old-track"]


def test_repeat_apply_is_a_no_op(tmp_path):
    remote = BatchRemote()
    first = apply_mapped_playlist(remote.api(), make_plan(ids(60)), tmp_path)
    before_posts = len(remote.posts())
    second = apply_mapped_playlist(remote.api(), make_plan(ids(60)), tmp_path)
    assert len(remote.posts()) == before_posts
    assert second["playlist_id"] == first["playlist_id"]
    assert second["added_count"] == second["post_requests"] == 0


def test_incremental_apply_adds_only_new_tracks(tmp_path):
    remote = BatchRemote()
    apply_mapped_playlist(remote.api(), make_plan(ids(10)), tmp_path)
    remote.batch_sizes.clear()
    result = apply_mapped_playlist(remote.api(), make_plan(ids(12)), tmp_path)
    assert remote.batch_sizes == [2]
    assert result["added_count"] == 2


def test_extras_and_spotify_removals_are_reported_never_removed(tmp_path):
    remote = BatchRemote()
    first = apply_mapped_playlist(remote.api(), make_plan(ids(5)), tmp_path)
    remote.items[first["playlist_id"]].append("hand-added")
    # Spotify removed the first two tracks since the last run.
    result = apply_mapped_playlist(remote.api(), make_plan(ids(3, start=2)), tmp_path)
    assert result["added_count"] == 0
    assert sorted(result["not_in_spotify"]) == sorted(["1000", "1001", "hand-added"])
    assert len(remote.items[first["playlist_id"]]) == 6  # nothing removed


def test_dry_run_sends_no_writes_and_previews_creation(tmp_path):
    remote = BatchRemote()
    preview = apply_mapped_playlist(remote.api(apply=False), make_plan(ids(7)), tmp_path)
    assert remote.posts() == []
    assert preview["mode"] == "dry_run"
    assert preview["action"] == "create"
    assert preview["would_add"] == 7
    assert not (tmp_path / "tidal-playlists").exists()


def test_dry_run_after_creation_previews_only_missing(tmp_path):
    remote = BatchRemote()
    apply_mapped_playlist(remote.api(), make_plan(ids(4)), tmp_path)
    count = len(remote.posts())
    preview = apply_mapped_playlist(remote.api(apply=False), make_plan(ids(6)), tmp_path)
    assert len(remote.posts()) == count
    assert preview["action"] == "update" and preview["would_add"] == 2


def test_lost_create_response_recovered_by_marker(tmp_path):
    remote = BatchRemote()
    remote.lose_create = True
    with pytest.raises(SyncError, match="uncertain"):
        apply_mapped_playlist(remote.api(), make_plan(ids(3)), tmp_path)
    result = apply_mapped_playlist(remote.api(), make_plan(ids(3)), tmp_path)
    assert len([p for p in remote.playlists if p != "existing"]) == 1
    assert result["after_count"] == 3


def test_lost_add_response_reconciled_without_duplicates(tmp_path):
    remote = BatchRemote()
    remote.lose_add = True
    with pytest.raises(SyncError, match="uncertain"):
        apply_mapped_playlist(remote.api(), make_plan(ids(70)), tmp_path)
    result = apply_mapped_playlist(remote.api(), make_plan(ids(70)), tmp_path)
    assert result["after_count"] == 70
    assert len(remote.items[result["playlist_id"]]) == 70


def test_bad_readback_is_not_reported_as_success(tmp_path):
    remote = BatchRemote()
    remote.bad_readback = True
    with pytest.raises(SyncError, match="readback"):
        apply_mapped_playlist(remote.api(), make_plan(ids(3)), tmp_path)


def test_intent_from_another_account_is_refused(tmp_path):
    remote = BatchRemote()
    apply_mapped_playlist(remote.api(), make_plan(ids(2)), tmp_path)
    path = next((tmp_path / "tidal-playlists").glob("*.json"))
    data = json.loads(path.read_text())
    data["user_id"] = "someone-else"
    path.write_text(json.dumps(data))
    with pytest.raises(SyncError, match="different"):
        apply_mapped_playlist(remote.api(), make_plan(ids(2)), tmp_path)


def test_renamed_spotify_playlist_keeps_destination_and_flags_rename(tmp_path):
    remote = BatchRemote()
    first = apply_mapped_playlist(remote.api(), make_plan(ids(2)), tmp_path)
    second = apply_mapped_playlist(remote.api(), make_plan(ids(2), name="Renamed"), tmp_path)
    assert second["playlist_id"] == first["playlist_id"]
    assert second["rename_pending"] == "DJ / Renamed"


# ---------- plan loading from the ledger ----------


def store_report(ledger, playlist, group, complete=True, age=0.0, status="completed", n=3):
    rows = [
        {
            "position": i,
            "source": {"id": f"sp{i}"},
            "match": {"status": "matched", "selected_id": f"t{i}"},
        }
        for i in range(n)
    ]
    rows.append({"position": n, "source": None, "match": {"status": "unavailable"}})
    report = {
        "playlist": {
            "id": playlist,
            "name": f"Name {playlist}",
            "group": group,
            "snapshot_id": "snap",
            "complete": complete,
        },
        "rows": rows,
        "plan": {"proposed_track_ids": [f"t{i}" for i in range(n)]},
    }
    run = ledger.begin(playlist)
    ledger.finish(run, status, report)
    ledger.connection.execute("UPDATE runs SET started=? WHERE id=?", (time.time() - age, run))
    ledger.connection.commit()
    return run


def test_load_plans_uses_latest_complete_fresh_report(ledger):
    store_report(ledger, "p1", "DJ", age=3600, n=2)
    latest = store_report(ledger, "p1", "DJ", n=3)
    store_report(ledger, "p1", "DJ", complete=False, n=1)  # newer sample is ignored
    plans, skipped = load_mapped_plans(ledger, {"p1": "DJ"})
    assert skipped == []
    assert plans[0]["report_run_id"] == latest
    assert plans[0]["track_ids"] == ["t0", "t1", "t2"]
    assert plans[0]["name"] == "DJ / Name p1"
    assert plans[0]["unresolved"] == 1


def test_load_plans_skips_stale_missing_or_regrouped_reports(ledger):
    store_report(ledger, "old", "DJ", age=2 * 86400)
    store_report(ledger, "moved", "DJ")
    store_report(ledger, "failed", "DJ", status="failed")
    mappings = {"old": "DJ", "moved": "Vibes", "never": "DJ", "failed": "DJ"}
    plans, skipped = load_mapped_plans(ledger, mappings)
    assert plans == []
    assert {s["id"] for s in skipped} == set(mappings)


# ---------- multi-playlist runs and ledger records ----------


def test_sync_records_verified_membership_and_continues_after_one_failure(tmp_path, ledger):
    remote = BatchRemote()
    store_report(ledger, "p" * 22, "DJ")
    good = make_plan(ids(3), source="p" * 22)
    bad = make_plan([], name="Empty", source="q" * 22)
    summary = sync_mapped_playlists(remote.api(), [bad, good], tmp_path, ledger)
    status = {r["source_id"]: r["status"] for r in summary["playlists"]}
    assert status == {"q" * 22: "skipped", "p" * 22: "verified"}
    row = ledger.connection.execute(
        "SELECT tidal_playlist_id FROM spotify_playlists WHERE id=?", ("p" * 22,)
    ).fetchone()
    assert row[0] == summary["playlists"][1]["playlist_id"]
    count = ledger.connection.execute("SELECT count(*) FROM tidal_memberships").fetchone()[0]
    assert count == 3
    assert summary["post_requests"] == 2


def test_sync_stops_everything_on_access_denied(tmp_path, ledger):
    remote = BatchRemote()
    remote.forbid_create = True
    plans = [make_plan(ids(2), source="a" * 22), make_plan(ids(2), source="b" * 22)]
    with pytest.raises(ApiError):
        sync_mapped_playlists(remote.api(), plans, tmp_path, ledger)
    assert len(remote.posts()) == 1


def test_batch_size_cannot_exceed_tidal_limit():
    with pytest.raises(SyncError):
        TestPlaylistAPI(Token(), httpx.Client(), 0, "US", max_batch=51)


def test_cli_tidal_sync_defaults_to_dry_run():
    args = parser().parse_args(["tidal-sync"])
    assert not args.apply
    assert parser().parse_args(["tidal-sync", "--apply"]).apply
