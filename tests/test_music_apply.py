"""Music.app placement: guarded playlist creation, adds, imports, intent, archive."""

import json
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest

from dj_sync.db import Ledger
from dj_sync.errors import SyncError
from dj_sync.local_library import LocalFile, index_files, match_local
from dj_sync.models import PlaylistItem, Snapshot
from dj_sync.music_app import (
    ADD_FILE_SCRIPT,
    ANCESTRY_SCRIPT,
    CREATE_PLAYLIST_SCRIPT,
    DUPLICATE_SCRIPT,
    FIND_TRACK_SCRIPT,
    INDEX_SCRIPT,
    MEMBERS_SCRIPT,
    SET_ARTWORK_SCRIPT,
    MusicApp,
    index_music,
)
from dj_sync.music_apply import apply_music
from dj_sync.pipeline import run_playlist

MEDIA = "/Users/me/Music/Music/iTunes/iTunes Music"


class FakeMusic:
    """An in-memory Music.app: folders, playlists, tracks, and a media folder it copies into."""

    def __init__(self, media_dir: Path):
        self.media_dir = media_dir
        self.playlists = {
            "LIB1": {
                "name": "Library",
                "kind": "Library",
                "cls": "libraryPlaylist",
                "parent": None,
            },
            "F-DJ": {
                "name": "Nu DJ Vibes",
                "kind": "folder",
                "cls": "folderPlaylist",
                "parent": None,
            },
            "F-VB": {
                "name": "Nu Vibes Vibes",
                "kind": "folder",
                "cls": "folderPlaylist",
                "parent": None,
            },
            "F-OLD": {"name": "Vibes", "kind": "folder", "cls": "folderPlaylist", "parent": None},
            "P-OLD": {
                "name": "Floor Fillers",
                "kind": "none",
                "cls": "userPlaylist",
                "parent": "F-OLD",
            },
            "P-DH": {"name": "Deep House", "kind": "none", "cls": "userPlaylist", "parent": "F-DJ"},
        }
        self.tracks = {
            "T1": {
                "name": "Night Drive (Extended Mix)",
                "artist": "Artist",
                "duration": 420.0,
                "location": f"{MEDIA}/Artist/A/01 Night Drive (Extended Mix).aiff",
            },
        }
        self.members = {"P-DH": ["T1"]}
        self.calls = []
        self.fail_after_add = False
        self.hide_location_once = False
        self.hide_id_once = False
        self.add_skips_playlist_once = False
        self.stale_reads = 0
        self._snapshot = None
        self.reference_in_place = False
        self.counter = 0
        self.covers = {}

    def __call__(self, script, args):
        self.calls.append(script)
        arg = None
        if args and script not in (CREATE_PLAYLIST_SCRIPT, SET_ARTWORK_SCRIPT):
            arg = json.loads(args[0])
        if script is INDEX_SCRIPT:
            return json.dumps(
                {
                    "playlists": [{"id": k, **v} for k, v in self.playlists.items()],
                    "tracks": [{"id": k, **v} for k, v in self.tracks.items()],
                }
            )
        if script is MEMBERS_SCRIPT:
            if self.stale_reads and self._snapshot is not None:
                self.stale_reads -= 1
                return json.dumps({pid: list(self._snapshot.get(pid, [])) for pid in arg})
            self._snapshot = {k: list(v) for k, v in self.members.items()}
            return json.dumps({pid: list(self.members.get(pid, [])) for pid in arg})
        if script is ANCESTRY_SCRIPT:
            chain, cur = [], self.playlists.get(arg["playlist_id"], {}).get("parent")
            while cur:
                chain.append(cur)
                cur = self.playlists[cur]["parent"]
            return json.dumps({"exists": arg["playlist_id"] in self.playlists, "ancestors": chain})
        if script is CREATE_PLAYLIST_SCRIPT:
            folder_id, name = args  # AppleScript takes plain arguments, not JSON
            self.counter += 1
            pid = f"NEW{self.counter}"
            self.playlists[pid] = {
                "name": name,
                "kind": "none",
                "cls": "userPlaylist",
                "parent": folder_id,
            }
            self.members[pid] = []
            return f"{pid}|{folder_id}|{name}"
        if script is DUPLICATE_SCRIPT:
            out = []
            for tid in arg["track_ids"]:
                ok = tid in self.tracks
                if ok and tid not in self.members.setdefault(arg["playlist_id"], []):
                    self.members[arg["playlist_id"]].append(tid)
                out.append({"track_id": tid, "ok": ok})
            return json.dumps(out)
        if script is FIND_TRACK_SCRIPT:
            return json.dumps(
                [{"id": k, **v} for k, v in self.tracks.items() if v["name"] == arg["name"]]
            )
        if script is ADD_FILE_SCRIPT:
            src = Path(arg["path"])
            self.counter += 1
            tid = f"IMP{self.counter}"
            if self.reference_in_place or src.is_relative_to(self.media_dir):
                location = str(src)  # Music.app leaves files already in its folder in place
            else:
                dest = self.media_dir / "Imported" / src.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dest)
                location = str(dest)
            stem = src.stem.rsplit(" [", 1)[0]
            artist, _, title = stem.partition(" - ")
            # Music.app organizes copies into Artist/Album/NN Title, so the name changes.
            if not self.reference_in_place and not src.is_relative_to(self.media_dir):
                organized = self.media_dir / artist / "Album" / f"01 {title}{src.suffix}"
                organized.parent.mkdir(parents=True, exist_ok=True)
                Path(location).rename(organized)
                location = str(organized)
            self.tracks[tid] = {
                "name": title or src.stem,
                "artist": artist or "Artist",
                "duration": 0.2,
                "location": location,
                "added": time.time(),
            }
            if self.add_skips_playlist_once:
                self.add_skips_playlist_once = False
            else:
                self.members.setdefault(arg["playlist_id"], []).append(tid)
            if self.fail_after_add:
                self.fail_after_add = False
                raise RuntimeError("osascript died after the import")
            name = self.tracks[tid]["name"]
            if self.hide_id_once:
                self.hide_id_once = False
                return json.dumps({"id": None, "location": None, "duration": None, "name": None})
            if self.hide_location_once:  # Music.app still copying: id known, location not yet
                self.hide_location_once = False
                return json.dumps({"id": tid, "location": None, "duration": 0.2, "name": name})
            return json.dumps({"id": tid, "location": location, "duration": 0.2, "name": name})
        if script is SET_ARTWORK_SCRIPT:
            track_id, image = args
            self.covers[track_id] = Path(image).read_bytes()
            return "1"
        raise AssertionError(f"unexpected script: {script[:40]}")

    def writes(self):
        return [
            c
            for c in self.calls
            if c in (CREATE_PLAYLIST_SCRIPT, DUPLICATE_SCRIPT, ADD_FILE_SCRIPT)
        ]


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "data" / "ledger.sqlite3")
    yield db
    db.close()


@pytest.fixture
def music(tmp_path):
    return FakeMusic(tmp_path / "media")


class Spotify:
    def __init__(self, items, name):
        self.snap = Snapshot("playlist1", name, "v1", tuple(items), True)

    def full(self, playlist_id):
        return self.snap


class Catalog:
    def __init__(self, target):
        self.target = target

    def candidates(self, source):
        title, _, version = source.title.partition(" - ")
        return [
            replace(
                self.target,
                id="9" + source.id[1:],
                title=title,
                version=version,
                duration_ms=source.duration_ms,
                isrc=source.isrc,
            )
        ]


def seed(ledger, music, source, target, tmp_path, name="Floor Fillers"):
    """One Spotify playlist with: s1 (in Music.app), s2 (converted AIFF staged), s3 (missing)."""
    items = [
        PlaylistItem(0, source),
        PlaylistItem(1, replace(source, id="s2", title="Fresh", isrc=None, duration_ms=200)),
        PlaylistItem(2, replace(source, id="s3", title="Gone", isrc=None)),
    ]
    run_playlist(Spotify(items, name), Catalog(target), ledger, "playlist1", "US", "DJ")
    index_files(
        ledger,
        [
            LocalFile(
                music.tracks["T1"]["location"],
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
    index_music(ledger, MusicApp(music), confirm_folders=True)
    aiff_dir = tmp_path / "AIFF"
    aiff_dir.mkdir()
    aiff = aiff_dir / "Artist - Fresh [92].aiff"
    aiff.write_bytes(b"FORM\x00\x00\x00\x04AIFF")
    flac_dir = tmp_path / "Tracks"
    flac_dir.mkdir()
    flac = flac_dir / "Artist - Fresh [92].flac"
    flac.write_bytes(b"fLaC")
    track_id = ledger.connection.execute(
        "SELECT track_id FROM service_tracks WHERE service_id='s2'"
    ).fetchone()[0]
    ledger.connection.execute(
        "INSERT INTO downloads VALUES(?,'92','r','converted','tool',0,?,?,1)",
        (track_id, str(flac), str(aiff)),
    )
    ledger.connection.commit()
    return {"aiff": aiff, "flac": flac, "track_id": track_id}


def test_dry_run_plans_but_writes_nothing(ledger, music, source, target, tmp_path):
    seed(ledger, music, source, target, tmp_path)
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=False
    )
    assert result["mode"] == "dry_run"
    entry = result["playlists"][0]
    assert entry["destination"]["how"] == "create"
    assert entry["planned"] == {"add_existing": 1, "import": 1, "download_pending": 1}
    assert music.writes() == []
    assert result["writes_performed"] == 0


def test_apply_creates_playlist_in_folder_adds_and_imports_then_is_idempotent(
    ledger, music, source, target, tmp_path
):
    staged = seed(ledger, music, source, target, tmp_path)
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True
    )
    assert result["mode"] == "applied"
    entry = result["playlists"][0]
    assert entry["destination"]["id"] == "NEW1" and music.playlists["NEW1"]["parent"] == "F-DJ"
    assert entry["done"] == {"created": 1, "added": 1, "imported": 1}
    assert set(music.members["NEW1"]) == {"T1", "IMP2"}  # membership, not order, is managed
    # Ledger: destination stored, import verified, membership recorded.
    stored = ledger.connection.execute(
        "SELECT music_persistent_id FROM spotify_playlists WHERE id='playlist1'"
    ).fetchone()[0]
    assert stored == "NEW1"
    asset = ledger.connection.execute(
        "SELECT source, music_persistent_id, import_verified, path FROM local_assets "
        "WHERE track_id=?",
        (staged["track_id"],),
    ).fetchone()
    assert tuple(asset[:3]) == ("music", "IMP2", 1) and asset[3].startswith(str(tmp_path / "media"))
    assert (
        ledger.connection.execute("SELECT status FROM downloads WHERE tidal_id='92'").fetchone()[0]
        == "imported"
    )
    stages = dict(
        ledger.connection.execute(
            "SELECT stage, count(*) FROM sync_status WHERE stage IN "
            "('music_imported','music_playlist_member') GROUP BY stage"
        ).fetchall()
    )
    assert stages == {"music_imported": 1, "music_playlist_member": 2}
    # Backup export happened before the first write.
    assert list((tmp_path / "data" / "backups").glob("music-playlists-*.json"))
    # Second apply: nothing to do.
    before = len(music.writes())
    again = apply_music(ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True)
    assert len(music.writes()) == before
    assert again["playlists"][0]["done"] == {}


def test_existing_playlist_with_same_name_inside_folder_is_adopted(
    ledger, music, source, target, tmp_path
):
    seed(ledger, music, source, target, tmp_path, name="Deep House")
    apply_music(ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True)
    assert CREATE_PLAYLIST_SCRIPT not in music.calls
    assert music.members["P-DH"][0] == "T1" and len(music.members["P-DH"]) == 2


def test_guard_refuses_playlists_outside_confirmed_folders(ledger, music, source, target, tmp_path):
    seed(ledger, music, source, target, tmp_path)
    # Pretend the stored destination points at the legacy "Floor Fillers" under the old folder.
    ledger.connection.execute(
        "UPDATE spotify_playlists SET music_persistent_id='P-OLD' WHERE id='playlist1'"
    )
    ledger.connection.commit()
    with pytest.raises(SyncError, match="outside"):
        apply_music(ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True)
    assert music.writes() == []


def test_ambiguous_destination_is_skipped_without_writes(ledger, music, source, target, tmp_path):
    music.playlists["X1"] = {"name": "Dup", "kind": "none", "cls": "userPlaylist", "parent": "F-DJ"}
    music.playlists["X2"] = {"name": "Dup", "kind": "none", "cls": "userPlaylist", "parent": "F-DJ"}
    seed(ledger, music, source, target, tmp_path, name="Dup")
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True
    )
    assert result["playlists"][0]["status"] == "skipped"
    assert music.writes() == []


def test_interrupted_import_is_adopted_on_rerun_without_a_second_add(
    ledger, music, source, target, tmp_path
):
    seed(ledger, music, source, target, tmp_path)
    music.fail_after_add = True
    with pytest.raises(SyncError):
        apply_music(ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True)
    adds_before = music.calls.count(ADD_FILE_SCRIPT)
    intents = list((tmp_path / "data" / "music-imports").glob("*.json"))
    assert intents and json.loads(intents[0].read_text())["status"] == "add_sent"
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True
    )
    assert music.calls.count(ADD_FILE_SCRIPT) == adds_before
    assert result["playlists"][0]["done"].get("adopted_import") == 1
    assert len([t for t in music.tracks if t.startswith("IMP")]) == 1


def test_archive_moves_only_tool_files_after_verified_copy_import(
    ledger, music, source, target, tmp_path
):
    staged = seed(ledger, music, source, target, tmp_path)
    apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True, archive=True
    )
    assert not staged["aiff"].exists() and not staged["flac"].exists()
    assert (staged["aiff"].parent / "imported" / staged["aiff"].name).exists()
    assert (staged["flac"].parent / "imported" / staged["flac"].name).exists()
    row = ledger.connection.execute("SELECT flac_path, aiff_path FROM downloads").fetchone()
    assert "/imported/" in row[0] and "/imported/" in row[1]


def test_archive_never_moves_preexisting_or_referenced_files(
    ledger, music, source, target, tmp_path
):
    staged = seed(ledger, music, source, target, tmp_path)
    ledger.connection.execute("UPDATE downloads SET origin='preexisting'")
    ledger.connection.commit()
    apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True, archive=True
    )
    assert staged["aiff"].exists() and staged["flac"].exists()


def test_referenced_in_place_import_is_flagged_and_not_archived(
    ledger, music, source, target, tmp_path
):
    staged = seed(ledger, music, source, target, tmp_path)
    music.reference_in_place = True
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True, archive=True
    )
    assert result["playlists"][0]["done"]["imported"] == 1
    assert result["referenced_in_place"] == [str(staged["aiff"])]
    assert staged["aiff"].exists()


def test_only_and_max_writes_limit_a_careful_first_run(ledger, music, source, target, tmp_path):
    seed(ledger, music, source, target, tmp_path)
    result = apply_music(
        ledger,
        MusicApp(music),
        {"playlist1": "DJ", "other": "DJ"},
        tmp_path / "data",
        apply=True,
        only={"playlist1"},
        max_writes=1,
    )
    entry = result["playlists"][0]
    # Playlist creation does not count; exactly one track write happened.
    assert entry["done"] == {"created": 1, "added": 1}
    assert entry["deferred_by_limit"] == 1
    assert len([c for c in music.calls if c is ADD_FILE_SCRIPT]) == 0
    again = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True, max_writes=5
    )
    assert again["playlists"][0]["done"] == {"imported": 1}


def test_files_on_disk_but_not_in_music_are_added_back_and_placed(
    ledger, music, source, target, tmp_path
):
    seed(ledger, music, source, target, tmp_path)
    # s3 ("Gone") has a file on disk that Music.app does not list.
    on_disk = tmp_path / "media" / "Artist" / "G" / "01 Gone.aiff"
    on_disk.parent.mkdir(parents=True)
    on_disk.write_bytes(b"FORM\x00\x00\x00\x04AIFF")
    index_files(
        ledger,
        [
            LocalFile(
                music.tracks["T1"]["location"],
                1,
                1,
                "Night Drive (Extended Mix)",
                ("Artist",),
                "A",
                420000,
                None,
                "tags",
            ),
            LocalFile(str(on_disk), 1, 1, "Gone", ("Artist",), "G", 420000, None, "tags"),
        ],
    )
    match_local(ledger, {"playlist1": "DJ"})
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True, archive=True
    )
    entry = result["playlists"][0]
    assert entry["done"]["imported_from_disk"] == 1
    assert result["referenced_in_place"] == []  # disk files are expected to stay in place
    track_id = ledger.connection.execute(
        "SELECT track_id FROM service_tracks WHERE service_id='s3'"
    ).fetchone()[0]
    row = ledger.connection.execute(
        "SELECT music_persistent_id, import_verified, source FROM local_assets WHERE track_id=?",
        (track_id,),
    ).fetchone()
    assert row[0] and row[1] == 1
    assert on_disk.exists()  # never archived: it was already library audio
    again = apply_music(ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True)
    assert "imported_from_disk" not in again["playlists"][0]["done"]


def test_archive_also_covers_imports_from_earlier_runs(ledger, music, source, target, tmp_path):
    staged = seed(ledger, music, source, target, tmp_path)
    apply_music(ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True)
    assert staged["aiff"].exists()  # no archive flag on the first run
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True, archive=True
    )
    assert result["archived"] == 1
    assert not staged["aiff"].exists() and not staged["flac"].exists()


def test_import_without_immediate_location_is_resolved_by_lookup(
    ledger, music, source, target, tmp_path
):
    seed(ledger, music, source, target, tmp_path)
    music.hide_location_once = True  # Music.app still copying: id known, location not yet
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True, archive=True
    )
    assert result["playlists"][0]["done"]["imported"] == 1
    assert music.calls.count(FIND_TRACK_SCRIPT) >= 1
    row = ledger.connection.execute("SELECT path FROM local_assets WHERE source='music'").fetchone()
    assert row[0].startswith(str(tmp_path / "media"))
    assert result["archived"] == 1


def test_import_whose_response_lacks_an_id_is_resolved_by_lookup(
    ledger, music, source, target, tmp_path
):
    seed(ledger, music, source, target, tmp_path)
    music.hide_id_once = True  # Music.app imported the file but returned an unusable reference
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True
    )
    assert result["playlists"][0]["done"]["imported"] == 1
    assert len([t for t in music.tracks if t.startswith("IMP")]) == 1
    assert (
        ledger.connection.execute(
            "SELECT count(*) FROM local_assets WHERE source='music'"
        ).fetchone()[0]
        == 1
    )


def test_same_song_already_in_playlist_under_another_library_entry_is_not_added_again(
    ledger, music, source, target, tmp_path
):
    # The playlist already holds a second library copy of "Night Drive (Extended Mix)".
    music.tracks["T1b"] = {
        **music.tracks["T1"],
        "location": f"{MEDIA}/Artist/Other/01 Night Drive (Extended Mix).aiff",
    }
    music.playlists["P-4F"] = {
        "name": "Floor Fillers",
        "kind": "none",
        "cls": "userPlaylist",
        "parent": "F-DJ",
    }
    music.members["P-4F"] = ["T1b"]
    seed(ledger, music, source, target, tmp_path)
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True
    )
    entry = result["playlists"][0]
    assert entry["destination"]["id"] == "P-4F"
    assert "added" not in entry["done"]
    assert (
        music.members["P-4F"].count("T1") == 0 and len(music.members["P-4F"]) == 2
    )  # T1b + import


def test_track_present_via_another_library_copy_is_never_added_again(
    ledger, music, source, target, tmp_path
):
    # Spotify s1 is linked to two library copies; the playlist already holds the second.
    music.tracks["T1b"] = {
        **music.tracks["T1"],
        "name": "Night Drive (Extended Mix)",
        "location": f"{MEDIA}/Artist/Other/01 Night Drive (Extended Mix).aiff",
    }
    music.playlists["P-4F"] = {
        "name": "Floor Fillers",
        "kind": "none",
        "cls": "userPlaylist",
        "parent": "F-DJ",
    }
    music.members["P-4F"] = ["T1b"]
    seed(ledger, music, source, target, tmp_path)
    from dj_sync.music_app import build_report

    track = next(
        t
        for t in build_report(ledger, {"playlist1": "DJ"})["playlists"][0]["tracks"]
        if t["spotify_id"] == "s1"
    )
    assert track["action"] == "in_playlist" and track["music_track_id"] == "T1b"
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True
    )
    assert "added" not in result["playlists"][0]["done"]
    assert music.members["P-4F"].count("T1") == 0


def test_readback_waits_briefly_for_music_to_reflect_adds(ledger, music, source, target, tmp_path):
    seed(ledger, music, source, target, tmp_path)
    music.stale_reads = 2  # the first two membership reads after writes lag behind
    waits = []
    result = apply_music(
        ledger,
        MusicApp(music),
        {"playlist1": "DJ"},
        tmp_path / "data",
        apply=True,
        sleep=waits.append,
    )
    assert result["playlists"][0]["done"]["added"] == 1
    assert waits  # it waited instead of failing


def test_one_import_shared_by_two_playlists_is_added_to_both(
    ledger, music, source, target, tmp_path
):
    seed(ledger, music, source, target, tmp_path)
    fresh = replace(source, id="s2", title="Fresh", isrc=None, duration_ms=200)
    snap = Snapshot("playlist2", "Second", "v1", (PlaylistItem(0, fresh),), True)

    class Two:
        def full(self, playlist_id):
            return snap

    run_playlist(Two(), Catalog(target), ledger, "playlist2", "US", "DJ")
    mappings = {"playlist1": "DJ", "playlist2": "DJ"}
    result = apply_music(
        ledger, MusicApp(music), mappings, tmp_path / "data", apply=True, sleep=lambda _: None
    )
    assert [p["status"] for p in result["playlists"]] == ["ok", "ok"]
    imported = [t for t in music.tracks if t.startswith("IMP")]
    assert len(imported) == 1  # imported once
    second = result["playlists"][1]["destination"]["id"]
    assert imported[0] in music.members[second]  # and placed in both playlists


def test_import_that_music_left_out_of_the_playlist_is_added_afterwards(
    ledger, music, source, target, tmp_path
):
    seed(ledger, music, source, target, tmp_path)
    music.add_skips_playlist_once = True  # imported to the library but not into the playlist
    result = apply_music(
        ledger,
        MusicApp(music),
        {"playlist1": "DJ"},
        tmp_path / "data",
        apply=True,
        sleep=lambda _: None,
    )
    entry = result["playlists"][0]
    assert entry["status"] == "ok"
    imported = [t for t in music.tracks if t.startswith("IMP")]
    assert len(imported) == 1 and imported[0] in music.members[entry["destination"]["id"]]
    # never doubled: the import appears exactly once
    assert music.members[entry["destination"]["id"]].count(imported[0]) == 1


def test_import_hands_the_cover_to_music_so_it_shows(
    ledger, music, source, target, tmp_path, make_aiff
):
    staged = seed(ledger, music, source, target, tmp_path)
    make_aiff(staged["aiff"], cover_encoding=0, tidal_id=92, cover=b"\xff\xd8\xff\xe0art")
    result = apply_music(
        ledger, MusicApp(music), {"playlist1": "DJ"}, tmp_path / "data", apply=True
    )
    assert result["playlists"][0]["done"]["covers"] == 1
    assert music.covers == {"IMP2": b"\xff\xd8\xff\xe0art"}
    assert (
        ledger.connection.execute("SELECT persistent_id FROM music_artwork").fetchall()[0][0]
        == "IMP2"
    )


def test_cover_handoff_is_retried_while_music_finishes_the_import(
    ledger, music, source, target, tmp_path, make_aiff
):
    staged = seed(ledger, music, source, target, tmp_path)
    make_aiff(staged["aiff"], cover_encoding=0, tidal_id=92, cover=b"\xff\xd8\xff\xe0art")
    real = music.__call__
    refusals = {"left": 2}

    def busy(script, args):
        if script is SET_ARTWORK_SCRIPT and refusals["left"]:
            refusals["left"] -= 1
            raise SyncError("Music.app scripting failed: Parameter error. (-50)")
        return real(script, args)

    waits = []
    result = apply_music(
        ledger,
        MusicApp(busy),
        {"playlist1": "DJ"},
        tmp_path / "data",
        apply=True,
        sleep=waits.append,
    )
    assert result["playlists"][0]["done"]["covers"] == 1
    assert "cover_failures" not in result and waits.count(5) >= 2


def test_osascript_errors_skip_framework_log_lines(monkeypatch):
    import subprocess

    from dj_sync.music_app import osascript_runner

    err = (
        "2026-10-20 20:42:21.726 osascript[52527:1386514] CFURLGetFSRef was passed a URL which "
        "has no scheme\nexecution error: Music got an error: Parameter error. (-50)\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", err)
    )
    with pytest.raises(SyncError, match=r"Parameter error"):
        osascript_runner("on run argv\nend run", [])
