"""Read-only Music.app index through its scripting interface (osascript / JXA).

Nothing here changes Music.app. Playlists, folder ancestry, file tracks and the
memberships of playlists under the two managed folders are copied into the ledger,
and Spotify tracks that Music.app already holds are marked as owned. Managed folders
are identified by persistent ID after the user confirms them, never by name alone.
"""

import json
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any

from .config import DESTINATIONS
from .db import Ledger
from .errors import SyncError
from .models import Track
from .track_matcher import identity, score

Runner = Callable[[str, list[str]], str]

# Bulk property reads (one Apple event per property, not per item) keep a 24k-track
# library to a few seconds. `parent` is read per playlist because it raises for roots.
INDEX_SCRIPT = r"""
function run(argv) {
  const app = Application("Music");
  const pls = app.playlists;
  const ids = pls.persistentID(), names = pls.name(), kinds = pls.specialKind(),
        classes = pls.class();
  const playlists = [];
  for (let i = 0; i < ids.length; i++) {
    let parent = null;
    try { parent = pls[i].parent.persistentID(); } catch (e) { parent = null; }
    playlists.push({id: ids[i] == null ? null : String(ids[i]),
                    name: names[i] == null ? null : String(names[i]),
                    kind: kinds[i] == null ? null : String(kinds[i]),
                    cls: classes[i] == null ? null : String(classes[i]), parent: parent});
  }
  const lib = app.libraryPlaylists[0].fileTracks;
  const tids = lib.persistentID(), tnames = lib.name(), artists = lib.artist(),
        durations = lib.duration(), locations = lib.location();
  const tracks = [];
  for (let i = 0; i < tids.length; i++) {
    const loc = locations[i];
    tracks.push({id: tids[i] == null ? null : String(tids[i]),
                 name: tnames[i] == null ? null : String(tnames[i]),
                 artist: artists[i] == null ? null : String(artists[i]),
                 duration: durations[i] == null ? null : durations[i],
                 location: loc ? loc.toString() : null});
  }
  return JSON.stringify({playlists: playlists, tracks: tracks});
}
"""

MEMBERS_SCRIPT = r"""
function run(argv) {
  const app = Application("Music");
  const wanted = JSON.parse(argv[0]);
  const out = {};
  for (const id of wanted) {
    const matches = app.playlists.whose({persistentID: id})();
    // Bulk property reads raise "Can't get object" on an empty playlist.
    let ids = [];
    if (matches.length) {
      const tracks = matches[0].tracks();
      ids = tracks.length ? matches[0].tracks.persistentID() : [];
    }
    out[id] = ids;
  }
  return JSON.stringify(out);
}
"""


ANCESTRY_SCRIPT = r"""
function run(argv) {
  const app = Application("Music");
  const arg = JSON.parse(argv[0]);
  const hits = app.playlists.whose({persistentID: arg.playlist_id})();
  if (!hits.length) return JSON.stringify({exists: false, ancestors: []});
  const chain = [];
  let cur = hits[0];
  for (let i = 0; i < 32; i++) {
    let id = null;
    try { id = cur.parent.persistentID(); } catch (e) { break; }
    chain.push(id);
    cur = cur.parent;
  }
  return JSON.stringify({exists: true, ancestors: chain});
}
"""

CREATE_PLAYLIST_SCRIPT = r"""
on run argv
  set folderId to item 1 of argv
  set newName to item 2 of argv
  tell application "Music"
    set theFolder to (first folder playlist whose persistent ID is folderId)
    set pl to make new user playlist at theFolder with properties {name:newName}
    set parentId to ""
    try
      set parentId to persistent ID of (get parent of pl)
    end try
    if parentId is not folderId then
      try
        move pl to theFolder
        set parentId to persistent ID of (get parent of pl)
      end try
    end if
    return (persistent ID of pl) & "|" & parentId & "|" & (name of pl)
  end tell
end run
"""

DUPLICATE_SCRIPT = r"""
function run(argv) {
  const app = Application("Music");
  const arg = JSON.parse(argv[0]);
  const pl = app.playlists.whose({persistentID: arg.playlist_id})()[0];
  const lib = app.libraryPlaylists[0];
  const out = [];
  for (const tid of arg.track_ids) {
    const hits = lib.tracks.whose({persistentID: tid})();
    if (!hits.length) { out.push({track_id: tid, ok: false, error: "not in library"}); continue; }
    try { app.duplicate(hits[0], {to: pl}); out.push({track_id: tid, ok: true}); }
    catch (e) { out.push({track_id: tid, ok: false, error: String(e)}); }
  }
  return JSON.stringify(out);
}
"""

ADD_FILE_SCRIPT = r"""
function run(argv) {
  const app = Application("Music");
  const arg = JSON.parse(argv[0]);
  const pl = app.playlists.whose({persistentID: arg.playlist_id})()[0];
  let track = app.add(Path(arg.path), {to: pl});
  if (Array.isArray(track)) track = track[0];
  // Music.app copies and renames the file ("Keep organized") after the call returns;
  // its properties can be briefly unreadable, so poll before giving up.
  let id = null, loc = null, name = null, duration = null;
  for (let i = 0; i < 12; i++) {
    try {
      id = track.persistentID(); name = track.name(); duration = track.duration();
      const l = track.location(); loc = l ? l.toString() : null;
      if (id && loc) break;
      if (id && i >= 10) break;  // location can lag while Music.app copies a large file
    } catch (e) { /* retry */ }
    delay(0.5);
  }
  return JSON.stringify({id: id, name: name, duration: duration, location: loc});
}
"""

FIND_TRACK_SCRIPT = r"""
function run(argv) {
  const app = Application("Music");
  const arg = JSON.parse(argv[0]);
  const lib = app.libraryPlaylists[0];
  const hits = lib.tracks.whose({name: arg.name})();
  const out = [];
  for (const t of hits) {
    let added = null;
    try { added = t.dateAdded().getTime() / 1000; } catch (e) { added = null; }
    const l = t.location();
    out.push({id: t.persistentID(), name: t.name(), artist: t.artist(), duration: t.duration(),
              location: l ? l.toString() : null, added: added});
  }
  return JSON.stringify(out);
}
"""

SET_ARTWORK_SCRIPT = r"""
on run argv
  set trackId to item 1 of argv
  set imagePath to item 2 of argv
  if imagePath ends with ".png" then
    set img to read (POSIX file imagePath) as «class PNGf»
  else
    set img to read (POSIX file imagePath) as JPEG picture
  end if
  tell application "Music"
    set t to first track of library playlist 1 whose persistent ID is trackId
    set data of artwork 1 of t to img
    return (count of artworks of t) as text
  end tell
end run
"""

RATINGS_SCRIPT = r"""
function run(argv) {
  const want = new Set(JSON.parse(argv[0]));
  const lib = Application("Music").libraryPlaylists[0].fileTracks;
  const ids = lib.persistentID(), ratings = lib.rating();
  let kinds = null;
  try { kinds = lib.ratingKind(); } catch (e) { kinds = null; }
  const out = {};
  for (let i = 0; i < ids.length; i++) {
    if (!want.has(ids[i])) continue;
    const kind = kinds ? String(kinds[i]) : "user";
    out[ids[i]] = {stars: Math.round((ratings[i] || 0) / 20), kind: kind};
  }
  return JSON.stringify(out);
}
"""

SET_RATINGS_SCRIPT = r"""
function run(argv) {
  const want = JSON.parse(argv[0]);
  const lib = Application("Music").libraryPlaylists[0];
  for (const [id, stars] of Object.entries(want)) {
    const hits = lib.tracks.whose({persistentID: id})();
    if (hits.length) hits[0].rating = stars * 20;
  }
  return "ok";
}
"""


def osascript_runner(script: str, args: list[str]) -> str:
    language = "AppleScript" if script.lstrip().startswith("on run") else "JavaScript"
    try:
        done = subprocess.run(
            ["osascript", "-l", language, "-e", script, *args],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError("Music.app did not answer within 15 minutes; try again later.") from exc
    if done.returncode != 0:
        err = done.stderr or ""
        if "-1743" in err or "Not authorized" in err:
            raise SyncError(
                "macOS blocked Automation access to Music. Allow it in System Settings → "
                "Privacy & Security → Automation for the app running this command, then retry."
            )
        if "-600" in err or "isn't running" in err:
            raise SyncError("Music.app is not running. Open it, then retry.")
        # osascript prefixes harmless framework log lines ("<date> osascript[pid] ..."); report
        # the script's own error instead.
        lines = [line for line in err.splitlines() if line.strip()]
        real = [
            line for line in lines if not re.match(r"^\d{4}-\d\d-\d\d [\d:.]+ osascript\[", line)
        ]
        first = (real or lines or ["no error text"])[0]
        raise SyncError(f"Music.app scripting failed: {first[:300]}")
    return done.stdout


class MusicApp:
    def __init__(self, runner: Runner = osascript_runner) -> None:
        self.runner = runner

    def index(self) -> dict[str, Any]:
        data = json.loads(self.runner(INDEX_SCRIPT, []))
        if not isinstance(data.get("playlists"), list) or not isinstance(data.get("tracks"), list):
            raise SyncError("Music.app returned an unexpected index shape.")
        return data

    def ancestry(self, playlist_id: str) -> dict[str, Any]:
        return dict(
            json.loads(self.runner(ANCESTRY_SCRIPT, [json.dumps({"playlist_id": playlist_id})]))
        )

    def create_playlist(self, folder_id: str, name: str) -> dict[str, Any]:
        raw = self.runner(CREATE_PLAYLIST_SCRIPT, [folder_id, name]).strip()
        parts = raw.split("|", 2)
        if len(parts) != 3 or not parts[0]:
            raise SyncError("Music.app did not report the new playlist's identity.")
        return {"id": parts[0], "parent": parts[1] or None, "name": parts[2]}

    def duplicate(self, playlist_id: str, track_ids: list[str]) -> list[dict[str, Any]]:
        payload = json.dumps({"playlist_id": playlist_id, "track_ids": track_ids})
        return list(json.loads(self.runner(DUPLICATE_SCRIPT, [payload])))

    def find_tracks(self, name: str) -> list[dict[str, Any]]:
        return list(json.loads(self.runner(FIND_TRACK_SCRIPT, [json.dumps({"name": name})])))

    def add_file(self, playlist_id: str, path: str) -> dict[str, Any]:
        payload = json.dumps({"playlist_id": playlist_id, "path": path})
        return dict(json.loads(self.runner(ADD_FILE_SCRIPT, [payload])))

    def set_artwork(self, track_id: str, image_path: str) -> None:
        count = self.runner(SET_ARTWORK_SCRIPT, [track_id, image_path]).strip()
        if not count.isdigit() or int(count) < 1:
            raise SyncError(f"Music.app did not take the cover for track {track_id}.")

    def ratings(self, track_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Stars (0-5) and rating kind ("user" or "computed") per Music.app track."""
        return dict(json.loads(self.runner(RATINGS_SCRIPT, [json.dumps(sorted(track_ids))])))

    def set_ratings(self, stars: dict[str, int]) -> None:
        self.runner(SET_RATINGS_SCRIPT, [json.dumps(stars)])

    def members(self, playlist_ids: list[str]) -> dict[str, list[str]]:
        if not playlist_ids:
            return {}
        data = json.loads(self.runner(MEMBERS_SCRIPT, [json.dumps(sorted(playlist_ids))]))
        return {str(k): [str(t) for t in v] for k, v in data.items()}


def is_folder(playlist: dict[str, Any]) -> bool:
    return playlist.get("cls") == "folderPlaylist" or playlist.get("kind") == "folder"


def resolve_folders(playlists: list[dict[str, Any]]) -> dict[str, str]:
    """Persistent ID of each managed folder. Exactly one folder per name, or stop."""
    folders: dict[str, str] = {}
    for group, name in DESTINATIONS.items():
        hits = [p for p in playlists if p.get("name") == name and is_folder(p)]
        if len(hits) != 1:
            raise SyncError(
                f"Expected exactly one folder playlist named '{name}' in Music.app, "
                f"found {len(hits)}. Fix the folder layout before continuing."
            )
        folders[group] = str(hits[0]["id"])
    return folders


def descendants(playlists: list[dict[str, Any]], folder_id: str) -> list[dict[str, Any]]:
    """Every playlist whose parent chain reaches folder_id (folders included)."""
    children: dict[str | None, list[dict[str, Any]]] = {}
    for p in playlists:
        children.setdefault(p.get("parent"), []).append(p)
    out: list[dict[str, Any]] = []
    stack = list(children.get(folder_id, []))
    seen: set[str] = set()
    while stack:
        p = stack.pop()
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        out.append(p)
        stack.extend(children.get(p["id"], []))
    return out


def _stray_destinations(
    ledger: Ledger, playlists: list[dict[str, Any]], managed: set[str]
) -> list[str]:
    """Stored DJ/Vibes destinations that the index lists but not under a managed folder.

    Music.app sometimes fails to report `parent` for a moment, which flattens the tree and
    makes every managed playlist look like a root playlist. Writing such an index would make
    `music apply` create a second copy of every playlist.
    """
    present = {p["id"] for p in playlists}
    marks = ",".join("?" * len(DESTINATIONS))
    stored = [
        str(r[0])
        for r in ledger.connection.execute(
            f"SELECT music_persistent_id FROM spotify_playlists WHERE music_persistent_id IS NOT "
            f"NULL AND group_name IN ({marks})",
            tuple(DESTINATIONS),
        )
    ]
    return [pid for pid in stored if pid in present and pid not in managed]


def index_music(
    ledger: Ledger,
    app: MusicApp,
    *,
    confirm_folders: bool,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    for attempt in range(attempts):
        data = app.index()
        playlists: list[dict[str, Any]] = [
            {**p, "id": str(p["id"]), "name": p.get("name") or ""}
            for p in data["playlists"]
            if p.get("id")
        ]
        tracks: list[dict[str, Any]] = [
            {**t, "id": str(t["id"]), "name": t.get("name") or ""}
            for t in data["tracks"]
            if t.get("id")
        ]
        folders = resolve_folders(playlists)
        managed = {
            p["id"]
            for fid in folders.values()
            for p in descendants(playlists, fid)
            if not is_folder(p)
        }
        stray = _stray_destinations(ledger, playlists, managed)
        if not stray:
            break
        if attempt < attempts - 1:
            sleep(3)
    else:
        raise SyncError(
            f"Music.app reported {len(stray)} linked playlist(s) outside Nu DJ Vibes / Nu Vibes "
            "Vibes, so its playlist tree looks incomplete. Nothing was recorded. Retry in a "
            "minute; if it persists, check whether those playlists were moved."
        )
    memberships = app.members(sorted(managed))
    now = time.time()
    with ledger.connection:
        c = ledger.connection
        c.execute("DELETE FROM music_playlists")
        c.executemany(
            "INSERT INTO music_playlists VALUES(?,?,?,?,?)",
            [
                (
                    p["id"],
                    p["name"],
                    "folder" if is_folder(p) else str(p.get("kind")),
                    p.get("parent"),
                    now,
                )
                for p in playlists
            ],
        )
        c.execute("DELETE FROM music_tracks")
        c.executemany(
            "INSERT INTO music_tracks VALUES(?,?,?,?,?,?)",
            [
                (
                    t["id"],
                    t.get("name") or "",
                    t.get("artist") or "",
                    round(float(t["duration"]) * 1000) if t.get("duration") is not None else None,
                    t.get("location"),
                    now,
                )
                for t in tracks
            ],
        )
        c.execute("DELETE FROM music_memberships")
        c.executemany(
            "INSERT OR IGNORE INTO music_memberships VALUES(?,?,?)",
            [
                (pid, tid, pos)
                for pid, members in memberships.items()
                for pos, tid in enumerate(members)
            ],
        )
        if confirm_folders:
            names = {p["id"]: p["name"] for p in playlists}
            c.executemany(
                """INSERT INTO music_folders VALUES(?,?,?,?) ON CONFLICT(group_name) DO UPDATE
                SET persistent_id=excluded.persistent_id,name=excluded.name,
                confirmed_at=excluded.confirmed_at""",
                [(g, fid, names[fid], now) for g, fid in folders.items()],
            )
    owned = _link_ownership(ledger)
    return {
        "folders": folders,
        "folders_confirmed": confirm_folders,
        "playlists_indexed": len(playlists),
        "managed_playlists": len(managed),
        "tracks_indexed": len(tracks),
        "memberships_indexed": sum(len(m) for m in memberships.values()),
        "spotify_tracks_owned_by_music": owned,
        "writes_performed": 0,
    }


def _link_ownership(ledger: Ledger) -> int:
    """Mark Spotify tracks whose recording Music.app already holds as found/verified."""
    c = ledger.connection
    now = time.time()
    by_title: dict[str, list[tuple[str, Track]]] = {}
    for pid, name, artist, duration_ms, location in c.execute(
        "SELECT persistent_id,name,artist,duration_ms,location FROM music_tracks "
        "WHERE location IS NOT NULL AND location != ''"
    ):
        track = Track("local", pid, name, (artist,) if artist else (), duration_ms, available=True)
        by_title.setdefault(identity(track)[0], []).append((location, track))
    with c:
        # A Music.app entry whose file is gone is a dead reference: it cannot be played or
        # added to playlists, so it never counts as owning the recording.
        c.execute(
            """DELETE FROM local_assets WHERE source='music' AND music_persistent_id IN
            (SELECT persistent_id FROM music_tracks WHERE location IS NULL OR location='')"""
        )
        c.execute(
            """UPDATE local_assets SET music_persistent_id=NULL, import_verified=0
            WHERE music_persistent_id IN
            (SELECT persistent_id FROM music_tracks WHERE location IS NULL OR location='')"""
        )
        c.execute(
            """DELETE FROM sync_status WHERE stage='local_file_detected' AND track_id NOT IN
            (SELECT track_id FROM local_assets)"""
        )
        # Scan-derived files that Music.app also lists are verified imports.
        c.execute(
            """UPDATE local_assets SET music_persistent_id=(SELECT persistent_id FROM music_tracks
            m WHERE m.location=local_assets.path), import_verified=1
            WHERE music_persistent_id IS NULL AND EXISTS (SELECT 1 FROM music_tracks m
            WHERE m.location=local_assets.path)"""
        )
        count = 0
        for track_id, identity_json in c.execute(
            "SELECT t.id, t.identity_json FROM tracks t JOIN service_tracks s ON s.track_id=t.id "
            "AND s.service='spotify'"
        ).fetchall():
            raw = json.loads(identity_json)
            source = Track(**{**raw, "artists": tuple(raw["artists"])})
            source = Track(
                source.service,
                source.id,
                source.title,
                source.artists,
                source.duration_ms,
                None,
                source.version,
                source.available,
                source.explicit,
            )
            for path, candidate in by_title.get(identity(source)[0], []):
                result = score(source, candidate)
                if result.eligible and result.score >= 0.89:
                    c.execute(
                        """INSERT INTO local_assets(track_id,path,source,music_persistent_id,
                        import_verified) VALUES(?,?,'music',?,1)
                        ON CONFLICT(track_id,path) DO UPDATE SET
                        music_persistent_id=excluded.music_persistent_id,import_verified=1""",
                        (track_id, path, candidate.id),
                    )
                    count += 1
        c.execute(
            """INSERT INTO sync_status SELECT DISTINCT track_id,'local_file_detected','found',?,1
            FROM local_assets WHERE music_persistent_id IS NOT NULL
            ON CONFLICT(track_id,stage) DO UPDATE SET status='found',updated=excluded.updated""",
            (now,),
        )
    return count


def confirmed_folders(ledger: Ledger) -> dict[str, str]:
    rows = ledger.connection.execute(
        "SELECT group_name, persistent_id FROM music_folders"
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def build_report(ledger: Ledger, mappings: dict[str, str]) -> dict[str, Any]:
    """Zero-write reconciliation of mapped Spotify playlists against Music.app."""
    folders = confirmed_folders(ledger)
    missing = sorted({g for g in mappings.values() if g not in folders})
    if missing:
        raise SyncError(
            "Managed Music.app folders are not confirmed yet. Run "
            "`dj-sync music index --confirm-folders` first."
        )
    c = ledger.connection
    playlists = [
        {
            "id": r[0],
            "name": r[1],
            "kind": r[2],
            "parent": r[3],
            "cls": "folderPlaylist" if r[2] == "folder" else "userPlaylist",
        }
        for r in c.execute("SELECT persistent_id,name,kind,parent_id FROM music_playlists")
    ]
    under: dict[str, list[dict[str, Any]]] = {
        g: [p for p in descendants(playlists, fid) if not is_folder(p)]
        for g, fid in folders.items()
    }
    track_names = {
        r[0]: f"{r[2]} — {r[1]}" if r[2] else r[1]
        for r in c.execute("SELECT persistent_id,name,artist FROM music_tracks")
    }
    entries: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    for playlist_id, group in mappings.items():
        latest = ledger.latest_complete_report(playlist_id)
        if not latest:
            continue
        report = latest[2]
        name = report["playlist"]["name"]
        stored = c.execute(
            "SELECT music_persistent_id FROM spotify_playlists WHERE id=?", (playlist_id,)
        ).fetchone()
        allowed = {p["id"]: p for p in under[group]}
        destination: dict[str, Any]
        if stored and stored[0] and stored[0] in allowed:
            destination = {"id": stored[0], "name": allowed[stored[0]]["name"], "how": "stored"}
        else:
            hits = [p for p in allowed.values() if p["name"] == name]
            if len(hits) == 1:
                destination = {"id": hits[0]["id"], "name": name, "how": "name"}
            elif hits:
                destination = {"id": None, "name": name, "how": "ambiguous"}
            else:
                destination = {"id": None, "name": name, "how": "create"}
        members = [
            r[0]
            for r in c.execute(
                "SELECT track_id FROM music_memberships WHERE playlist_id=? ORDER BY position",
                (destination["id"] or "",),
            )
        ]
        member_set = set(members)
        rows_out: list[dict[str, Any]] = []
        used: set[str] = set()
        for row in report["rows"]:
            src = row["source"]
            if not src:
                continue
            track_row = c.execute(
                "SELECT track_id FROM service_tracks WHERE service='spotify' AND service_id=?",
                (src["id"],),
            ).fetchone()
            track_id = track_row[0] if track_row else None
            music_ids = [
                r[0]
                for r in c.execute(
                    "SELECT music_persistent_id FROM local_assets WHERE track_id=? AND "
                    "music_persistent_id IS NOT NULL ORDER BY path",
                    (track_id,),
                )
            ]
            converted = c.execute(
                "SELECT aiff_path FROM downloads WHERE track_id=? AND status='converted'",
                (track_id,),
            ).fetchone()
            on_disk = c.execute(
                "SELECT path FROM local_assets WHERE track_id=? AND music_persistent_id IS NULL "
                "ORDER BY path LIMIT 1",
                (track_id,),
            ).fetchone()
            present = [m for m in music_ids if m in member_set]
            if present:
                action = "in_playlist"
                used.update(present)
            elif music_ids:
                action = "add_existing"
            elif converted:
                action = "import_pending"
            elif on_disk:
                action = "import_from_disk"
            elif row["match"]["status"] == "matched":
                action = "download_pending"
            elif row["match"]["status"] in ("local", "skipped"):
                action = row["match"]["status"]
            else:
                action = "unresolved"
            totals[action] = totals.get(action, 0) + 1
            rows_out.append(
                {
                    "spotify_id": src["id"],
                    "title": src["title"],
                    "artists": list(src["artists"]),
                    "action": action,
                    "music_track_id": present[0]
                    if present
                    else (music_ids[0] if music_ids else None),
                    "staged_aiff": converted[0] if converted else None,
                    "disk_file": on_disk[0] if on_disk else None,
                }
            )
        entries.append(
            {
                "spotify_id": playlist_id,
                "group": group,
                "name": name,
                "destination": destination,
                "tracks": rows_out,
                "local_only": [m for m in members if m not in used],
                "local_only_names": [track_names.get(m, m) for m in members if m not in used],
            }
        )
    return {
        "generated_at": time.time(),
        "folders": folders,
        "playlists": entries,
        "totals": totals,
        "writes_performed": 0,
    }
