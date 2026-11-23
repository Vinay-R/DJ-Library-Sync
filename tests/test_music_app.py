"""Read-only Music.app index: folder resolution, memberships, ownership, reconciliation."""

import json
from dataclasses import replace

import pytest

from dj_sync.db import Ledger
from dj_sync.errors import SyncError
from dj_sync.local_library import LocalFile, index_files, match_local
from dj_sync.models import PlaylistItem, Snapshot
from dj_sync.music_app import (
    INDEX_SCRIPT,
    MEMBERS_SCRIPT,
    MusicApp,
    build_report,
    index_music,
    osascript_runner,
    resolve_folders,
)
from dj_sync.pipeline import run_playlist

LIB = "/Users/me/Music/Music/iTunes/iTunes Music"


def catalog(playlists=None, tracks=None):
    base_playlists = [
        {
            "id": "LIB1",
            "name": "Library",
            "kind": "Library",
            "cls": "libraryPlaylist",
            "parent": None,
        },
        {
            "id": "F-DJ",
            "name": "Nu DJ Vibes",
            "kind": "folder",
            "cls": "folderPlaylist",
            "parent": None,
        },
        {
            "id": "F-VB",
            "name": "Nu Vibes Vibes",
            "kind": "folder",
            "cls": "folderPlaylist",
            "parent": None,
        },
        {"id": "F-OLD", "name": "Vibes", "kind": "folder", "cls": "folderPlaylist", "parent": None},
        {
            "id": "P-4F",
            "name": "Floor Fillers",
            "kind": "none",
            "cls": "userPlaylist",
            "parent": "F-DJ",
        },
        {
            "id": "P-SUB",
            "name": "Deep",
            "kind": "folder",
            "cls": "folderPlaylist",
            "parent": "F-DJ",
        },
        {
            "id": "P-DH",
            "name": "Deep House",
            "kind": "none",
            "cls": "userPlaylist",
            "parent": "P-SUB",
        },
        {
            "id": "P-SETS",
            "name": "Floor Fillers",
            "kind": "none",
            "cls": "userPlaylist",
            "parent": "F-OLD",
        },
        {
            "id": "P-TRAX",
            "name": "Old Trax",
            "kind": "none",
            "cls": "userPlaylist",
            "parent": None,
        },
    ]
    base_tracks = [
        {
            "id": "T1",
            "name": "Night Drive (Extended Mix)",
            "artist": "Artist",
            "duration": 420.0,
            "location": f"{LIB}/Artist/A/01 Night Drive (Extended Mix).aiff",
        },
        {
            "id": "T2",
            "name": "Undertow",
            "artist": "Nova Hale",
            "duration": 200.5,
            "location": f"{LIB}/Nova Hale/1M/03 Undertow.aiff",
        },
        {"id": "T3", "name": "Ghost", "artist": "Someone", "duration": 100.0, "location": None},
    ]
    return {
        "playlists": base_playlists if playlists is None else playlists,
        "tracks": base_tracks if tracks is None else tracks,
    }


def members(mapping):
    return mapping


class FakeRunner:
    def __init__(self, index, memberships):
        self.index, self.memberships, self.calls = index, memberships, []

    def __call__(self, script, args):
        self.calls.append((script, args))
        if script is INDEX_SCRIPT:
            return json.dumps(self.index)
        assert script is MEMBERS_SCRIPT
        wanted = json.loads(args[0])
        return json.dumps({pid: self.memberships.get(pid, []) for pid in wanted})


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.sqlite3")
    yield db
    db.close()


def test_resolve_folders_requires_exactly_one_match_per_group():
    folders = resolve_folders(catalog()["playlists"])
    assert folders == {"DJ": "F-DJ", "Vibes": "F-VB"}
    twice = catalog()["playlists"] + [
        {
            "id": "F-DJ2",
            "name": "Nu DJ Vibes",
            "kind": "folder",
            "cls": "folderPlaylist",
            "parent": None,
        }
    ]
    with pytest.raises(SyncError, match="exactly one"):
        resolve_folders(twice)
    not_folder = [
        p if p["id"] != "F-VB" else {**p, "kind": "none", "cls": "userPlaylist"}
        for p in catalog()["playlists"]
    ]
    with pytest.raises(SyncError, match="Nu Vibes Vibes"):
        resolve_folders(not_folder)


def test_index_stores_playlists_tracks_and_memberships_under_managed_folders(ledger):
    runner = FakeRunner(catalog(), {"P-4F": ["T1", "T2"], "P-DH": ["T2"], "P-SETS": ["T3"]})
    summary = index_music(ledger, MusicApp(runner), confirm_folders=True)
    assert summary["folders"] == {"DJ": "F-DJ", "Vibes": "F-VB"}
    assert summary["playlists_indexed"] == 9 and summary["tracks_indexed"] == 3
    # Memberships are only requested for playlists inside the managed folders.
    requested = json.loads(runner.calls[1][1][0])
    assert sorted(requested) == ["P-4F", "P-DH"]
    rows = ledger.connection.execute(
        "SELECT playlist_id, track_id, position FROM music_memberships ORDER BY 1,3"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("P-4F", "T1", 0), ("P-4F", "T2", 1), ("P-DH", "T2", 0)]
    folders = ledger.connection.execute(
        "SELECT group_name, persistent_id FROM music_folders"
    ).fetchall()
    assert sorted(tuple(r) for r in folders) == [("DJ", "F-DJ"), ("Vibes", "F-VB")]
    assert summary["writes_performed"] == 0


def test_index_without_confirmation_reports_but_does_not_store_folders(ledger):
    runner = FakeRunner(catalog(), {})
    summary = index_music(ledger, MusicApp(runner), confirm_folders=False)
    assert summary["folders"] == {"DJ": "F-DJ", "Vibes": "F-VB"}
    assert ledger.connection.execute("SELECT count(*) FROM music_folders").fetchone()[0] == 0


class Spotify:
    def __init__(self, items, name="Floor Fillers"):
        self.snap = Snapshot("playlist1", name, "v1", tuple(items), True)

    def full(self, playlist_id):
        return self.snap


class Catalog:
    def __init__(self, target):
        self.target = target

    def candidates(self, source):
        title, _, version = source.title.partition(" - ")
        return [replace(self.target, id="t-" + source.id, title=title, version=version)]


def test_music_tracks_count_as_owned_even_when_scanner_cannot_read_the_file(ledger, source, target):
    unreadable = replace(
        source, id="s2", title="Undertow", artists=("Nova Hale",), duration_ms=200500, isrc=None
    )
    items = [PlaylistItem(0, source), PlaylistItem(1, unreadable)]
    run_playlist(Spotify(items), Catalog(target), ledger, "playlist1", "US", "DJ")
    # The scanner found only the first file; the second exists in Music.app but is unreadable.
    index_files(
        ledger,
        [
            LocalFile(
                f"{LIB}/Artist/A/01 Night Drive (Extended Mix).aiff",
                1,
                1,
                "Night Drive (Extended Mix)",
                ("Artist",),
                "A",
                420000,
                None,
                "tags",
            )
        ],
    )
    match_local(ledger, {"playlist1": "DJ"})
    index_music(ledger, MusicApp(FakeRunner(catalog(), {"P-4F": ["T1"]})), confirm_folders=True)
    status = dict(
        ledger.connection.execute(
            "SELECT track_id, status FROM sync_status WHERE stage='local_file_detected'"
        ).fetchall()
    )
    assert list(status.values()) == ["found", "found"]
    assets = ledger.connection.execute(
        "SELECT source, music_persistent_id, import_verified FROM local_assets ORDER BY source"
    ).fetchall()
    assert [tuple(r) for r in assets] == [("music", "T2", 1), ("scan", "T1", 1)]
    # A later local match keeps the Music.app-derived ownership.
    match_local(ledger, {"playlist1": "DJ"})
    again = ledger.connection.execute(
        "SELECT count(*) FROM sync_status WHERE stage='local_file_detected' AND status='found'"
    ).fetchone()[0]
    assert again == 2


def test_report_maps_playlists_by_stored_id_then_exact_name_inside_folder(ledger, source, target):
    items = [
        PlaylistItem(0, source),
        PlaylistItem(1, replace(source, id="s2", title="Hit", isrc=None)),
    ]
    run_playlist(Spotify(items), Catalog(target), ledger, "playlist1", "US", "DJ")
    index_files(
        ledger,
        [
            LocalFile(
                f"{LIB}/Artist/A/01 Night Drive (Extended Mix).aiff",
                1,
                1,
                "Night Drive (Extended Mix)",
                ("Artist",),
                "A",
                420000,
                None,
                "tags",
            )
        ],
    )
    match_local(ledger, {"playlist1": "DJ"})
    index_music(ledger, MusicApp(FakeRunner(catalog(), {"P-4F": ["T2"]})), confirm_folders=True)
    report = build_report(ledger, {"playlist1": "DJ"})
    entry = report["playlists"][0]
    # Name matches "Floor Fillers" twice in Music, but only one is inside Nu DJ Vibes.
    assert entry["destination"] == {"id": "P-4F", "name": "Floor Fillers", "how": "name"}
    by_id = {t["spotify_id"]: t["action"] for t in entry["tracks"]}
    assert by_id == {"s1": "add_existing", "s2": "download_pending"}
    assert entry["local_only"] == ["T2"]
    assert report["writes_performed"] == 0


def test_report_flags_missing_destination_and_ambiguous_names(ledger, source, target):
    run_playlist(
        Spotify([PlaylistItem(0, source)], name="Fresh Batch"),
        Catalog(target),
        ledger,
        "playlist1",
        "US",
        "Vibes",
    )
    playlists = catalog()["playlists"] + [
        {"id": "X1", "name": "Dup", "kind": "none", "cls": "userPlaylist", "parent": "F-VB"},
        {"id": "X2", "name": "Dup", "kind": "none", "cls": "userPlaylist", "parent": "F-VB"},
    ]
    index_music(
        ledger, MusicApp(FakeRunner(catalog(playlists=playlists), {})), confirm_folders=True
    )
    report = build_report(ledger, {"playlist1": "Vibes"})
    assert report["playlists"][0]["destination"] == {
        "id": None,
        "name": "Fresh Batch",
        "how": "create",
    }
    run_playlist(
        Spotify([PlaylistItem(0, source)], name="Dup"),
        Catalog(target),
        ledger,
        "playlist1",
        "US",
        "Vibes",
    )
    report = build_report(ledger, {"playlist1": "Vibes"})
    assert report["playlists"][0]["destination"]["how"] == "ambiguous"


def test_report_requires_confirmed_folders(ledger):
    with pytest.raises(SyncError, match="confirm-folders"):
        build_report(ledger, {"playlist1": "DJ"})


def test_osascript_runner_explains_permission_and_not_running_errors(monkeypatch):
    import subprocess

    class Done:
        def __init__(self, code, err):
            self.returncode, self.stdout, self.stderr = code, "", err

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["osascript", "-l", "JavaScript"]
        assert kwargs["timeout"] >= 60
        return Done(1, "execution error: Not authorized to send Apple events to Music. (-1743)")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SyncError, match="Automation"):
        osascript_runner("return 1;", [])


def test_report_marks_files_on_disk_but_not_in_music_as_import_from_disk(ledger, source, target):
    run_playlist(
        Spotify([PlaylistItem(0, source)]), Catalog(target), ledger, "playlist1", "US", "DJ"
    )
    index_files(
        ledger,
        [
            LocalFile(
                "/elsewhere/Night Drive (Extended Mix).aiff",
                1,
                1,
                "Night Drive (Extended Mix)",
                ("Artist",),
                "A",
                420000,
                None,
                "tags",
            )
        ],
    )
    match_local(ledger, {"playlist1": "DJ"})
    index_music(ledger, MusicApp(FakeRunner(catalog(tracks=[]), {})), confirm_folders=True)
    report = build_report(ledger, {"playlist1": "DJ"})
    track = report["playlists"][0]["tracks"][0]
    assert track["action"] == "import_from_disk"
    assert track["disk_file"].endswith("Night Drive (Extended Mix).aiff")


def test_index_tolerates_entries_without_names_or_ids(ledger):
    data = catalog()
    data["playlists"].append(
        {"id": "P-NONAME", "kind": "none", "cls": "userPlaylist", "parent": "F-DJ"}
    )
    data["playlists"].append(
        {"name": "ghost", "kind": "none", "cls": "userPlaylist", "parent": None}
    )
    data["tracks"].append({"id": "T-NONAME", "artist": None, "duration": None, "location": None})
    summary = index_music(ledger, MusicApp(FakeRunner(data, {})), confirm_folders=False)
    assert summary["playlists_indexed"] == 10 and summary["tracks_indexed"] == 4
    name = ledger.connection.execute(
        "SELECT name FROM music_playlists WHERE persistent_id='P-NONAME'"
    ).fetchone()[0]
    assert name == ""


def test_tracks_without_a_file_never_count_as_owned_and_stale_links_are_cleared(
    ledger, source, target
):
    run_playlist(
        Spotify([PlaylistItem(0, source)]), Catalog(target), ledger, "playlist1", "US", "DJ"
    )
    dead = {
        "id": "T-DEAD",
        "name": "Night Drive (Extended Mix)",
        "artist": "Artist",
        "duration": 420.0,
        "location": None,
    }
    index_music(ledger, MusicApp(FakeRunner(catalog(tracks=[dead]), {})), confirm_folders=True)
    assert (
        ledger.connection.execute(
            "SELECT count(*) FROM local_assets WHERE music_persistent_id='T-DEAD'"
        ).fetchone()[0]
        == 0
    )
    assert (
        ledger.connection.execute(
            "SELECT count(*) FROM sync_status WHERE stage='local_file_detected'"
        ).fetchone()[0]
        == 0
    )
    # A link recorded earlier to a track that has since lost its file is cleared on re-index.
    track_id = ledger.connection.execute("SELECT id FROM tracks").fetchone()[0]
    ledger.connection.execute(
        "INSERT INTO local_assets(track_id,path,source,music_persistent_id,import_verified) "
        "VALUES(?,?,'music','T-DEAD',1)",
        (track_id, "music:T-DEAD"),
    )
    ledger.connection.commit()
    index_music(ledger, MusicApp(FakeRunner(catalog(tracks=[dead]), {})), confirm_folders=True)
    assert (
        ledger.connection.execute(
            "SELECT count(*) FROM local_assets WHERE music_persistent_id='T-DEAD'"
        ).fetchone()[0]
        == 0
    )


class FlakyRunner(FakeRunner):
    """Music.app failing to report `parent` for the first ``bad`` index reads."""

    def __init__(self, index, memberships, bad):
        super().__init__(index, memberships)
        self.bad = bad

    def __call__(self, script, args):
        if script is INDEX_SCRIPT and self.bad:
            self.bad -= 1
            self.calls.append((script, args))
            flat = [{**p, "parent": None} for p in self.index["playlists"]]
            return json.dumps({**self.index, "playlists": flat})
        return super().__call__(script, args)


def _link(ledger, music_id="P-4F", group="DJ"):
    ledger.connection.execute(
        "INSERT INTO spotify_playlists(id, name, group_name, music_persistent_id) "
        "VALUES('sp1', 'Floor Fillers', ?, ?)",
        (group, music_id),
    )
    ledger.connection.commit()


def test_index_with_a_flattened_tree_is_retried_then_recorded(ledger):
    index_music(ledger, MusicApp(FakeRunner(catalog(), {"P-4F": ["T1"]})), confirm_folders=True)
    _link(ledger)
    runner = FlakyRunner(catalog(), {"P-4F": ["T1"]}, bad=1)
    summary = index_music(ledger, MusicApp(runner), confirm_folders=False, sleep=lambda _: None)
    assert summary["managed_playlists"] == 2
    assert sum(1 for s, _ in runner.calls if s is INDEX_SCRIPT) == 2


def test_index_that_stays_flattened_records_nothing(ledger):
    index_music(ledger, MusicApp(FakeRunner(catalog(), {"P-4F": ["T1"]})), confirm_folders=True)
    _link(ledger)
    before = ledger.connection.execute("SELECT count(*) FROM music_memberships").fetchone()[0]
    runner = FlakyRunner(catalog(), {"P-4F": ["T1"]}, bad=99)
    with pytest.raises(SyncError, match="looks incomplete"):
        index_music(ledger, MusicApp(runner), confirm_folders=False, sleep=lambda _: None)
    after = ledger.connection.execute("SELECT count(*) FROM music_memberships").fetchone()[0]
    assert after == before == 1
    parent = ledger.connection.execute(
        "SELECT parent_id FROM music_playlists WHERE persistent_id='P-4F'"
    ).fetchone()[0]
    assert parent == "F-DJ"


def test_destinations_outside_the_managed_groups_do_not_block_the_index(ledger):
    # A one-time copy into Sets is linked but deliberately outside the managed folders.
    _link(ledger, music_id="P-SETS", group="Sets")
    summary = index_music(
        ledger, MusicApp(FakeRunner(catalog(), {})), confirm_folders=True, sleep=lambda _: None
    )
    assert summary["managed_playlists"] == 2
