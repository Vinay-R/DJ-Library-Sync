"""Read-only index of local audio files and Spotify-to-local matching.

Files are opened only for reading tags and stream length; nothing is ever written,
renamed, moved or deleted. Music.app keeps much of its metadata in its own database,
so untagged files fall back to Music.app's "Artist/Album/NN Title.ext" folder layout.
A local match is evidence that a file exists on disk, not proof that Music.app has
imported it; Music.app membership is verified separately in a later phase.
"""

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import mutagen

from .db import Ledger
from .models import Track, normalize_isrc
from .track_matcher import identity, normalize, score

AUDIO = {".mp3", ".m4a", ".m4p", ".mp4", ".aif", ".aiff", ".wav", ".flac", ".aac", ".ogg"}
UNKNOWN_ARTISTS = {"compilations", "unknown artist", "various artists", ""}
TRACK_NUMBER = re.compile(r"^(?:\d{1,2}-)?\d{1,3}[ .-]+")
ARTIST_SPLIT = re.compile(r"\s*(?:,|;|&| x | feat\.? | ft\.? | featuring )\s*", re.I)
# Suffixes some download tools append to titles and filenames.
DOWNLOAD_SUFFIX = re.compile(r"[\s_-]*\(?(?:high|low)[\s_]quality\)?$", re.I)

TAG_KEYS = {
    "title": ("TIT2", "\xa9nam", "title", "TITLE"),
    "artist": ("TPE1", "\xa9ART", "artist", "ARTIST"),
    "album": ("TALB", "\xa9alb", "album", "ALBUM"),
    "isrc": ("TSRC", "----:com.apple.iTunes:ISRC", "isrc", "ISRC"),
}


@dataclass(frozen=True)
class LocalFile:
    path: str
    size: int
    mtime: float
    title: str
    artists: tuple[str, ...]
    album: str
    duration_ms: int | None
    isrc: str | None
    metadata_source: str

    def as_track(self) -> Track:
        return Track(
            "local",
            self.path,
            self.title,
            self.artists,
            self.duration_ms,
            self.isrc,
            available=True,
            album_title=self.album,
        )


def _tag(tags: Any, field: str) -> str:
    if tags is None:
        return ""
    for key in TAG_KEYS[field]:
        try:
            value = tags.get(key)
        except (KeyError, ValueError, TypeError):
            continue
        if value is None:
            continue
        if hasattr(value, "text"):
            value = value.text
        if isinstance(value, list | tuple):
            value = value[0] if value else ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        text = str(value).strip()
        if text:
            return text
    return ""


def clean_title(text: str) -> str:
    return " ".join(DOWNLOAD_SUFFIX.sub("", text).replace("_", " ").split())


def split_artists(text: str) -> tuple[str, ...]:
    return tuple(a for a in ARTIST_SPLIT.split(text.strip()) if a)


def path_metadata(parts: tuple[str, ...]) -> dict[str, Any]:
    """Title, artist and album implied by Music.app's Artist/Album/NN Title layout."""
    stem = Path(parts[-1]).stem
    title = clean_title(TRACK_NUMBER.sub("", stem)) or stem
    artist = parts[-3] if len(parts) >= 3 else ""
    album = parts[-2] if len(parts) >= 2 else ""
    known = artist.casefold() not in UNKNOWN_ARTISTS
    return {
        "title": " ".join(title.split()),
        "artists": split_artists(artist) if known else (),
        "album": album if album.casefold() != "unknown album" else "",
    }


def read_file(path: Path, root: Path) -> LocalFile | None:
    """Read tags and length. Returns None when the file is not recognizable audio."""
    audio = mutagen.File(path)  # read-only: no save() is ever called
    if audio is None:
        return None
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    fallback = path_metadata(parts)
    title = _tag(audio.tags, "title")
    artist = _tag(audio.tags, "artist")
    source = "tags" if title and artist else "path" if not title and not artist else "mixed"
    length = getattr(audio.info, "length", None)
    stat = path.stat()
    return LocalFile(
        str(path),
        stat.st_size,
        stat.st_mtime,
        clean_title(title) if title else fallback["title"],
        split_artists(artist) if artist else fallback["artists"],
        _tag(audio.tags, "album") or fallback["album"],
        round(length * 1000) if length else None,
        normalize_isrc(_tag(audio.tags, "isrc")),
        source,
    )


def scan(roots: Iterable[Path]) -> tuple[list[LocalFile], list[tuple[str, str]]]:
    files: list[LocalFile] = []
    problems: list[tuple[str, str]] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in AUDIO or not path.is_file() or path.is_symlink():
                continue
            try:
                found = read_file(path, root)
                reason = None if found else "not recognized as audio"
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the scan
                found, reason = None, type(exc).__name__
            if found is None:
                # Still index it from its folder layout, without a length, so the track can
                # be flagged as possibly owned instead of silently looking absent.
                problems.append((str(path), reason or "unreadable"))
                found = _from_path_only(path, root)
            files.append(found)
    return files, problems


def _from_path_only(path: Path, root: Path) -> LocalFile:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    meta = path_metadata(parts)
    stat = path.stat()
    return LocalFile(
        str(path),
        stat.st_size,
        stat.st_mtime,
        meta["title"],
        meta["artists"],
        meta["album"],
        None,
        None,
        "path-unreadable",
    )


def index_files(ledger: Ledger, files: list[LocalFile]) -> None:
    """Replace the file index with this scan. The index is metadata only."""
    with ledger.connection:
        ledger.connection.execute("DELETE FROM local_files")
        ledger.connection.executemany(
            "INSERT INTO local_files VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f.path,
                    f.size,
                    f.mtime,
                    f.title,
                    json.dumps(f.artists),
                    f.album,
                    f.duration_ms,
                    f.isrc,
                    f.metadata_source,
                    time.time(),
                )
                for f in files
            ],
        )


def _indexed(ledger: Ledger) -> list[LocalFile]:
    return [
        LocalFile(r[0], r[1], r[2], r[3], tuple(json.loads(r[4])), r[5], r[6], r[7], r[8])
        for r in ledger.connection.execute(
            "SELECT path,size,mtime,title,artists_json,album,duration_ms,isrc,metadata_source "
            "FROM local_files"
        )
    ]


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def classify(source: Track, candidates: list[LocalFile]) -> tuple[str, list[LocalFile]]:
    """found: same recording on disk. possibly_found: same title/artist, length unknown.
    other_version: same song, different mix or length."""
    found: list[LocalFile] = []
    possible: list[LocalFile] = []
    other: list[LocalFile] = []
    lead = normalize(source.artists[0]) if source.artists else ""
    base = identity(source)[0]
    for candidate in candidates:
        track = candidate.as_track()
        result = score(source, track)
        same_isrc = bool(source.isrc and track.isrc and source.isrc == track.isrc)
        within = (
            source.duration_ms is not None
            and track.duration_ms is not None
            and abs(source.duration_ms - track.duration_ms) <= max(3000, source.duration_ms * 0.01)
        )
        same_song = (
            identity(track) == identity(source)
            and bool(track.artists)
            and _similar(lead, normalize(track.artists[0])) >= 0.94
        )
        if (result.eligible and result.score >= 0.89) or (same_isrc and within):
            found.append(candidate)
        elif same_song and track.duration_ms is None:
            possible.append(candidate)
        elif (
            _similar(base, identity(track)[0]) >= 0.94
            and track.artists
            and _similar(lead, normalize(track.artists[0])) >= 0.94
        ):
            other.append(candidate)
    if found:
        return "found", found
    if possible:
        return "possibly_found", possible
    return ("other_version", other) if other else ("missing", [])


def match_local(ledger: Ledger, mappings: dict[str, str]) -> dict[str, Any]:
    """Compare every Spotify track in mapped playlists with the local file index."""
    files = _indexed(ledger)
    by_title: dict[str, list[LocalFile]] = {}
    by_isrc: dict[str, list[LocalFile]] = {}
    for f in files:
        by_title.setdefault(identity(f.as_track())[0], []).append(f)
        if f.isrc:
            by_isrc.setdefault(f.isrc, []).append(f)
    tracks: dict[str, dict[str, Any]] = {}
    for playlist_id, group in mappings.items():
        latest = ledger.latest_complete_report(playlist_id)
        if not latest:
            continue
        report = latest[2]
        label = f"{group} / {report['playlist']['name']}"
        for row in report["rows"]:
            src = row["source"]
            if not src:
                continue
            entry = tracks.get(src["id"])
            if entry is None:
                source = Track(**{**src, "artists": tuple(src["artists"])})
                pool = {f.path: f for f in by_title.get(identity(source)[0], [])}
                pool.update({f.path: f for f in by_isrc.get(source.isrc or "", [])})
                status, hits = classify(source, list(pool.values()))
                hits.sort(key=lambda f: f.path)
                entry = tracks[src["id"]] = {
                    "spotify": {
                        "id": src["id"],
                        "title": src["title"],
                        "artists": list(src["artists"]),
                        "duration_ms": src["duration_ms"],
                    },
                    "tidal_status": row["match"]["status"],
                    "local_status": status,
                    "local": _describe(hits[0]) if hits else None,
                    "local_paths": [f.path for f in hits],
                    "local_copies": len(hits),
                    "playlists": [],
                }
            entry["playlists"].append(label)
    _record(ledger, tracks)
    found = [t for t in tracks.values() if t["local_status"] == "found"]
    return {
        "generated_at": time.time(),
        "files_indexed": len(files),
        "tracks": list(tracks.values()),
        "totals": {
            "spotify_tracks": len(tracks),
            "found": len(found),
            "possibly_found": sum(
                1 for t in tracks.values() if t["local_status"] == "possibly_found"
            ),
            "other_version": sum(
                1 for t in tracks.values() if t["local_status"] == "other_version"
            ),
            "missing": sum(1 for t in tracks.values() if t["local_status"] == "missing"),
            "found_and_tidal_matched": sum(1 for t in found if t["tidal_status"] == "matched"),
            "found_with_several_copies": sum(1 for t in found if t["local_copies"] > 1),
        },
    }


def _describe(f: LocalFile) -> dict[str, Any]:
    return {
        "path": f.path,
        "title": f.title,
        "artists": list(f.artists),
        "duration_ms": f.duration_ms,
        "metadata_source": f.metadata_source,
    }


def _record(ledger: Ledger, tracks: dict[str, dict[str, Any]]) -> None:
    """Record detected files. Only unverified, scan-derived links are replaced."""
    now = time.time()
    with ledger.connection:
        ledger.connection.execute(
            "DELETE FROM local_assets WHERE source='scan' AND import_verified=0 "
            "AND music_persistent_id IS NULL"
        )
        ledger.connection.execute("DELETE FROM sync_status WHERE stage='local_file_detected'")
        for spotify_id, t in tracks.items():
            if t["local_status"] not in ("found", "possibly_found"):
                continue
            row = ledger.connection.execute(
                "SELECT track_id FROM service_tracks WHERE service='spotify' AND service_id=?",
                (spotify_id,),
            ).fetchone()
            if not row:
                continue
            if t["local_status"] == "found":
                ledger.connection.executemany(
                    "INSERT INTO local_assets(track_id,path,source,import_verified) "
                    "VALUES(?,?,'scan',0) ON CONFLICT(track_id,path) DO NOTHING",
                    [(row[0], path) for path in t["local_paths"]],
                )
            ledger.connection.execute(
                "INSERT INTO sync_status VALUES(?,'local_file_detected',?,?,1) "
                "ON CONFLICT(track_id,stage) DO UPDATE SET status=excluded.status,"
                "updated=excluded.updated",
                (row[0], t["local_status"], now),
            )
        # Ownership established through Music.app's own index outlives any rescan.
        ledger.connection.execute(
            """INSERT INTO sync_status SELECT DISTINCT track_id,'local_file_detected','found',?,1
            FROM local_assets WHERE music_persistent_id IS NOT NULL
            ON CONFLICT(track_id,stage) DO UPDATE SET status='found',updated=excluded.updated""",
            (now,),
        )
