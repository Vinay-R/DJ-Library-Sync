"""Tidaler-driven acquisition: plan, run, verify by tags, convert to AIFF."""

import json
import os
import shutil
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from dj_sync.acquire import (
    TidalerSettings,
    _write_aiff_tags,
    convert_downloads,
    download_plan,
    gui_running,
    run_tidaler,
    tidal_id_from_url,
    verify_downloads,
)
from dj_sync.db import Ledger
from dj_sync.errors import SyncError
from dj_sync.local_library import LocalFile, index_files, match_local, read_file
from dj_sync.models import PlaylistItem, Snapshot
from dj_sync.pipeline import run_playlist
from dj_sync.review import import_decisions

FIXTURES = Path(__file__).parent / "fixtures"
FFMPEG = shutil.which("ffmpeg") or (
    "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else None
)
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")

FAKE_TIDALER = r"""
import json, os, shutil, sys, time, subprocess
from pathlib import Path
from mutagen.flac import FLAC
spec = json.loads(Path(os.environ["FAKE_TIDALER"]).read_text())
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("0.1.8"); sys.exit(0)
assert args[:2] == ["dl", "-l"], args
urls = [u for u in Path(args[2]).read_text().splitlines() if u.strip()]
if spec.get("login_prompt"):
    print("Paste 'Ooops' page URL here and press <ENTER>:")
    sys.exit(1)
if spec.get("hang"):
    child = subprocess.Popen(["sleep", "300"])
    Path(spec["child_pid_file"]).write_text(str(child.pid))
    child.wait()
tracks = Path(spec["tracks_dir"]); tracks.mkdir(parents=True, exist_ok=True)
for url in urls:
    tid = url.rstrip("/").rsplit("/", 1)[-1]
    item = spec["produce"].get(tid)
    if item is None:
        print(f"Media not found (ID: {tid}). Maybe it is not available anymore.")
        continue
    dest = tracks / f'{item["artist"]} - {item["title"]} [{item.get("file_id", tid)}].flac'
    shutil.copy(item["fixture"], dest)
    f = FLAC(dest)
    f["TITLE"] = item["title"]; f["ARTIST"] = item["artist"]
    f["ALBUM"] = item.get("album", "Album")
    if item.get("isrc"): f["ISRC"] = item["isrc"]
    f["URL"] = f'https://tidal.com/browse/track/{item.get("file_id", tid)}'
    f.save()
    if item.get("truncate"):
        data = dest.read_bytes(); dest.write_bytes(data[: len(data) // 2])
for extra in spec.get("extra", []):
    dest = tracks / extra["name"]; shutil.copy(extra["fixture"], dest)
    f = FLAC(dest); f["TITLE"] = "Extra"; f["ARTIST"] = "Nobody"
    f["URL"] = extra.get("url", ""); f.save()
print("Finished list")
sys.exit(0)
"""


@pytest.fixture
def tidaler(tmp_path):
    """A fake Tidaler install: settings.json, token.json, and a dl command."""
    config = tmp_path / "config" / "tidaler"
    config.mkdir(parents=True)
    base = tmp_path / "Tidal-DL" / "FLAC "
    (config / "settings.json").write_text(
        json.dumps(
            {
                "download_base_path": str(base),
                "format_track": "Tracks/{artist_name} - {track_title}{track_explicit} [{track_id}]",
                "skip_existing": True,
            }
        )
    )
    (config / "token.json").write_text(json.dumps({"expiry_time": time.time() + 3600}))
    script = tmp_path / "fake-tidaler.py"
    script.write_text(FAKE_TIDALER)
    launcher = tmp_path / "tidaler"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    spec_path = tmp_path / "spec.json"
    os.environ["FAKE_TIDALER"] = str(spec_path)
    settings = TidalerSettings.load(config)

    class Fake:
        bin = launcher
        settings_ = settings
        tracks_dir = base / "Tracks"

        def behave(self, **spec):
            spec.setdefault("tracks_dir", str(self.tracks_dir))
            spec.setdefault("produce", {})
            spec_path.write_text(json.dumps(spec))

    fake = Fake()
    fake.behave()
    yield fake
    os.environ.pop("FAKE_TIDALER", None)


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.sqlite3")
    yield db
    db.close()


class Spotify:
    def __init__(self, items):
        self.snap = Snapshot("playlist1", "Floor Fillers", "v1", tuple(items), True)

    def full(self, playlist_id):
        return self.snap


class Catalog:
    """Each Spotify track matches TIDAL id "t<n>" with the same title and length."""

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


def seeded(ledger, source, target, n=3):
    items = [
        PlaylistItem(
            i, replace(source, id=f"s{i + 1}", title=f"Song {i + 1}", isrc=f"USAAA26000{i:02d}")
        )
        for i in range(n)
    ]
    run_playlist(Spotify(items), Catalog(target), ledger, "playlist1", "US", "DJ")
    return items


def produce(items, **overrides):
    out = {}
    for it in items:
        tid = "9" + it.track.id[1:]
        out[tid] = {
            "fixture": str(FIXTURES / "silence16.flac"),
            "title": it.track.title,
            "artist": it.track.artists[0],
            "isrc": it.track.isrc,
            **overrides.get(tid, {}),
        }
    return out


def short_source(source):
    # Fixture audio is 0.2 s, so Spotify/TIDAL lengths must agree with it.
    return replace(source, duration_ms=200)


# ---------- settings and preflight ----------


def test_settings_keep_the_download_path_verbatim(tidaler):
    s = tidaler.settings_
    assert s.download_base_path.name == "FLAC "
    assert s.tracks_dir == s.download_base_path / "Tracks"
    assert "{track_id}" in s.format_track
    assert s.token_state() == "present"


def test_gui_detection_uses_the_app_bundle_path():
    ps = "/Applications/tidaler.app/Contents/MacOS/tidaler\n/bin/zsh -c tidaler dl -l x\n"
    assert gui_running(ps)
    assert not gui_running("/usr/bin/python3 tidaler dl -l x\n")


def test_tidal_id_from_url():
    assert tidal_id_from_url("https://tidal.com/browse/track/410334946") == "410334946"
    assert tidal_id_from_url("https://listen.tidal.com/track/410334946?u") == "410334946"
    assert tidal_id_from_url("") is None


# ---------- plan ----------


def test_plan_excludes_owned_decided_and_done_tracks(ledger, source, target, tidaler):
    items = seeded(ledger, short_source(source), target, n=5)
    # s1 is on disk; s2 marked local by the user; s3 skipped; s4 already downloaded.
    index_files(
        ledger,
        [
            LocalFile(
                "/m/A/B/01 Song 1.aiff",
                1,
                1,
                "Song 1",
                ("Artist",),
                "B",
                200,
                "USAAA2600000",
                "tags",
            )
        ],
    )
    match_local(ledger, {"playlist1": "DJ"})
    import_decisions(
        ledger,
        [{"spotify_id": "s2", "decision": "local"}, {"spotify_id": "s3", "decision": "skip"}],
        "US",
    )
    run_playlist(Spotify(items), Catalog(target), ledger, "playlist1", "US", "DJ")
    ledger.connection.execute(
        "INSERT INTO downloads VALUES((SELECT track_id FROM service_tracks WHERE service_id='s4'),"
        "'94','r','downloaded','tool',1,'/x.flac',NULL,1)"
    )
    ledger.connection.commit()
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    assert [e["tidal_id"] for e in plan["requests"]] == ["95"]
    assert plan["excluded"] == {"on_disk": 1, "decided": 2, "downloaded": 1}
    assert Path(plan["list_file"]).read_text().strip() == "https://tidal.com/browse/track/95"


def test_plan_groups_spotify_tracks_sharing_one_tidal_id_and_applies_limit(
    ledger, source, target, tidaler
):
    src = short_source(source)
    items = [
        PlaylistItem(0, replace(src, id="s1")),
        PlaylistItem(1, replace(src, id="s2")),
        PlaylistItem(2, replace(src, id="s3", title="Other")),
    ]

    class SharedCatalog(Catalog):
        def candidates(self, source):
            [hit] = super().candidates(source)
            return [replace(hit, id="91" if source.title.startswith("Night") else "93")]

    run_playlist(Spotify(items), SharedCatalog(target), ledger, "playlist1", "US", "DJ")
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=1)
    assert len(plan["requests"]) == 1
    assert plan["requests"][0]["tidal_id"] == "91"
    assert sorted(plan["requests"][0]["spotify_ids"]) == ["s1", "s2"]
    assert plan["deferred_by_limit"] == 1


def test_plan_records_files_already_in_tracks_folder_without_requesting(
    ledger, source, target, tidaler
):
    seeded(ledger, short_source(source), target, n=2)
    tidaler.tracks_dir.mkdir(parents=True)
    existing = tidaler.tracks_dir / "Artist - Song 1 [91].flac"
    shutil.copy(FIXTURES / "silence16.flac", existing)
    f = FLAC(existing)
    f["URL"] = "https://tidal.com/browse/track/91"
    f.save()
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    assert [e["tidal_id"] for e in plan["requests"]] == ["92"]
    row = ledger.connection.execute(
        "SELECT status, origin, flac_path FROM downloads WHERE tidal_id='91'"
    ).fetchone()
    assert tuple(row) == ("downloaded", "preexisting", str(existing))


# ---------- run + verify ----------


def test_successful_run_verifies_by_url_tag_and_marks_every_track(
    ledger, source, target, tidaler, tmp_path
):
    src = short_source(source)
    items = [PlaylistItem(0, replace(src, id="s1")), PlaylistItem(1, replace(src, id="s2"))]
    run_playlist(Spotify(items), Catalog(target), ledger, "playlist1", "US", "DJ")
    tidaler.behave(produce=produce(items))
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    run = run_tidaler(plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="")
    assert run["returncode"] == 0 and not run["timed_out"] and not run["login_required"]
    result = verify_downloads(ledger, plan, tidaler.settings_, run, quiet_seconds=0)
    assert result["downloaded"] == 2 and result["failed"] == 0
    rows = ledger.connection.execute(
        "SELECT track_id, status, origin FROM downloads ORDER BY track_id"
    ).fetchall()
    assert [tuple(r)[1:] for r in rows] == [("downloaded", "tool"), ("downloaded", "tool")]
    assert (
        ledger.connection.execute(
            "SELECT count(*) FROM sync_status WHERE stage='downloaded' AND status='downloaded'"
        ).fetchone()[0]
        == 2
    )
    assert (tmp_path / "logs").glob("tidaler-*.log")


def test_exit_zero_with_missing_file_is_a_failure_with_attempt_count(
    ledger, source, target, tidaler, tmp_path
):
    items = seeded(ledger, short_source(source), target, n=2)
    tidaler.behave(produce={k: v for k, v in produce(items).items() if k == "91"})
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    run = run_tidaler(plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="")
    result = verify_downloads(ledger, plan, tidaler.settings_, run, quiet_seconds=0)
    assert (result["downloaded"], result["failed"]) == (1, 1)
    row = ledger.connection.execute(
        "SELECT status, attempts FROM downloads WHERE tidal_id='92'"
    ).fetchone()
    assert tuple(row) == ("unavailable", 1)  # Tidaler reported "Media not found" for it
    assert "92" in result["unavailable_ids"]


def test_failed_downloads_are_retried_at_most_twice(ledger, source, target, tidaler, tmp_path):
    seeded(ledger, short_source(source), target, n=1)
    track_id = ledger.connection.execute("SELECT id FROM tracks").fetchone()[0]
    ledger.connection.execute(
        "INSERT INTO downloads VALUES(?,'91','r','failed','tool',2,NULL,NULL,NULL)", (track_id,)
    )
    ledger.connection.commit()
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    assert plan["requests"] == [] and plan["excluded"]["attempts_exhausted"] == 1


def test_wrong_length_file_is_not_accepted(ledger, source, target, tidaler, tmp_path):
    items = seeded(ledger, replace(source, duration_ms=300000), target, n=1)
    tidaler.behave(produce=produce(items))  # fixture is 0.2 s, expected 300 s
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    run = run_tidaler(plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="")
    result = verify_downloads(ledger, plan, tidaler.settings_, run, quiet_seconds=0)
    assert result["downloaded"] == 0 and result["corrupt"] == 1
    assert ledger.connection.execute("SELECT status FROM downloads").fetchone()[0] == "corrupt"


def test_sibling_release_and_unexpected_files_go_to_review_not_downloaded(
    ledger, source, target, tidaler, tmp_path
):
    items = seeded(ledger, short_source(source), target, n=1)
    spec = produce(items, **{"91": {"file_id": "777"}})  # same ISRC, different TIDAL id
    tidaler.behave(
        produce=spec,
        extra=[
            {
                "name": "Nobody - Extra [555].flac",
                "fixture": str(FIXTURES / "silence16.flac"),
                "url": "https://tidal.com/browse/track/555",
            }
        ],
    )
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    run = run_tidaler(plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="")
    result = verify_downloads(ledger, plan, tidaler.settings_, run, quiet_seconds=0)
    assert result["downloaded"] == 0
    assert result["sibling_release"] == 1 and result["unexpected"] == 1
    assert ledger.connection.execute("SELECT status FROM downloads").fetchone()[0] == "review"


def test_login_prompt_is_detected_and_run_stops(ledger, source, target, tidaler, tmp_path):
    seeded(ledger, short_source(source), target, n=1)
    tidaler.behave(login_prompt=True)
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    run = run_tidaler(plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="")
    assert run["login_required"]
    with pytest.raises(SyncError, match="tidaler login"):
        verify_downloads(ledger, plan, tidaler.settings_, run, quiet_seconds=0)


def test_timeout_kills_the_whole_process_group(ledger, source, target, tidaler, tmp_path):
    seeded(ledger, short_source(source), target, n=1)
    pid_file = tmp_path / "child.pid"
    tidaler.behave(hang=True, child_pid_file=str(pid_file))
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    run = run_tidaler(
        plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="", timeout=3
    )
    assert run["timed_out"]
    child = int(pid_file.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(child, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    else:
        pytest.fail("sleep child survived the timeout")


def test_gui_running_or_missing_token_refuses_to_start(ledger, source, target, tidaler, tmp_path):
    seeded(ledger, short_source(source), target, n=1)
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    with pytest.raises(SyncError, match="GUI"):
        run_tidaler(
            plan,
            tidaler.settings_,
            tidaler.bin,
            tmp_path / "logs",
            ps_output="/Applications/tidaler.app/Contents/MacOS/tidaler\n",
        )
    tidaler.settings_.token_path.unlink()
    with pytest.raises(SyncError, match="tidaler login"):
        run_tidaler(plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="")


def test_preexisting_files_are_never_touched_by_verify(ledger, source, target, tidaler, tmp_path):
    seeded(ledger, short_source(source), target, n=1)
    tidaler.tracks_dir.mkdir(parents=True)
    existing = tidaler.tracks_dir / "Artist - Song 1 [91].flac"
    shutil.copy(FIXTURES / "silence16.flac", existing)
    f = FLAC(existing)
    f["URL"] = "https://tidal.com/browse/track/91"
    f.save()
    before = existing.stat().st_mtime_ns
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    assert plan["requests"] == []
    assert existing.stat().st_mtime_ns == before
    assert ledger.connection.execute("SELECT origin FROM downloads").fetchone()[0] == "preexisting"


# ---------- convert ----------


@needs_ffmpeg
def test_convert_writes_tagged_aiff_capped_at_48k_and_verifies(
    ledger, source, target, tidaler, tmp_path
):
    items = seeded(ledger, short_source(source), target, n=2)
    spec = produce(items, **{"92": {"fixture": str(FIXTURES / "tone24_96k.flac")}})
    tidaler.behave(produce=spec)
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    run = run_tidaler(plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="")
    verify_downloads(ledger, plan, tidaler.settings_, run, quiet_seconds=0)
    aiff_dir = tmp_path / "AIFF"
    result = convert_downloads(ledger, aiff_dir, FFMPEG, max_rate=48000)
    assert result["converted"] == 2 and result["failed"] == 0
    rows = ledger.connection.execute(
        "SELECT tidal_id, status, aiff_path FROM downloads ORDER BY tidal_id"
    ).fetchall()
    assert [r[1] for r in rows] == ["converted", "converted"]
    for _tidal_id, _, aiff_path in rows:
        back = read_file(Path(aiff_path), aiff_dir)
        assert back.isrc and back.title.startswith("Song")
        assert back.duration_ms == pytest.approx(200, abs=60)
    from mutagen.aiff import AIFF

    hi = AIFF(rows[1][2])
    assert hi.info.sample_rate == 48000 and hi.info.bits_per_sample == 24
    lo = AIFF(rows[0][2])
    assert lo.info.sample_rate == 44100 and lo.info.bits_per_sample == 16
    assert any(k.startswith("TXXX") for k in lo.tags.keys())
    assert (
        ledger.connection.execute(
            "SELECT count(*) FROM sync_status WHERE stage='converted'"
        ).fetchone()[0]
        == 2
    )
    # Repeat is a no-op and existing targets are never overwritten.
    again = convert_downloads(ledger, aiff_dir, FFMPEG, max_rate=48000)
    assert again["converted"] == 0 and not list(aiff_dir.glob("*.part"))


@needs_ffmpeg
def test_convert_refuses_unsupported_bit_depth(
    ledger, source, target, tidaler, tmp_path, monkeypatch
):
    items = seeded(ledger, short_source(source), target, n=1)
    tidaler.behave(produce=produce(items))
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    run = run_tidaler(plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="")
    verify_downloads(ledger, plan, tidaler.settings_, run, quiet_seconds=0)
    import dj_sync.acquire as acquire

    monkeypatch.setattr(acquire, "_bits_per_sample", lambda path: 12)
    result = convert_downloads(ledger, tmp_path / "AIFF", FFMPEG, max_rate=48000)
    assert result["failed"] == 1
    assert ledger.connection.execute("SELECT status FROM downloads").fetchone()[0] == "downloaded"


def test_plan_only_restricts_requests_to_chosen_tidal_ids(ledger, source, target, tidaler):
    seeded(ledger, short_source(source), target, n=3)
    plan = download_plan(
        ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50, only={"92", "nope"}
    )
    assert [e["tidal_id"] for e in plan["requests"]] == ["92"]
    assert plan["excluded"] == {"not_selected": 2}
    assert plan["unknown_ids"] == ["nope"]


def test_success_line_mentioning_login_is_not_a_login_prompt():
    from dj_sync.acquire import LOGIN_MARKERS

    success = "Let us check if you are already logged in... Yep, looks good! You are logged in."
    assert not any(m in success.lower() for m in LOGIN_MARKERS)
    prompt = "Paste 'Ooops' page URL here and press <ENTER>:"
    assert any(m in prompt.lower() for m in LOGIN_MARKERS)


@needs_ffmpeg
def test_convert_caps_32_bit_sources_at_24_bit(
    ledger, source, target, tidaler, tmp_path, monkeypatch
):
    items = seeded(ledger, short_source(source), target, n=1)
    spec = produce(items, **{"91": {"fixture": str(FIXTURES / "tone24_96k.flac")}})
    tidaler.behave(produce=spec)
    plan = download_plan(ledger, {"playlist1": "DJ"}, "US", tidaler.settings_, limit=50)
    run = run_tidaler(plan, tidaler.settings_, tidaler.bin, tmp_path / "logs", ps_output="")
    verify_downloads(ledger, plan, tidaler.settings_, run, quiet_seconds=0)
    import dj_sync.acquire as acquire

    monkeypatch.setattr(acquire, "_bits_per_sample", lambda path: 32)
    result = convert_downloads(ledger, tmp_path / "AIFF", FFMPEG, max_rate=48000)
    assert result["converted"] == 1 and result["failed"] == 0
    from mutagen.aiff import AIFF

    aiff = AIFF(ledger.connection.execute("SELECT aiff_path FROM downloads").fetchone()[0])
    assert aiff.info.bits_per_sample == 24 and aiff.info.sample_rate == 48000


def test_aiff_cover_uses_latin1_empty_description(tmp_path, make_aiff):
    # Music.app shows no cover for a v2.3 APIC with a UTF-16 description (2026-10-20).
    from mutagen.aiff import AIFF
    from mutagen.flac import Picture

    flac_path = tmp_path / "in.flac"
    shutil.copy(FIXTURES / "silence16.flac", flac_path)
    f = FLAC(flac_path)
    pic = Picture()
    pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover ✓", b"\xff\xd8\xff\xe0jpeg"
    f.add_picture(pic)
    f["TITLE"] = "Song"
    f.save()
    aiff_path = tmp_path / "out.aiff"
    make_aiff(aiff_path)
    _write_aiff_tags(FLAC(flac_path), aiff_path, "https://tidal.com/browse/track/1")
    tags = AIFF(aiff_path).tags
    assert tags.version[:2] == (2, 3)
    [apic] = tags.getall("APIC")
    assert int(apic.encoding) == 0 and apic.desc == "" and apic.type == 3
    assert apic.data == pic.data
    raw = aiff_path.read_bytes()
    frame = raw[raw.find(b"APIC") + 10 :]
    assert frame[:1] == b"\x00" and b"\xff\xfe" not in frame[:16]
