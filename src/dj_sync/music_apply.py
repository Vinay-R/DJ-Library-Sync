"""Place tracks into Music.app playlists inside the two confirmed managed folders.

Additive only: creates missing playlists inside the confirmed folder, adds library
tracks by persistent ID, imports converted AIFFs. Never removes, renames, reorders or
deletes anything. Every write is preceded by a live ancestry check against the
confirmed folder IDs, and every import is journaled before the call so a crash can
be reconciled without importing twice. Dry run unless ``apply``.
"""

import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import mutagen

from .artwork import cover, hand_to_music, record_handed
from .db import Ledger
from .errors import SyncError
from .music_app import MusicApp, build_report, confirmed_folders
from .storage import private_dir, save_json
from .track_matcher import normalize

ADDS_PER_CALL = 50


def _backup(ledger: Ledger, directory: Path) -> Path:
    """Export every indexed playlist under the managed folders with its memberships."""
    c = ledger.connection
    playlists = [
        {"id": r[0], "name": r[1], "kind": r[2], "parent": r[3]}
        for r in c.execute("SELECT persistent_id,name,kind,parent_id FROM music_playlists")
    ]
    memberships: dict[str, list[str]] = {}
    for pid, tid in c.execute(
        "SELECT playlist_id, track_id FROM music_memberships ORDER BY playlist_id, position"
    ):
        memberships.setdefault(pid, []).append(tid)
    path = directory / "backups" / f"music-playlists-{int(time.time())}.json"
    save_json(
        path, {"exported_at": time.time(), "playlists": playlists, "memberships": memberships}
    )
    return path


def _intent_path(directory: Path, aiff: str) -> Path:
    return directory / "music-imports" / (hashlib.sha256(aiff.encode()).hexdigest() + ".json")


def _guard(app: MusicApp, playlist_id: str, folder_id: str, what: str) -> None:
    chain = app.ancestry(playlist_id)
    if not chain.get("exists") or folder_id not in chain.get("ancestors", []):
        raise SyncError(
            f"Refusing to write: {what} is outside the confirmed managed folder. "
            "Review the Music.app layout or clear the stored destination."
        )


def _within(a: float | None, b: float | None, tolerance_ms: float = 3000) -> bool:
    return a is not None and b is not None and abs(a - b) <= tolerance_ms


def apply_music(
    ledger: Ledger,
    app: MusicApp,
    mappings: dict[str, str],
    data_dir: Path,
    *,
    apply: bool = False,
    archive: bool = False,
    only: set[str] | None = None,
    max_writes: int | None = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """``only`` restricts to those Spotify playlist IDs; ``max_writes`` caps track adds and
    imports across the run (playlist creation is not counted). Both are for careful ramps."""
    folders = confirmed_folders(ledger)
    if only is not None:
        mappings = {k: v for k, v in mappings.items() if k in only}
    report = build_report(ledger, mappings)
    budget = max_writes
    c = ledger.connection
    now = time.time()
    result: dict[str, Any] = {
        "mode": "applied" if apply else "dry_run",
        "playlists": [],
        "writes_performed": 0,
        "referenced_in_place": [],
        "archived": 0,
    }
    backup: Path | None = None
    imported_this_run: dict[str, dict[str, Any]] = {}
    for entry in report["playlists"]:
        folder_id = folders[entry["group"]]
        dest = dict(entry["destination"])
        out: dict[str, Any] = {
            "spotify_id": entry["spotify_id"],
            "name": entry["name"],
            "destination": dest,
            "planned": {},
            "done": {},
            "status": "ok",
        }
        result["playlists"].append(out)
        stored = c.execute(
            "SELECT music_persistent_id FROM spotify_playlists WHERE id=?", (entry["spotify_id"],)
        ).fetchone()
        stored_id = stored[0] if stored and stored[0] else None
        if stored_id and dest.get("id") != stored_id:
            # A stored destination that is no longer inside the folder is never silently
            # replaced; the user decides.
            _guard(app, stored_id, folder_id, f"the stored destination of '{entry['name']}'")
        planned = Counter(
            "import" if t["action"] == "import_pending" else t["action"] for t in entry["tracks"]
        )
        planned.pop("in_playlist", None)
        out["planned"] = dict(planned)
        if dest["how"] == "ambiguous":
            out["status"] = "skipped"
            out["error"] = "several playlists with this name inside the folder; pick one by hand"
            continue
        if not apply:
            continue
        if backup is None:
            backup = _backup(ledger, data_dir)
            result["backup"] = str(backup)
        done: Counter[str] = Counter()
        if dest["how"] == "create":
            created = app.create_playlist(folder_id, entry["name"])
            if created.get("parent") != folder_id or not created.get("id"):
                raise SyncError(
                    f"Music.app created '{entry['name']}' outside the managed folder; stopping."
                )
            dest.update(id=str(created["id"]), how="created")
            done["created"] += 1
            result["writes_performed"] += 1
            with c:
                # Keep the ledger's index consistent so a rerun adopts this playlist.
                c.execute(
                    "INSERT OR REPLACE INTO music_playlists VALUES(?,?,?,?,?)",
                    (str(created["id"]), entry["name"], "none", folder_id, now),
                )
        else:
            _guard(app, str(dest["id"]), folder_id, f"playlist '{dest['name']}'")
        playlist_id = str(dest["id"])
        with c:
            c.execute(
                "UPDATE spotify_playlists SET music_persistent_id=? WHERE id=?",
                (playlist_id, entry["spotify_id"]),
            )
        members = set(app.members([playlist_id]).get(playlist_id, []))
        # A playlist may already hold the same song as a different library entry (the
        # library has several copies). Never add a second entry for the same song.
        member_keys = _song_keys(ledger, members)
        expected: dict[str, str] = {}  # music track id -> spotify id
        to_add: list[str] = []
        deferred = 0
        imports_made = False
        for track in entry["tracks"]:
            action = track["action"]
            music_id = track.get("music_track_id")
            needs_write = (
                (action == "add_existing" and music_id not in members)
                or (action == "import_pending" and track.get("staged_aiff"))
                or (action == "import_from_disk" and track.get("disk_file"))
            )
            if needs_write and budget is not None:
                if budget <= 0:
                    deferred += 1
                    continue
                budget -= 1
            if action == "in_playlist" and music_id:
                expected[music_id] = track["spotify_id"]
            elif action == "add_existing" and music_id:
                if music_id not in members and _song_keys(ledger, {music_id}) & member_keys:
                    done["present_by_name"] += 1
                    continue
                expected[music_id] = track["spotify_id"]
                if music_id not in members:
                    to_add.append(music_id)
            elif action == "import_pending" and track.get("staged_aiff"):
                aiff = str(track["staged_aiff"])
                imported = imported_this_run.get(aiff) or _import(
                    ledger,
                    app,
                    data_dir,
                    playlist_id,
                    aiff,
                    track["spotify_id"],
                    done,
                    result,
                    sleep=sleep,
                )
                imported_this_run[aiff] = imported
                expected[imported["id"]] = track["spotify_id"]
                imports_made = True
            elif action == "import_from_disk" and track.get("disk_file"):
                # A file inside the library folder that Music.app no longer lists: add it back.
                path = str(track["disk_file"])
                imported = imported_this_run.get(path) or _import(
                    ledger,
                    app,
                    data_dir,
                    playlist_id,
                    path,
                    track["spotify_id"],
                    done,
                    result,
                    from_disk=True,
                    sleep=sleep,
                )
                imported_this_run[path] = imported
                expected[imported["id"]] = track["spotify_id"]
                imports_made = True
        if imports_made:
            # Music.app does not reliably place an import into the target playlist, and a
            # shared import was placed only in the first playlist. Re-read and add whatever
            # expected track is still missing, so nothing is ever doubled or skipped.
            queued = set(to_add)
            live = set(app.members([playlist_id]).get(playlist_id, []))
            for _ in range(6):
                # Give Music.app time to reflect imports it did place; adding before that
                # would create a second entry.
                if all(m in live or m in queued for m in expected):
                    break
                sleep(5)
                live = set(app.members([playlist_id]).get(playlist_id, []))
            to_add.extend(m for m in expected if m not in live and m not in queued)
        for start in range(0, len(to_add), ADDS_PER_CALL):
            batch = to_add[start : start + ADDS_PER_CALL]
            outcomes = app.duplicate(playlist_id, batch)
            result["writes_performed"] += 1
            failed = [o for o in outcomes if not o.get("ok")]
            if failed:
                names = ", ".join(
                    str(
                        c.execute(
                            "SELECT name FROM music_tracks WHERE persistent_id=?",
                            (o.get("track_id"),),
                        ).fetchone()
                        or (o.get("track_id"),)
                    )[0]
                    for o in failed[:5]
                )
                raise SyncError(
                    f"Music.app could not add {len(failed)} track(s) to '{dest['name']}' "
                    f"({names}; {failed[0].get('error', 'no detail')}); stopping before "
                    "verification."
                )
            done["added"] += len(batch)
        after = app.members([playlist_id]).get(playlist_id, [])
        missing = [m for m in expected if m not in set(after)]
        # Music.app can take a moment to reflect a batch of adds; re-read before failing.
        for _ in range(6):
            if not missing:
                break
            sleep(5)
            after = app.members([playlist_id]).get(playlist_id, [])
            missing = [m for m in expected if m not in set(after)]
        if missing:
            raise SyncError(
                f"Music.app readback of '{dest['name']}' lacks {len(missing)} expected track(s)."
            )
        with c:
            c.execute("DELETE FROM music_memberships WHERE playlist_id=?", (playlist_id,))
            c.executemany(
                "INSERT OR IGNORE INTO music_memberships VALUES(?,?,?)",
                [(playlist_id, tid, pos) for pos, tid in enumerate(after)],
            )
            for spotify_id in expected.values():
                row = c.execute(
                    "SELECT track_id FROM service_tracks WHERE service='spotify' AND service_id=?",
                    (spotify_id,),
                ).fetchone()
                if row:
                    c.execute(
                        """INSERT INTO sync_status VALUES(?,'music_playlist_member','verified',?,1)
                        ON CONFLICT(track_id,stage) DO UPDATE SET status='verified',
                        updated=excluded.updated""",
                        (row[0], now),
                    )
        out["done"] = {k: v for k, v in done.items() if v}
        out["deferred_by_limit"] = deferred
    if apply and archive:
        result["archived"] = _archive(ledger, imported_this_run)
    return result


def _import(
    ledger: Ledger,
    app: MusicApp,
    data_dir: Path,
    playlist_id: str,
    aiff: str,
    spotify_id: str,
    done: Counter[str],
    result: dict[str, Any],
    *,
    from_disk: bool = False,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Import one file into Music.app (and this playlist) exactly once.

    ``from_disk`` marks a file that already lives in the library folder; it is expected
    to stay in place and is never archived.
    """
    c = ledger.connection
    intent_path = _intent_path(data_dir, aiff)
    intent: dict[str, Any] | None = None
    if intent_path.exists():
        intent = json.loads(intent_path.read_text())
    found: dict[str, Any] | None = None
    if intent and intent.get("status") == "add_sent":
        # The earlier call may have imported the file before the response was lost. Music.app
        # renames copied files, so match on title, artist, length and date added instead.
        found = _adopt(app, aiff, float(intent.get("started", 0)))
        if found:
            done["adopted_import"] += 1
    if found is None:
        intent = {
            "aiff": aiff,
            "playlist_id": playlist_id,
            "spotify_id": spotify_id,
            "status": "add_sent",
            "started": time.time(),
        }
        save_json(intent_path, intent)
        try:
            found = app.add_file(playlist_id, aiff)
        except Exception as exc:  # noqa: BLE001 - any failure leaves the intent for reconciliation
            raise SyncError(
                f"Music.app import of '{Path(aiff).name}' ended uncertainly ({exc}); the intent "
                "is kept and the next run reconciles it before importing again."
            ) from exc
        result["writes_performed"] += 1
        if not found.get("id"):
            # The add succeeded but the returned reference was unusable; find the track the
            # same way an interrupted import is reconciled.
            resolved = _adopt(app, aiff, float(intent["started"]))
            if resolved is None:
                raise SyncError(
                    f"Music.app did not report an identity for '{Path(aiff).name}' and no "
                    "matching track was found; the intent is kept for the next run."
                )
            found = resolved

        done["imported_from_disk" if from_disk else "imported"] += 1
    music_id = str(found.get("id") or "")
    if not music_id:
        raise SyncError(f"Music.app returned no track identity for '{Path(aiff).name}'.")
    location = found.get("location") or ""
    if not location:
        location = _wait_for_location(app, music_id, aiff)
    referenced = Path(location) == Path(aiff)
    if referenced and not from_disk:
        result["referenced_in_place"].append(aiff)
    row = c.execute(
        "SELECT track_id FROM service_tracks WHERE service='spotify' AND service_id=?",
        (spotify_id,),
    ).fetchone()
    now = time.time()
    with c:
        if row:
            c.execute(
                """INSERT INTO local_assets(track_id,path,source,music_persistent_id,
                import_verified) VALUES(?,?,'music',?,1) ON CONFLICT(track_id,path) DO UPDATE SET
                music_persistent_id=excluded.music_persistent_id, import_verified=1""",
                (row[0], location, music_id),
            )
            c.execute(
                """INSERT INTO sync_status VALUES(?,'music_imported','verified',?,1)
                ON CONFLICT(track_id,stage) DO UPDATE SET status='verified',
                updated=excluded.updated""",
                (row[0], now),
            )
        c.execute(
            "UPDATE downloads SET status='imported' WHERE aiff_path=? AND status='converted'",
            (aiff,),
        )
        c.execute(
            """INSERT INTO music_tracks VALUES(?,?,?,?,?,?) ON CONFLICT(persistent_id) DO UPDATE SET
            location=excluded.location""",
            (
                music_id,
                found.get("name") or Path(aiff).stem,
                "",
                round(float(found["duration"]) * 1000) if found.get("duration") else None,
                location,
                now,
            ),
        )
    assert intent is not None
    intent.update(status="verified", music_id=music_id, location=location, verified_at=now)
    save_json(intent_path, intent)
    if not from_disk:
        # Music.app keeps its own artwork store and does not always fill it from the file.
        try:
            pic = cover(Path(aiff))
        except Exception:  # noqa: BLE001 - an unreadable cover never fails an import
            pic = None
        if pic is not None:
            scratch = data_dir / "artwork-tmp"
            private_dir(scratch)
            # Music.app refuses artwork while it is still finishing a fresh import; wait and retry.
            for attempt in range(4):
                try:
                    hand_to_music(app, music_id, pic, scratch)
                    done["covers"] += 1
                    record_handed(ledger, music_id)
                    break
                except SyncError as exc:
                    if attempt == 3:
                        # `dj-sync music artwork --apply` picks these up later.
                        result.setdefault("cover_failures", []).append(f"{Path(aiff).name}: {exc}")
                    else:
                        sleep(5)
    return {**found, "referenced": referenced or from_disk}


def _song_keys(ledger: Ledger, music_ids: set[str]) -> set[tuple[str, str]]:
    """(normalized title, normalized lead artist) for indexed Music.app tracks."""
    keys: set[tuple[str, str]] = set()
    if not music_ids:
        return keys
    marks = ",".join("?" * len(music_ids))
    for name, artist in ledger.connection.execute(
        f"SELECT name, artist FROM music_tracks WHERE persistent_id IN ({marks})",
        tuple(music_ids),
    ):
        lead = (artist or "").split(",")[0].split("&")[0]
        keys.add((normalize(name or ""), normalize(lead)))
    return keys


def _file_identity(path: str) -> tuple[str, str, float | None]:
    """Title, lead artist and length of a staged file, from tags or its Tidaler filename."""
    title, artist, length = "", "", None
    try:
        audio = mutagen.File(path)
        if audio is not None:
            length = float(getattr(audio.info, "length", 0) or 0) or None
            tags = audio.tags
            if tags is not None:
                tit = tags.get("TIT2")
                art = tags.get("TPE1")
                title = str(tit.text[0]) if tit is not None and getattr(tit, "text", None) else ""
                artist = str(art.text[0]) if art is not None and getattr(art, "text", None) else ""
    except Exception:  # noqa: BLE001 - fall back to the filename
        pass
    if not title or not artist:
        stem = re.sub(r"\s*\[\d+\]$", "", Path(path).stem)
        head, _, tail = stem.partition(" - ")
        artist = artist or head.split(",")[0].strip()
        title = title or tail.strip() or stem
    return title, artist, length


def _wait_for_location(
    app: MusicApp, music_id: str, path: str, *, attempts: int = 12, sleep: Any = time.sleep
) -> str:
    """Music.app reports a copied file's location only once the copy is finished."""
    title, _, _ = _file_identity(path)
    for i in range(attempts):
        for track in app.find_tracks(title):
            if str(track.get("id")) == music_id and track.get("location"):
                return str(track["location"])
        if i < attempts - 1:
            sleep(5)
    raise SyncError(
        f"Music.app imported '{Path(path).name}' (track {music_id}) but has not reported its "
        "file location yet; rerun in a minute to finish recording it."
    )


def _adopt(app: MusicApp, path: str, started: float) -> dict[str, Any] | None:
    """Find the Music.app track an interrupted import produced, or None."""
    title, artist, length = _file_identity(path)
    lead = normalize(artist)
    candidates = []
    for track in app.find_tracks(title):
        if lead and lead not in normalize(str(track.get("artist") or "")):
            continue
        if length and track.get("duration") and abs(float(track["duration"]) - length) > 2:
            continue
        added = track.get("added")
        if added is not None and added < started - 120:
            continue
        candidates.append(track)
    if len(candidates) > 1:
        raise SyncError(
            f"Several Music.app tracks could be the interrupted import of '{Path(path).name}'; "
            "resolve by hand before rerunning."
        )
    if candidates:
        t = candidates[0]
        return {
            "id": str(t["id"]),
            "location": t.get("location") or "",
            "duration": t.get("duration"),
            "name": t.get("name"),
        }
    name = Path(path).name
    for track in app.index()["tracks"]:
        location = track.get("location") or ""
        if location and Path(location).name == name:
            return {
                "id": str(track["id"]),
                "location": location,
                "duration": track.get("duration"),
                "name": track.get("name"),
            }
    return None


def _archive(ledger: Ledger, imported: dict[str, dict[str, Any]]) -> int:
    """Move tool-downloaded originals of verified copy-imports into imported/ subfolders.

    Covers imports from this run and earlier ones. A file is moved only when the track
    Music.app holds lives somewhere else (it copied the file), never when it references
    the staging file in place, and never for files the user put in Tidaler's folder.
    """
    c = ledger.connection
    moved = 0
    rows = c.execute(
        "SELECT aiff_path, flac_path FROM downloads WHERE status='imported' AND origin='tool' "
        "AND aiff_path IS NOT NULL GROUP BY aiff_path"
    ).fetchall()
    for aiff, flac in rows:
        if imported.get(aiff, {}).get("referenced"):
            continue
        location = c.execute(
            "SELECT a.path FROM local_assets a JOIN downloads d ON d.track_id=a.track_id "
            "WHERE d.aiff_path=? AND a.music_persistent_id IS NOT NULL LIMIT 1",
            (aiff,),
        ).fetchone()
        if not location or Path(location[0]) == Path(aiff):
            continue
        updates: dict[str, tuple[str, str]] = {}
        for column, current in (("flac_path", flac), ("aiff_path", aiff)):
            if not current:
                continue
            source = Path(current)
            if not source.is_file() or source.parent.name == "imported":
                continue
            target_dir = source.parent / "imported"
            private_dir(target_dir)
            target = target_dir / source.name
            if target.exists():
                continue
            source.rename(target)
            updates[column] = (current, str(target))
        if updates:
            with c:
                for column, (old, new) in updates.items():
                    c.execute(f"UPDATE downloads SET {column}=? WHERE {column}=?", (new, old))
            moved += 1
    return moved
