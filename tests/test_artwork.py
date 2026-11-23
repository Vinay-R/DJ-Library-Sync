"""Cover repair for the tool's own imports: Latin-1 cover frame, then Music.app's store."""

import pytest

from dj_sync.artwork import _sound_digest, cover, fix_file_cover, repair_artwork
from dj_sync.db import Ledger
from dj_sync.errors import SyncError
from dj_sync.music_app import SET_ARTWORK_SCRIPT, MusicApp


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "data" / "ledger.sqlite3")
    yield db
    db.close()


class CoverMusic:
    def __init__(self, fail=False):
        self.covers: dict[str, bytes] = {}
        self.fail = fail

    def __call__(self, script, args):
        assert script is SET_ARTWORK_SCRIPT
        track_id, image = args
        if self.fail:
            return "0"
        with open(image, "rb") as fh:
            self.covers[track_id] = fh.read()
        return "1"


def test_fix_file_cover_rewrites_only_the_cover_frame(tmp_path, make_aiff):
    path = make_aiff(tmp_path / "a.aiff", audio=b"\x12\x34" * 64, cover_encoding=1, tidal_id=7)
    audio_before = _sound_digest(path)
    data_before = cover(path).data
    assert fix_file_cover(path) is True
    pic = cover(path)
    assert int(pic.encoding) == 0 and pic.desc == "" and pic.data == data_before
    assert _sound_digest(path) == audio_before
    assert fix_file_cover(path) is False  # already Latin-1: untouched


def seed(ledger, tmp_path, make_aiff, *, file_tidal_id="92", encoding=1):
    """One imported tool download whose library copy carries the given TIDAL URL."""
    (tmp_path / "media").mkdir()
    lib = make_aiff(
        tmp_path / "media" / "01 Song.aiff", cover_encoding=encoding, tidal_id=file_tidal_id
    )
    c = ledger.connection
    track_id = c.execute(
        "INSERT INTO tracks(title, artists_json, identity_json) VALUES('Song','[]','{}')"
    ).lastrowid
    c.execute(
        "INSERT INTO downloads VALUES(?,'92','r','imported','tool',0,'f.flac','a.aiff',1)",
        (track_id,),
    )
    c.execute(
        "INSERT INTO local_assets(track_id,path,source,music_persistent_id,import_verified) "
        "VALUES(?,?,'music','M1',1)",
        (track_id, str(lib)),
    )
    c.execute("INSERT INTO music_tracks VALUES('M1','Song','Artist',1000,?,1)", (str(lib),))
    c.commit()
    return lib, track_id


def test_repair_dry_run_changes_nothing_then_apply_fixes_and_is_idempotent(
    ledger, tmp_path, make_aiff
):
    lib, _ = seed(ledger, tmp_path, make_aiff)
    music = CoverMusic()
    dry = repair_artwork(ledger, MusicApp(music), tmp_path / "data")
    assert dry["candidates"] == 1 and dry["files_fixed"] == 0 and music.covers == {}
    assert int(cover(lib).encoding) == 1
    done = repair_artwork(ledger, MusicApp(music), tmp_path / "data", apply=True)
    assert done["files_fixed"] == 1 and done["handed_to_music"] == 1
    assert music.covers == {"M1": cover(lib).data} and int(cover(lib).encoding) == 0
    handed = ledger.connection.execute("SELECT persistent_id FROM music_artwork").fetchall()
    assert [r[0] for r in handed] == ["M1"]
    assert not list((tmp_path / "data" / "artwork-tmp").iterdir())  # temp images removed
    again = repair_artwork(ledger, MusicApp(music), tmp_path / "data", apply=True)
    assert again["candidates"] == 0


def test_repair_never_touches_a_file_that_is_not_the_tools_download(ledger, tmp_path, make_aiff):
    lib, _ = seed(ledger, tmp_path, make_aiff, file_tidal_id="555")
    before = lib.read_bytes()
    music = CoverMusic()
    out = repair_artwork(ledger, MusicApp(music), tmp_path / "data", apply=True)
    assert out["skipped"] == [{"name": "Song", "reason": "not the tool's download"}]
    assert lib.read_bytes() == before and music.covers == {}


def test_repair_stops_when_music_does_not_take_the_cover(ledger, tmp_path, make_aiff):
    seed(ledger, tmp_path, make_aiff)
    with pytest.raises(SyncError, match="did not take the cover"):
        repair_artwork(ledger, MusicApp(CoverMusic(fail=True)), tmp_path / "data", apply=True)
    assert ledger.connection.execute("SELECT count(*) FROM music_artwork").fetchone()[0] == 0


def test_one_library_file_linked_to_two_downloads_is_recognised(ledger, tmp_path, make_aiff):
    # One file can back two Spotify tracks whose downloads had different TIDAL IDs.
    lib, _ = seed(ledger, tmp_path, make_aiff)
    c = ledger.connection
    other = c.execute(
        "INSERT INTO tracks(title, artists_json, identity_json) VALUES('Song','[]','{}')"
    ).lastrowid
    c.execute(
        "INSERT INTO downloads VALUES(?,'93','r','imported','tool',0,'g.flac','b.aiff',1)",
        (other,),
    )
    c.execute(
        "INSERT INTO local_assets(track_id,path,source,music_persistent_id,import_verified) "
        "VALUES(?,?,'music','M1',1)",
        (other, str(lib)),
    )
    c.commit()
    music = CoverMusic()
    out = repair_artwork(ledger, MusicApp(music), tmp_path / "data", apply=True)
    assert out["candidates"] == 1 and out["skipped"] == [] and set(music.covers) == {"M1"}
