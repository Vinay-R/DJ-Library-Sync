"""Read-only local library index and Spotify-to-local matching."""

import hashlib
import wave
from dataclasses import replace

import pytest
from mutagen.id3 import TIT2, TPE1, TSRC
from mutagen.wave import WAVE

from dj_sync.db import Ledger
from dj_sync.local_library import (
    LocalFile,
    index_files,
    match_local,
    path_metadata,
    read_file,
    scan,
)
from dj_sync.models import PlaylistItem, Snapshot
from dj_sync.pipeline import run_playlist


def make_wav(path, seconds=1.0, title=None, artist=None, isrc=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    if title or artist or isrc:
        audio = WAVE(str(path))
        audio.add_tags()
        if title:
            audio.tags.add(TIT2(encoding=3, text=title))
        if artist:
            audio.tags.add(TPE1(encoding=3, text=artist))
        if isrc:
            audio.tags.add(TSRC(encoding=3, text=isrc))
        audio.save()
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reads_tags_and_length_without_modifying_the_file(tmp_path):
    path = make_wav(tmp_path / "A" / "B" / "01 x.wav", 2.0, "Night Drive", "Artist", "usaaa2600001")
    before, mtime = digest(path), path.stat().st_mtime_ns
    found = read_file(path, tmp_path)
    assert (found.title, found.artists, found.isrc) == ("Night Drive", ("Artist",), "USAAA2600001")
    assert found.duration_ms == 2000
    assert found.metadata_source == "tags"
    assert digest(path) == before and path.stat().st_mtime_ns == mtime


def test_untagged_files_fall_back_to_music_app_folder_layout(tmp_path):
    root = tmp_path / "iTunes"
    path = make_wav(root / "iTunes Music" / "Hot Since 82" / "Damage _ Veins" / "1-02 Veins.wav")
    found = read_file(path, root)
    assert (found.title, found.artists, found.album) == (
        "Veins",
        ("Hot Since 82",),
        "Damage _ Veins",
    )
    assert found.metadata_source == "path"


@pytest.mark.parametrize(
    "parts, title, artists",
    [
        (("Nova Hale", "1:M", "03 Undertow.aiff"), "Undertow", ("Nova Hale",)),
        (("Compilations", "Hits", "07 Song.mp3"), "Song", ()),
        (("Unknown Artist", "Unknown Album", "Track.mp3"), "Track", ()),
        (("loose.mp3",), "loose", ()),
    ],
)
def test_path_metadata(parts, title, artists):
    meta = path_metadata(parts)
    assert (meta["title"], meta["artists"]) == (title, artists)


def test_multiple_artists_split_for_lead_artist_comparison(tmp_path):
    path = make_wav(tmp_path / "x.wav", 1.0, "Fade Out", "Kestrel, lumo")
    assert read_file(path, tmp_path).artists == ("Kestrel", "lumo")


def test_scan_skips_non_audio_and_reports_unreadable(tmp_path):
    make_wav(tmp_path / "a.wav", 1.0, "One", "A")
    (tmp_path / "cover.jpg").write_bytes(b"jpg")
    (tmp_path / "broken.mp3").write_bytes(b"not audio")
    files, problems = scan([tmp_path])
    # Unreadable audio is still indexed from its path so it can count as "possibly owned".
    assert [(f.title, f.metadata_source) for f in files] == [
        ("One", "tags"),
        ("broken", "path-unreadable"),
    ]
    assert len(problems) == 1 and problems[0][0].endswith("broken.mp3")


# ---------- index and match ----------


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.sqlite3")
    yield db
    db.close()


class Spotify:
    def __init__(self, items):
        self.snap = Snapshot("playlist1", "Test", "v1", tuple(items), True)

    def full(self, playlist_id):
        return self.snap


class Catalog:
    def __init__(self, target):
        self.target = target

    def candidates(self, source):
        return [replace(self.target, id="t-" + source.id)] if source.id != "s4" else []


def local(path, title, artists, ms, isrc=None):
    return LocalFile(path, 1, 1, title, tuple(artists), "Album", ms, isrc, "tags")


def test_match_local_classifies_found_other_version_and_missing(ledger, source, target):
    items = [
        PlaylistItem(0, source),  # "Night Drive - Extended Mix", 7:00
        PlaylistItem(1, replace(source, id="s2", title="Undertow", isrc=None, duration_ms=200000)),
        PlaylistItem(2, replace(source, id="s3", title="Gone", isrc=None)),
        PlaylistItem(3, replace(source, id="s4", title="Rare", isrc=None, duration_ms=180000)),
    ]
    run_playlist(Spotify(items), Catalog(target), ledger, "playlist1", "US", "DJ")
    index_files(
        ledger,
        [
            local(
                "/m/Artist/A/01 Night Drive (Extended Mix).aiff",
                "Night Drive (Extended Mix)",
                ["Artist"],
                420500,
            ),
            local(
                "/m/Artist/A/02 Undertow (Radio Edit).mp3",
                "Undertow (Radio Edit)",
                ["Artist"],
                150000,
            ),
            local("/m/Artist/A/03 Rare.mp3", "Rare", ["Artist"], 180900),
        ],
    )
    summary = match_local(ledger, {"playlist1": "DJ"})
    by_id = {t["spotify"]["id"]: t for t in summary["tracks"]}
    assert by_id["s1"]["local_status"] == "found"
    assert by_id["s1"]["local"]["path"].endswith("Night Drive (Extended Mix).aiff")
    assert by_id["s2"]["local_status"] == "other_version"
    assert by_id["s3"]["local_status"] == "missing"
    assert by_id["s4"]["local_status"] == "found"
    assert by_id["s1"]["tidal_status"] == "matched" and by_id["s4"]["tidal_status"] != "matched"
    totals = summary["totals"]
    assert totals["found"] == 2 and totals["found_and_tidal_matched"] == 1
    row = ledger.connection.execute(
        "SELECT status FROM sync_status WHERE stage='local_file_detected'"
    ).fetchall()
    assert len(row) == 2


def test_isrc_match_wins_even_with_messy_title(ledger, source, target):
    run_playlist(Spotify([PlaylistItem(0, source)]), Catalog(target), ledger, "playlist1", "US")
    index_files(
        ledger,
        [local("/m/x.mp3", "night drive ext mix (promo)", ["Artist"], 420000, "USAAA2600001")],
    )
    summary = match_local(ledger, {"playlist1": "DJ"})
    assert summary["tracks"][0]["local_status"] == "found"


def test_reindex_replaces_rows_and_drops_missing_files(ledger):
    index_files(ledger, [local("/m/a.mp3", "A", ["X"], 1000), local("/m/b.mp3", "B", ["X"], 1000)])
    index_files(ledger, [local("/m/a.mp3", "A2", ["X"], 1000)])
    rows = ledger.connection.execute("SELECT path,title FROM local_files").fetchall()
    assert [tuple(r) for r in rows] == [("/m/a.mp3", "A2")]


def test_semicolon_artists_and_download_suffixes_are_cleaned(tmp_path):
    path = make_wav(tmp_path / "x.wav", 1.0, "restless_High_Quality", "Gray Fields;Ivo Marten")
    found = read_file(path, tmp_path)
    assert found.title == "restless"
    assert found.artists == ("Gray Fields", "Ivo Marten")


def test_producer_credit_is_not_part_of_the_title():
    from dj_sync.models import Track
    from dj_sync.track_matcher import identity

    def title(text):
        return identity(Track("local", "x", text, ("Ka$e",), 1))[0]

    assert title("Harbor Lights (Prod. Elias Grove)") == "harbor lights"
    assert title("Harbor Lights [prod by Elias Grove]") == "harbor lights"
    assert title("Production Line") == "production line"


def test_review_items_show_a_matching_local_file(ledger, source, target):
    from dj_sync.review import review_items

    class NoTidal:
        def candidates(self, s):
            return []

    run_playlist(Spotify([PlaylistItem(0, source)]), NoTidal(), ledger, "playlist1", "US", "DJ")
    index_files(
        ledger,
        [
            local(
                "/m/Artist/A/01 Night Drive (Extended Mix).aiff",
                "Night Drive (Extended Mix)",
                ["Artist"],
                420000,
            )
        ],
    )
    match_local(ledger, {"playlist1": "DJ"})
    item = review_items(ledger, {"playlist1": "DJ"})["items"][0]
    assert item["local"]["path"].endswith("Night Drive (Extended Mix).aiff")
    assert item["local"]["duration_ms"] == 420000


# ---------- Step 0: unreadable files, possibly_found, v6 migration ----------


def test_unreadable_audio_is_indexed_from_its_path(tmp_path):
    root = tmp_path / "iTunes"
    bad = root / "iTunes Music" / "Hot Since 82" / "Damage" / "02 Veins.aiff"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"FORM\x00\x00\x00\x10AIFFjunkjunkjunk")
    files, problems = scan([root])
    assert len(files) == 1
    assert files[0].metadata_source == "path-unreadable"
    assert (files[0].title, files[0].artists, files[0].duration_ms) == (
        "Veins",
        ("Hot Since 82",),
        None,
    )
    assert problems and problems[0][0] == str(bad)


def test_unknown_length_match_is_possibly_found_never_found(ledger, source, target):
    items = [PlaylistItem(0, replace(source, isrc=None, title="Veins"))]
    run_playlist(Spotify(items), Catalog(target), ledger, "playlist1", "US", "DJ")
    unreadable = LocalFile(
        "/m/Hot/D/02 Veins.aiff", 1, 1, "Veins", ("Artist",), "D", None, None, "path-unreadable"
    )
    index_files(ledger, [unreadable])
    summary = match_local(ledger, {"playlist1": "DJ"})
    track = summary["tracks"][0]
    assert track["local_status"] == "possibly_found"
    assert summary["totals"]["possibly_found"] == 1 and summary["totals"]["found"] == 0
    rows = ledger.connection.execute(
        "SELECT status FROM sync_status WHERE stage='local_file_detected'"
    ).fetchall()
    assert [r[0] for r in rows] == ["possibly_found"]
    assert ledger.connection.execute("SELECT count(*) FROM local_assets").fetchone()[0] == 0


def test_found_beats_possibly_found(ledger, source, target):
    run_playlist(Spotify([PlaylistItem(0, source)]), Catalog(target), ledger, "playlist1", "US")
    good = local(
        "/m/A/B/01 Night Drive (Extended Mix).aiff",
        "Night Drive (Extended Mix)",
        ["Artist"],
        420000,
    )
    vague = LocalFile(
        "/m/A/C/01 Night Drive (Extended Mix).aiff",
        1,
        1,
        "Night Drive (Extended Mix)",
        ("Artist",),
        "C",
        None,
        None,
        "path-unreadable",
    )
    index_files(ledger, [vague, good])
    assert match_local(ledger, {"playlist1": "DJ"})["tracks"][0]["local_status"] == "found"


def test_two_spotify_tracks_sharing_one_file_both_get_asset_rows(ledger, source, target):
    twin = replace(source, id="s2")  # same recording listed twice on Spotify
    items = [PlaylistItem(0, source), PlaylistItem(1, twin)]
    run_playlist(Spotify(items), Catalog(target), ledger, "playlist1", "US", "DJ")
    index_files(
        ledger,
        [
            local(
                "/m/A/B/01 Night Drive (Extended Mix).aiff",
                "Night Drive (Extended Mix)",
                ["Artist"],
                420000,
            )
        ],
    )
    match_local(ledger, {"playlist1": "DJ"})
    assert ledger.connection.execute("SELECT count(*) FROM local_assets").fetchone()[0] == 2
    assert (
        ledger.connection.execute(
            "SELECT count(*) FROM sync_status WHERE stage='local_file_detected'"
        ).fetchone()[0]
        == 2
    )


def test_rescan_keeps_rows_it_did_not_create(ledger, source, target):
    run_playlist(Spotify([PlaylistItem(0, source)]), Catalog(target), ledger, "playlist1", "US")
    track_id = ledger.connection.execute("SELECT id FROM tracks").fetchone()[0]
    ledger.connection.execute(
        "INSERT INTO local_assets(track_id,path,source) VALUES(?,?,'download')",
        (track_id, "/staging/x.aiff"),
    )
    ledger.connection.commit()
    index_files(ledger, [])
    match_local(ledger, {"playlist1": "DJ"})
    rows = ledger.connection.execute("SELECT path FROM local_assets").fetchall()
    assert [r[0] for r in rows] == ["/staging/x.aiff"]


def test_review_local_file_is_deterministic_with_several_copies(ledger, source):
    from dj_sync.review import review_items

    class NoTidal:
        def candidates(self, s):
            return []

    run_playlist(Spotify([PlaylistItem(0, source)]), NoTidal(), ledger, "playlist1", "US", "DJ")
    copies = [
        local(
            f"/m/A/{n}/01 Night Drive (Extended Mix).aiff",
            "Night Drive (Extended Mix)",
            ["Artist"],
            420000,
        )
        for n in ("Zeta", "Alpha")
    ]
    index_files(ledger, copies)
    match_local(ledger, {"playlist1": "DJ"})
    item = review_items(ledger, {"playlist1": "DJ"})["items"][0]
    assert item["local"]["path"] == "/m/A/Alpha/01 Night Drive (Extended Mix).aiff"


def test_v5_ledger_migrates_local_assets_and_keeps_rows(tmp_path):
    import sqlite3

    path = tmp_path / "ledger.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, artists_json TEXT
         NOT NULL, duration_ms INTEGER, identity_json TEXT NOT NULL);
        CREATE TABLE local_assets (id INTEGER PRIMARY KEY, track_id INTEGER REFERENCES
         tracks(id), path TEXT UNIQUE, fingerprint TEXT, music_persistent_id TEXT,
         import_verified INTEGER NOT NULL DEFAULT 0);
        INSERT INTO tracks VALUES(1,'A','[]',1,'{}'),(2,'B','[]',1,'{}');
        INSERT INTO local_assets(track_id,path) VALUES(1,'/m/a.aiff');
        PRAGMA user_version = 5;
        """
    )
    old.commit()
    old.close()
    db = Ledger(path)
    assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 6
    rows = db.connection.execute("SELECT track_id,path,source FROM local_assets").fetchall()
    assert [tuple(r) for r in rows] == [(1, "/m/a.aiff", "scan")]
    db.connection.execute(
        "INSERT INTO local_assets(track_id,path,source) VALUES(2,'/m/a.aiff','scan')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO local_assets(track_id,path,source) VALUES(2,'/m/a.aiff','scan')"
        )
    names = {
        r[0] for r in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "downloads",
        "music_folders",
        "music_playlists",
        "music_tracks",
        "music_memberships",
    } <= names
    db.close()
    assert Ledger(path).connection.execute("PRAGMA user_version").fetchone()[0] == 6
