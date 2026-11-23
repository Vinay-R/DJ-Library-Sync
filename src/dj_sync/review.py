"""Human review of unresolved matches: export what needs a decision, import the answers.

Decisions are stored per Spotify track and market in ``manual_matches``. The next sync
uses them instead of searching: "match" selects the given TIDAL track ID, "no_match"
closes the item as not on TIDAL, "local" records that the user already has the file in
Music.app (nothing is added to TIDAL), "skip" leaves the track out of syncing, and
"clear" removes an earlier decision.
"""

import json
import re
import time
from collections import Counter
from typing import Any
from urllib.parse import quote

from .db import Ledger
from .errors import SyncError
from .models import simple_query

__all__ = ["import_decisions", "review_items", "simple_query"]

DECISIONS = {"match", "no_match", "local", "skip", "clear"}
MAX_CANDIDATES = 6
TIDAL_TRACK = re.compile(r"(?:tidal\.com/(?:browse/)?track/)?(\d{1,20})(?:[/?#].*)?")


def _track(t: dict[str, Any]) -> dict[str, Any]:
    service = t.get("service", "tidal")
    base = (
        "https://open.spotify.com/track/"
        if service == "spotify"
        else ("https://tidal.com/browse/track/")
    )
    return {
        "id": t["id"],
        "title": t["title"],
        "version": t.get("version", ""),
        "artists": list(t["artists"]),
        "album": t.get("album_title", ""),
        "released": t.get("album_release_date", ""),
        "duration_ms": t.get("duration_ms"),
        "isrc": t.get("isrc"),
        "explicit": t.get("explicit"),
        "url": base + quote(str(t["id"]), safe=""),
    }


def _local_file(ledger: Ledger, spotify_id: str) -> dict[str, Any] | None:
    """The file `dj-sync local match` found on disk for this track, if any."""
    row = ledger.connection.execute(
        """SELECT f.path, f.title, f.artists_json, f.duration_ms FROM service_tracks s
        JOIN local_assets a ON a.track_id = s.track_id JOIN local_files f ON f.path = a.path
        WHERE s.service='spotify' AND s.service_id=? ORDER BY f.path LIMIT 1""",
        (spotify_id,),
    ).fetchone()
    if not row:
        return None
    return {"path": row[0], "title": row[1], "artists": json.loads(row[2]), "duration_ms": row[3]}


def review_items(ledger: Ledger, mappings: dict[str, str]) -> dict[str, Any]:
    """Unresolved tracks (one entry per Spotify track) plus auto-resolved release choices."""
    items: dict[str, dict[str, Any]] = {}
    resolved: dict[str, dict[str, Any]] = {}
    for playlist_id, group in mappings.items():
        latest = ledger.latest_complete_report(playlist_id)
        if not latest:
            continue
        report = latest[2]
        label = f"{group} / {report['playlist']['name']}"
        for row in report["rows"]:
            source = row["source"]
            if not source:
                continue
            match = row["match"]
            sid = source["id"]
            if match["status"] == "matched":
                if match["reason"].startswith("Same recording"):
                    chosen = next(
                        c for c in match["candidates"] if c["track"]["id"] == match["selected_id"]
                    )
                    entry = resolved.setdefault(
                        sid,
                        {
                            "spotify": _track(source),
                            "tidal": _track(chosen["track"]),
                            "alternatives": sum(1 for c in match["candidates"] if c["eligible"]),
                            "playlists": [],
                        },
                    )
                    entry["playlists"].append(label)
                continue
            if ledger.manual(sid, report["country"]):
                continue
            entry = items.setdefault(
                sid,
                {
                    "spotify": {
                        **_track(source),
                        "unavailable_in_spotify": row.get("issue") == "unavailable_in_spotify",
                    },
                    "local": _local_file(ledger, sid),
                    "status": match["status"],
                    "reason": match["reason"],
                    "playlists": [],
                    "candidates": [
                        {
                            **_track(c["track"]),
                            "score": c["score"],
                            "eligible": c["eligible"],
                            "notes": [
                                r
                                for r in c["reasons"]
                                if not r.startswith(("title_similarity", "artist_similarity"))
                            ],
                        }
                        for c in match.get("candidates", [])[:MAX_CANDIDATES]
                    ],
                },
            )
            entry["playlists"].append(label)
    return {
        "generated_at": time.time(),
        "items": list(items.values()),
        "auto_resolved": list(resolved.values()),
    }


def import_decisions(
    ledger: Ledger, decisions: list[dict[str, Any]], country: str
) -> dict[str, int]:
    """Validate every decision first, then store them in one transaction."""
    rows: list[tuple[str, str, str | None]] = []
    for decision in decisions:
        sid = decision.get("spotify_id")
        kind = decision.get("decision")
        if not isinstance(sid, str) or kind not in DECISIONS:
            raise SyncError(
                "Each decision needs a spotify_id and one of: match, no_match, local, skip, clear."
            )
        known = ledger.connection.execute(
            "SELECT 1 FROM service_tracks WHERE service='spotify' AND service_id=?", (sid,)
        ).fetchone()
        if not known:
            raise SyncError(f"Unknown Spotify track in decisions: {sid}. Nothing imported.")
        tidal_id = None
        if kind == "match":
            found = TIDAL_TRACK.fullmatch(
                re.sub(r"^https?://(www\.|listen\.)?", "", str(decision.get("tidal_id", "")))
            )
            if not found:
                raise SyncError(f"Decision for {sid} needs a TIDAL track ID or link.")
            tidal_id = found[1]
        rows.append((sid, kind, tidal_id))
    with ledger.connection:
        for sid, kind, tidal_id in rows:
            if kind == "clear":
                ledger.connection.execute(
                    "DELETE FROM manual_matches WHERE spotify_id=? AND country=?", (sid, country)
                )
                continue
            ledger.connection.execute(
                """INSERT INTO manual_matches VALUES(?,?,?,?,?)
                ON CONFLICT(spotify_id,country) DO UPDATE SET decision=excluded.decision,
                tidal_id=excluded.tidal_id,decided_at=excluded.decided_at""",
                (sid, country, kind, tidal_id, time.time()),
            )
    return dict(Counter(kind for _, kind, _ in rows))
