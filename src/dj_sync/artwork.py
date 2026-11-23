"""Cover art for tool-made AIFFs, in a form Music.app shows.

Until 2026-10-20 the converter wrote the cover as an ID3v2.3 APIC frame with a UTF-16
description, which Music.app registers but cannot read, so the track shows no cover.
Two steps fix a track: rewrite that one frame as Latin-1 (audio bytes untouched,
checked), then hand the image to Music.app so it refreshes its own artwork store; a
file fix alone is not picked up. Only library files that carry the TIDAL URL of one
of the tool's own downloads are ever touched.
"""

import hashlib
import struct
import time
from pathlib import Path
from typing import Any

from mutagen.aiff import AIFF
from mutagen.id3 import APIC

from .acquire import tidal_id_from_url
from .db import Ledger
from .errors import SyncError
from .music_app import MusicApp
from .storage import private_dir


def _sound_digest(path: Path) -> str:
    """SHA-256 of the AIFF's SSND (audio) chunk."""
    data = path.read_bytes()
    if data[:4] != b"FORM":
        raise SyncError(f"'{path.name}' is not an AIFF file.")
    i = 12
    while i + 8 <= len(data):
        cid, size = data[i : i + 4], struct.unpack(">I", data[i + 4 : i + 8])[0]
        if cid == b"SSND":
            return hashlib.sha256(data[i : i + 8 + size]).hexdigest()
        i += 8 + size + (size & 1)
    raise SyncError(f"'{path.name}' has no audio chunk.")


def cover(path: Path) -> Any:
    tags = AIFF(path).tags
    pics: list[Any] = tags.getall("APIC") if tags is not None else []
    return pics[0] if pics else None


def tidal_id(path: Path) -> str | None:
    tags = AIFF(path).tags
    if tags is None:
        return None
    for frame in tags.getall("TXXX"):
        if frame.desc == "TIDAL URL" and frame.text:
            return tidal_id_from_url(str(frame.text[0]))
    return None


def fix_file_cover(path: Path) -> bool:
    """Rewrite a non-Latin-1 cover frame as Latin-1. True when the file changed."""
    pic = cover(path)
    if pic is None or (int(pic.encoding) == 0 and pic.desc == ""):
        return False
    before = _sound_digest(path)
    audio = AIFF(path)
    assert audio.tags is not None
    version = audio.tags.version[1]
    audio.tags.delall("APIC")
    audio.tags.add(APIC(encoding=0, mime=pic.mime, type=pic.type, desc="", data=pic.data))
    audio.save(v2_version=3 if version == 3 else 4)
    after = cover(path)
    if _sound_digest(path) != before:
        raise SyncError(f"Audio of '{path.name}' changed while fixing its cover; stopping.")
    if after is None or after.data != pic.data or int(after.encoding) != 0:
        raise SyncError(f"Cover of '{path.name}' did not read back after the fix; stopping.")
    return True


def hand_to_music(app: MusicApp, music_id: str, pic: Any, scratch: Path) -> None:
    """Give Music.app the cover image so its artwork store shows it."""
    suffix = ".png" if "png" in (pic.mime or "").lower() else ".jpg"
    image = scratch / f"{music_id}{suffix}"
    image.write_bytes(pic.data)
    try:
        app.set_artwork(music_id, str(image))
    finally:
        image.unlink(missing_ok=True)


def record_handed(ledger: Ledger, music_id: str) -> None:
    with ledger.connection:
        ledger.connection.execute(
            "INSERT OR REPLACE INTO music_artwork VALUES(?,?)", (music_id, time.time())
        )


def repair_artwork(
    ledger: Ledger,
    app: MusicApp,
    data_dir: Path,
    *,
    apply: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Fix covers of library copies of the tool's own downloads. Dry run unless ``apply``."""
    c = ledger.connection
    rows = c.execute(
        """SELECT t.persistent_id, t.location, t.name, group_concat(DISTINCT d.tidal_id)
        FROM downloads d JOIN local_assets la ON la.track_id=d.track_id
        JOIN music_tracks t ON t.persistent_id=la.music_persistent_id
        WHERE d.status='imported' AND t.location IS NOT NULL
        AND t.persistent_id NOT IN (SELECT persistent_id FROM music_artwork)
        GROUP BY t.persistent_id ORDER BY t.location"""
    ).fetchall()
    result: dict[str, Any] = {
        "mode": "applied" if apply else "dry_run",
        "candidates": len(rows),
        "files_fixed": 0,
        "handed_to_music": 0,
        "skipped": [],
        "deferred_by_limit": 0,
    }
    scratch = data_dir / "artwork-tmp"
    private_dir(scratch)
    done = 0
    for music_id, location, name, tidal_ids in rows:
        path = Path(location)
        if not path.is_file():
            result["skipped"].append({"name": name, "reason": "file missing"})
            continue
        if tidal_id(path) not in str(tidal_ids).split(","):
            # Not provably one of the tool's own downloads: never touch it.
            result["skipped"].append({"name": name, "reason": "not the tool's download"})
            continue
        pic = cover(path)
        if pic is None:
            result["skipped"].append({"name": name, "reason": "no embedded cover"})
            continue
        if limit is not None and done >= limit:
            result["deferred_by_limit"] += 1
            continue
        done += 1
        if not apply:
            continue
        if fix_file_cover(path):
            result["files_fixed"] += 1
        hand_to_music(app, str(music_id), pic, scratch)
        result["handed_to_music"] += 1
        record_handed(ledger, str(music_id))
    return result
