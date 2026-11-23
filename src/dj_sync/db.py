"""Transactional recording ledger. Sample observations never replace full membership."""

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import DESTINATIONS
from .models import Match, Snapshot, Track
from .storage import private_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, artists_json TEXT NOT NULL,
 duration_ms INTEGER, identity_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_tracks (
 service TEXT NOT NULL, service_id TEXT NOT NULL, track_id INTEGER REFERENCES tracks(id),
 metadata_json TEXT NOT NULL, PRIMARY KEY(service, service_id)
);
CREATE TABLE IF NOT EXISTS spotify_playlists (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, group_name TEXT,
 destination_folder TEXT, music_persistent_id TEXT, tidal_playlist_id TEXT,
 snapshot_id TEXT, complete_snapshot_id TEXT
);
CREATE TABLE IF NOT EXISTS observed_memberships (
 playlist_id TEXT REFERENCES spotify_playlists(id), position INTEGER NOT NULL,
 spotify_id TEXT, issue TEXT, PRIMARY KEY(playlist_id, position)
);
CREATE TABLE IF NOT EXISTS desired_memberships (
 playlist_id TEXT REFERENCES spotify_playlists(id), position INTEGER NOT NULL,
 spotify_id TEXT, issue TEXT, PRIMARY KEY(playlist_id, position)
);
CREATE TABLE IF NOT EXISTS local_assets (
 id INTEGER PRIMARY KEY, track_id INTEGER REFERENCES tracks(id), path TEXT NOT NULL,
 source TEXT NOT NULL DEFAULT 'scan', fingerprint TEXT, music_persistent_id TEXT,
 import_verified INTEGER NOT NULL DEFAULT 0, UNIQUE(track_id, path)
);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, started REAL NOT NULL, finished REAL, status TEXT NOT NULL,
 playlist_id TEXT, report_json TEXT
);
CREATE TABLE IF NOT EXISTS sync_status (
 track_id INTEGER REFERENCES tracks(id), stage TEXT NOT NULL, status TEXT NOT NULL,
 updated REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(track_id, stage)
);
CREATE TABLE IF NOT EXISTS sync_events (
 id INTEGER PRIMARY KEY, run_id TEXT REFERENCES runs(id), track_id INTEGER REFERENCES tracks(id),
 stage TEXT NOT NULL, status TEXT NOT NULL, occurred REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
 track_id INTEGER REFERENCES tracks(id), country TEXT NOT NULL, cache_key TEXT NOT NULL,
 result_json TEXT NOT NULL, updated REAL NOT NULL, tidal_id TEXT,
 PRIMARY KEY(track_id, country)
);
CREATE TABLE IF NOT EXISTS match_reviews (
 track_id INTEGER REFERENCES tracks(id), country TEXT NOT NULL, status TEXT NOT NULL,
 evidence_json TEXT NOT NULL, PRIMARY KEY(track_id, country)
);
CREATE TABLE IF NOT EXISTS tidal_test_memberships (
 playlist_id TEXT NOT NULL, tidal_id TEXT NOT NULL, verified_at REAL NOT NULL,
 PRIMARY KEY(playlist_id,tidal_id)
);
CREATE TABLE IF NOT EXISTS tidal_test_runs (
 id TEXT PRIMARY KEY, source_run_id TEXT REFERENCES runs(id), result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tidal_memberships (
 tidal_playlist_id TEXT NOT NULL, tidal_id TEXT NOT NULL, occurrences INTEGER NOT NULL,
 verified_at REAL NOT NULL, PRIMARY KEY(tidal_playlist_id, tidal_id)
);
CREATE TABLE IF NOT EXISTS manual_matches (
 spotify_id TEXT NOT NULL, country TEXT NOT NULL, decision TEXT NOT NULL,
 tidal_id TEXT, decided_at REAL NOT NULL, PRIMARY KEY(spotify_id, country)
);
CREATE TABLE IF NOT EXISTS local_files (
 path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL, title TEXT NOT NULL,
 artists_json TEXT NOT NULL, album TEXT NOT NULL, duration_ms INTEGER, isrc TEXT,
 metadata_source TEXT NOT NULL, indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS downloads (
 track_id INTEGER REFERENCES tracks(id), tidal_id TEXT NOT NULL, run_id TEXT,
 status TEXT NOT NULL, origin TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 flac_path TEXT, aiff_path TEXT, verified_at REAL, PRIMARY KEY(track_id, tidal_id)
);
CREATE TABLE IF NOT EXISTS music_folders (
 group_name TEXT PRIMARY KEY, persistent_id TEXT NOT NULL, name TEXT NOT NULL,
 confirmed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS music_playlists (
 persistent_id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, parent_id TEXT,
 indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS music_tracks (
 persistent_id TEXT PRIMARY KEY, name TEXT NOT NULL, artist TEXT NOT NULL,
 duration_ms INTEGER, location TEXT, indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS music_memberships (
 playlist_id TEXT NOT NULL, track_id TEXT NOT NULL, position INTEGER NOT NULL,
 PRIMARY KEY(playlist_id, track_id, position)
);
CREATE TABLE IF NOT EXISTS music_artwork (
 persistent_id TEXT PRIMARY KEY, handed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_features (
 music_id TEXT PRIMARY KEY, location TEXT NOT NULL, features_json TEXT NOT NULL,
 analysed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_choices (
 music_id TEXT PRIMARY KEY, stars INTEGER NOT NULL, home_playlist TEXT, decided_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_writes (
 music_id TEXT PRIMARY KEY, stars INTEGER NOT NULL, written_at REAL NOT NULL
);
PRAGMA user_version = 6;
"""

MIGRATE_V6 = """
ALTER TABLE local_assets RENAME TO local_assets_v5;
CREATE TABLE local_assets (
 id INTEGER PRIMARY KEY, track_id INTEGER REFERENCES tracks(id), path TEXT NOT NULL,
 source TEXT NOT NULL DEFAULT 'scan', fingerprint TEXT, music_persistent_id TEXT,
 import_verified INTEGER NOT NULL DEFAULT 0, UNIQUE(track_id, path)
);
INSERT INTO local_assets(id,track_id,path,source,fingerprint,music_persistent_id,import_verified)
 SELECT id,track_id,path,'scan',fingerprint,music_persistent_id,import_verified
 FROM local_assets_v5;
DROP TABLE local_assets_v5;
"""

MATCHED_TTL = 30 * 86400
REVIEW_TTL = 86400


def cache_key(track: Track, country: str) -> str:
    payload = json.dumps(
        {"source": asdict(track), "country": country, "matcher_version": 2}, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path) -> None:
        private_dir(path.parent)
        if path.is_symlink():
            raise ValueError("Ledger must not be a symlink")
        self.path = path
        self.connection = sqlite3.connect(path, timeout=10)
        path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6):
            raise ValueError("Unsupported ledger version")
        if 1 <= version <= 5:
            # Older files already hold local_assets with the v5 shape; CREATE IF NOT EXISTS
            # would leave it untouched, so rebuild it explicitly (rows are preserved).
            self.connection.executescript("BEGIN;" + MIGRATE_V6 + "COMMIT;")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def record_test_verification(self, result: dict[str, Any]) -> None:
        """Only called after remote readback confirms the exact intended membership."""
        with self.connection:
            self.connection.executemany(
                """INSERT INTO tidal_test_memberships VALUES(?,?,?)
                ON CONFLICT(playlist_id,tidal_id) DO UPDATE SET verified_at=excluded.verified_at""",
                [(result["playlist_id"], t, result["verified_at"]) for t in result["verified_ids"]],
            )
            self.connection.execute(
                "INSERT INTO tidal_test_runs VALUES(?,?,?)",
                (
                    str(uuid.uuid4()),
                    result["report_run_id"],
                    json.dumps(result),
                ),
            )

    def record_tidal_playlist(self, plan: dict[str, Any], result: dict[str, Any]) -> None:
        """Only called after readback; mirrors verified remote membership, not intentions."""
        playlist = result["playlist_id"]
        counts: dict[str, int] = {}
        for tidal_id in result["verified_ids"]:
            counts[tidal_id] = counts.get(tidal_id, 0) + 1
        with self.connection:
            self.connection.execute(
                """INSERT INTO spotify_playlists(id,name,group_name,destination_folder,
                tidal_playlist_id) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE
                SET tidal_playlist_id=excluded.tidal_playlist_id""",
                (
                    plan["source_id"],
                    plan["source_name"],
                    plan["group"],
                    DESTINATIONS.get(plan["group"]),
                    playlist,
                ),
            )
            self.connection.execute(
                "DELETE FROM tidal_memberships WHERE tidal_playlist_id=?", (playlist,)
            )
            self.connection.executemany(
                "INSERT INTO tidal_memberships VALUES(?,?,?,?)",
                [(playlist, t, n, result["verified_at"]) for t, n in counts.items()],
            )
            # Track-level checkpoint: the recording is in at least one verified TIDAL playlist.
            if not counts:
                return
            self.connection.execute(
                f"""INSERT INTO sync_status
                SELECT DISTINCT m.track_id,'tidal_playlist_added','verified',?,1
                FROM matches m WHERE m.tidal_id IN ({",".join("?" * len(counts))})
                ON CONFLICT(track_id,stage) DO UPDATE SET status='verified',
                updated=excluded.updated""",
                (result["verified_at"], *counts),
            )

    def begin(self, playlist_id: str) -> str:
        run_id = str(uuid.uuid4())
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status='interrupted', finished=? WHERE status='running'",
                (time.time(),),
            )
            self.connection.execute(
                "INSERT INTO runs(id, started, status, playlist_id) VALUES(?,?,'running',?)",
                (run_id, time.time(), playlist_id),
            )
        return run_id

    def finish(self, run_id: str, status: str, report: dict[str, Any] | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status=?, finished=?, report_json=? WHERE id=?",
                (status, time.time(), json.dumps(report) if report else None, run_id),
            )

    def discover_playlists(self, playlists: list[dict[str, Any]], groups: dict[str, str]) -> None:
        # Absence from discovery is not proof of deletion; never delete ledger playlists here.
        with self.connection:
            for playlist in playlists:
                group = groups.get(playlist["id"])
                self.connection.execute(
                    """INSERT INTO spotify_playlists(id,name,group_name,destination_folder)
                    VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                    group_name=excluded.group_name,destination_folder=excluded.destination_folder""",
                    (playlist["id"], playlist["name"], group, DESTINATIONS.get(group or "")),
                )

    def _track(self, track: Track) -> int:
        row = self.connection.execute(
            "SELECT track_id FROM service_tracks WHERE service='spotify' AND service_id=?",
            (track.id,),
        ).fetchone()
        identity = json.dumps(asdict(track))
        if row:
            track_id = int(row[0])
            self.connection.execute(
                "UPDATE tracks SET title=?,artists_json=?,duration_ms=?,identity_json=? WHERE id=?",
                (track.title, json.dumps(track.artists), track.duration_ms, identity, track_id),
            )
        else:
            cursor = self.connection.execute(
                "INSERT INTO tracks(title,artists_json,duration_ms,identity_json) VALUES(?,?,?,?)",
                (track.title, json.dumps(track.artists), track.duration_ms, identity),
            )
            track_id = int(cursor.lastrowid or 0)
        self.connection.execute(
            """INSERT INTO service_tracks VALUES('spotify',?,?,?)
            ON CONFLICT(service,service_id) DO UPDATE
                SET metadata_json=excluded.metadata_json""",
            (track.id, track_id, identity),
        )
        return track_id

    def snapshot(
        self, snapshot: Snapshot, group: str | None
    ) -> tuple[dict[str, Any], dict[str, int]]:
        c = self.connection
        with c:
            previous = c.execute(
                "SELECT * FROM spotify_playlists WHERE id=?", (snapshot.id,)
            ).fetchone()
            old = [
                r[0]
                for r in c.execute(
                    "SELECT spotify_id FROM desired_memberships "
                    "WHERE playlist_id=? ORDER BY position",
                    (snapshot.id,),
                )
            ]
            c.execute(
                """INSERT INTO spotify_playlists(id,name,group_name,destination_folder,snapshot_id)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                group_name=excluded.group_name,destination_folder=excluded.destination_folder,
                snapshot_id=excluded.snapshot_id""",
                (
                    snapshot.id,
                    snapshot.name,
                    group,
                    DESTINATIONS.get(group or ""),
                    snapshot.snapshot_id,
                ),
            )
            track_ids = {i.track.id: self._track(i.track) for i in snapshot.items if i.track}
            c.execute("DELETE FROM observed_memberships WHERE playlist_id=?", (snapshot.id,))
            c.executemany(
                "INSERT INTO observed_memberships VALUES(?,?,?,?)",
                [
                    (snapshot.id, i.position, i.track.id if i.track else None, i.issue)
                    for i in snapshot.items
                ],
            )
            new = [i.track.id if i.track else None for i in snapshot.items]
            had_complete = bool(previous and previous["complete_snapshot_id"])
            # Unavailable/null entries make absence unsafe even in a fully paginated response.
            comparable = snapshot.complete and had_complete and None not in old + new
            diff = {
                "renamed_from": previous["name"]
                if previous and previous["name"] != snapshot.name
                else None,
                "added_ids": sorted(x for x in set(new) - set(old) if x is not None),
                "removed_ids": sorted(set(old) - set(new)) if comparable else [],
                "order_changed": comparable and set(old) == set(new) and old != new,
                "removals_evaluated": comparable,
                "scope": "complete" if snapshot.complete else "first_items_sample",
                "baseline_exists": had_complete,
            }
            if snapshot.complete:
                c.execute("DELETE FROM desired_memberships WHERE playlist_id=?", (snapshot.id,))
                c.execute(
                    "INSERT INTO desired_memberships SELECT * FROM observed_memberships "
                    "WHERE playlist_id=?",
                    (snapshot.id,),
                )
                c.execute(
                    "UPDATE spotify_playlists SET complete_snapshot_id=? WHERE id=?",
                    (snapshot.snapshot_id, snapshot.id),
                )
        return diff, track_ids

    def manual(self, spotify_id: str, country: str) -> tuple[str, str | None] | None:
        row = self.connection.execute(
            "SELECT decision,tidal_id FROM manual_matches WHERE spotify_id=? AND country=?",
            (spotify_id, country),
        ).fetchone()
        return (str(row[0]), row[1]) if row else None

    def latest_complete_report(self, playlist_id: str) -> tuple[str, float, dict[str, Any]] | None:
        for row in self.connection.execute(
            "SELECT id,started,report_json FROM runs WHERE playlist_id=? AND report_json "
            "IS NOT NULL AND status IN ('completed','completed_with_errors') "
            "ORDER BY started DESC",
            (playlist_id,),
        ):
            report = json.loads(row[2])
            if report.get("playlist", {}).get("complete"):
                return str(row[0]), float(row[1]), report
        return None

    def tidal_playlist(self, playlist_id: str) -> str | None:
        """A linked TIDAL playlist ID, if one has ever been recorded. None today."""
        row = self.connection.execute(
            "SELECT tidal_playlist_id FROM spotify_playlists WHERE id=?", (playlist_id,)
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def cached(self, track_id: int, key: str, country: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT result_json,updated FROM matches "
            "WHERE track_id=? AND country=? AND cache_key=?",
            (track_id, country, key),
        ).fetchone()
        if not row:
            return None
        result = dict(json.loads(row[0]))
        # A confident match keyed to unchanged source metadata and matcher version is
        # reusable for longer; reviewable outcomes are retried sooner in case the catalog changed.
        ttl = MATCHED_TTL if result.get("status") == "matched" else REVIEW_TTL
        return result if row[1] > time.time() - ttl else None

    def checkpoint(
        self, run_id: str, track_id: int, country: str, key: str, match: Match
    ) -> dict[str, Any]:
        result = asdict(match)
        for candidate in result["candidates"]:
            candidate["url"] = (
                f"https://tidal.com/browse/track/{quote(candidate['track']['id'], safe='')}"
            )
        with self.connection:
            for candidate in match.candidates:
                self.connection.execute(
                    """INSERT INTO service_tracks VALUES('tidal',?,NULL,?)
                    ON CONFLICT(service,service_id) DO UPDATE
                SET metadata_json=excluded.metadata_json""",
                    (candidate.track.id, json.dumps(asdict(candidate.track))),
                )
            self.connection.execute(
                """INSERT INTO matches VALUES(?,?,?,?,?,?) ON CONFLICT(track_id,country)
                DO UPDATE SET cache_key=excluded.cache_key,result_json=excluded.result_json,
                updated=excluded.updated,tidal_id=excluded.tidal_id""",
                (track_id, country, key, json.dumps(result), time.time(), match.selected_id),
            )
            if match.status != "matched":
                self.connection.execute(
                    """INSERT INTO match_reviews VALUES(?,?,?,?) ON CONFLICT(track_id,country)
                    DO UPDATE SET status=excluded.status,evidence_json=excluded.evidence_json""",
                    (track_id, country, match.status, json.dumps(result)),
                )
            else:
                self.connection.execute(
                    "DELETE FROM match_reviews WHERE track_id=? AND country=?", (track_id, country)
                )
            self._event(run_id, track_id, match.status)
        return result

    def _event(self, run_id: str, track_id: int, status: str) -> None:
        self.connection.execute(
            """INSERT INTO sync_status VALUES(?,'catalog_match',?,?,1)
            ON CONFLICT(track_id,stage) DO UPDATE SET status=excluded.status,
            updated=excluded.updated,attempts=sync_status.attempts+1""",
            (track_id, status, time.time()),
        )
        self.connection.execute(
            "INSERT INTO sync_events(run_id,track_id,stage,status,occurred) "
            "VALUES(?,?,'catalog_match',?,?)",
            (run_id, track_id, status, time.time()),
        )

    def error(self, run_id: str, track_id: int, country: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM matches WHERE track_id=? AND country=?", (track_id, country)
            )
            self.connection.execute(
                """INSERT INTO match_reviews VALUES(?,?,'error','{}')
                ON CONFLICT(track_id,country) DO UPDATE SET status='error',evidence_json='{}'""",
                (track_id, country),
            )
            self._event(run_id, track_id, "error")
