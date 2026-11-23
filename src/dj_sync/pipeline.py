import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from .db import Ledger, cache_key
from .errors import ApiError, SyncError
from .models import Match, Snapshot, Track
from .planner import mapped_playlist_plan, plan_additions, test_playlist_plan
from .reports import write_report, write_summary
from .track_matcher import match_track


class SpotifySource(Protocol):
    def sample(self, playlist_id: str, limit: int = 10) -> Snapshot: ...
    def full(self, playlist_id: str) -> Snapshot: ...


class Catalog(Protocol):
    def candidates(self, source: Track) -> list[Track]: ...


def run_playlist(
    spotify: SpotifySource,
    tidal: Catalog,
    ledger: Ledger,
    playlist_id: str,
    country: str,
    group: str | None = None,
    limit: int | None = None,
    refresh: bool = False,
    retry_review: bool = False,
) -> dict[str, Any]:
    """Read and match one playlist. ``limit=None`` reads a complete snapshot.

    ``retry_review`` ignores cached results that are not confident matches.
    """
    run_id = ledger.begin(playlist_id)
    try:
        snapshot = (
            spotify.full(playlist_id) if limit is None else spotify.sample(playlist_id, limit)
        )
        diff, track_ids = ledger.snapshot(snapshot, group)
        rows: list[dict[str, Any]] = []
        blocked: str | None = None
        for item in snapshot.items:
            source = item.track
            cached = False
            result: dict[str, Any] | None
            if source is None:
                result = asdict(Match("unavailable", reason=item.issue or "Missing item"))
            else:
                track_id = track_ids[source.id]
                key = cache_key(source, country)
                manual = ledger.manual(source.id, country)
                result = None if refresh else ledger.cached(track_id, key, country)
                if result and retry_review and result["status"] != "matched":
                    result = None
                if manual:
                    result = asdict(manual_match(*manual))
                cached = result is not None
                if result is None:
                    try:
                        if blocked:
                            raise SyncError(blocked)
                        candidates = tidal.candidates(source)
                        match = match_track(source, candidates)
                        fallback = getattr(tidal, "fallback_candidates", None)
                        if fallback and not any(c.eligible for c in match.candidates):
                            match = match_track(source, [*candidates, *fallback(source)])
                        result = ledger.checkpoint(run_id, track_id, country, key, match)
                    except SyncError as exc:
                        if isinstance(exc, ApiError) and exc.status in {401, 403, 429}:
                            blocked = str(exc) + " Remaining catalog requests deferred."
                        ledger.error(run_id, track_id, country)
                        result = asdict(Match("error", reason=str(exc)))
            rows.append(
                {
                    "position": item.position,
                    "source": asdict(source) if source else None,
                    "issue": item.issue,
                    "cached": cached,
                    "match": result,
                }
            )
        counts = dict(Counter(row["match"]["status"] for row in rows))
        report: dict[str, Any] = {
            "run_id": run_id,
            "country": country,
            "dry_run": True,
            "playlist": {
                "id": snapshot.id,
                "name": snapshot.name,
                "group": group,
                "snapshot_id": snapshot.snapshot_id,
                "complete": snapshot.complete,
            },
            "mapping_review_required": group is None,
            "diff": diff,
            "rows": rows,
            "counts": counts,
            "catalog_blocked": blocked,
            "status": "completed_with_errors" if counts.get("error") else "completed",
            "plan": test_playlist_plan(rows)
            if limit is not None
            else mapped_playlist_plan(rows, diff, ledger.tidal_playlist(snapshot.id)),
        }
        ledger.finish(run_id, report["status"], report)
        return report
    except BaseException:
        ledger.finish(run_id, "failed")
        raise


def manual_match(decision: str, tidal_id: str | None) -> Match:
    if decision == "match" and tidal_id:
        return Match("matched", selected_id=tidal_id, reason="Manual review: chosen by user")
    if decision == "local":
        return Match("local", reason="Manual review: already in Music.app")
    if decision == "skip":
        return Match("skipped", reason="Manual review: skipped by user")
    return Match("unmatched", reason="Manual review: user marked as not on TIDAL")


def run_poc(
    spotify: SpotifySource,
    tidal: Catalog,
    ledger: Ledger,
    playlist_id: str,
    country: str,
    group: str | None = None,
    limit: int = 10,
    refresh: bool = False,
) -> dict[str, Any]:
    """Phase 1 entry point: the first 1–10 positions only."""
    return run_playlist(spotify, tidal, ledger, playlist_id, country, group, limit, refresh)


def run_mapped(
    spotify: SpotifySource,
    tidal: Catalog,
    ledger: Ledger,
    mappings: dict[str, str],
    country: str,
    report_dir: Path,
    refresh: bool = False,
    retry_review: bool = False,
) -> dict[str, Any]:
    """Complete read-only snapshots and matching for every mapped playlist.

    An unreadable or concurrently edited playlist is recorded and skipped. A catalog
    authorization or rate-limit stop defers all remaining playlists. Spotify
    authentication or rate-limit failures stop the whole run.
    """
    if not mappings:
        raise SyncError(
            "No Spotify playlists are mapped to DJ or Vibes in config.yaml. Run "
            "`dj-sync playlists` and copy entries from the mapping worksheet."
        )
    entries: list[dict[str, Any]] = []
    memberships: dict[str, set[str]] = {}
    proposed: list[str] = []
    row_counts: Counter[str] = Counter()
    blocked = False
    for playlist_id, group in mappings.items():
        entry: dict[str, Any] = {"id": playlist_id, "group": group}
        entries.append(entry)
        if blocked:
            entry["status"] = "deferred"
            continue
        try:
            report = run_playlist(
                spotify, tidal, ledger, playlist_id, country, group, None, refresh, retry_review
            )
        except ApiError as exc:
            if exc.service == "spotify" and exc.status in {403, 404}:
                entry.update(status="unreadable", error=str(exc))
                continue
            raise
        except SyncError as exc:
            entry.update(status="failed", error=str(exc))
            continue
        path = write_report(report_dir, report)
        sources = {r["source"]["id"] for r in report["rows"] if r["source"]}
        for spotify_id in sources:
            memberships.setdefault(spotify_id, set()).add(playlist_id)
        proposed.extend(report["plan"]["proposed_track_ids"])
        row_counts.update(report["counts"])
        entry.update(
            status=report["status"],
            name=report["playlist"]["name"],
            run_id=report["run_id"],
            items=len(report["rows"]),
            counts=report["counts"],
            cached=sum(1 for r in report["rows"] if r["cached"]),
            added_since_last=len(report["diff"]["added_ids"]),
            removed_since_last=len(report["diff"]["removed_ids"]),
            renamed_from=report["diff"]["renamed_from"],
            proposed_tidal_ids=len(report["plan"]["proposed_track_ids"]),
            report=str(path),
        )
        if report["catalog_blocked"]:
            blocked = True
    summary: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "country": country,
        "dry_run": True,
        "writes_performed": 0,
        "playlists": entries,
        "totals": {
            "playlists": len(entries),
            "row_counts": dict(row_counts),
            "unique_spotify_tracks": len(memberships),
            "tracks_in_multiple_playlists": sum(1 for p in memberships.values() if len(p) > 1),
            "proposed_tidal_ids": len(plan_additions(proposed, [])),
        },
    }
    write_summary(report_dir, summary)
    return summary
