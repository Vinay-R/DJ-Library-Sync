"""Additive TIDAL playlists mirroring mapped Spotify playlists.

One new UNLISTED TIDAL playlist per mapped Spotify playlist, created once and then
only appended to. It reuses the test writer's guarded API: durable intent before
any POST, owner/marker/visibility checks before additions, Idempotency-Key on
every POST, server-side duplicate skipping and full readback before success.

Never removes, reorders, renames or deletes. Tracks in TIDAL that Spotify no
longer lists are reported for review. Never adopts a playlist it did not create.
"""

import hashlib
import json
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .db import Ledger
from .errors import ApiError, SyncError
from .models import SETTLED_STATUSES
from .planner import plan_additions
from .storage import atomic_text, save_json
from .tidal_test_playlist import TestPlaylistAPI

STATE_DIR = "tidal-playlists"
MAX_REPORT_AGE = 86400
# TIDAL documents a one-hour idempotency window; stop replaying a lost create before it.
CREATE_REPLAY_WINDOW = 3000


def tidal_name(group: str, spotify_name: str) -> str:
    # Collapse whitespace so a server-side trim can never break the exact-name guard.
    return " ".join(f"{group} / {spotify_name}".split())[:250].rstrip()


def load_mapped_plans(
    ledger: Ledger, mappings: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Plans from each mapped playlist's newest complete, fresh, read-only report."""
    plans: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for playlist_id, group in mappings.items():
        report: dict[str, Any] | None = None
        run_id = ""
        started = 0.0
        for row in ledger.connection.execute(
            "SELECT id,started,report_json FROM runs WHERE playlist_id=? AND report_json "
            "IS NOT NULL AND status IN ('completed','completed_with_errors') "
            "ORDER BY started DESC",
            (playlist_id,),
        ):
            candidate = json.loads(row[2])
            if candidate.get("playlist", {}).get("complete"):
                report, run_id, started = candidate, str(row[0]), float(row[1])
                break
        reason = None
        if report is None:
            reason = "No complete read-only sync report. Run `dj-sync sync --mapped` first."
        elif time.time() - started > MAX_REPORT_AGE:
            reason = "Newest complete report is older than 24 hours; run sync again."
        elif report["playlist"].get("group") != group:
            reason = "Group mapping changed since the last sync; run sync again."
        if report is None or reason:
            skipped.append({"id": playlist_id, "group": group, "reason": reason})
            continue
        matched = {
            r["match"]["selected_id"]
            for r in report["rows"]
            if r["match"]["status"] == "matched" and r["match"].get("selected_id")
        }
        track_ids = list(report["plan"]["proposed_track_ids"])
        if not set(track_ids) <= matched:
            raise SyncError("A report plan lists IDs without a confident match; refusing.")
        plans.append(
            {
                "source_id": playlist_id,
                "source_name": report["playlist"]["name"],
                "group": group,
                "snapshot_id": report["playlist"]["snapshot_id"],
                "report_run_id": run_id,
                "name": tidal_name(group, report["playlist"]["name"]),
                "track_ids": plan_additions(track_ids, []),
                "unresolved": sum(
                    1 for r in report["rows"] if r["match"]["status"] not in SETTLED_STATUSES
                ),
            }
        )
    return plans, skipped


def _state_path(directory: Path, source_id: str) -> Path:
    return directory / STATE_DIR / (hashlib.sha256(source_id.encode()).hexdigest() + ".json")


def apply_mapped_playlist(
    api: TestPlaylistAPI, plan: dict[str, Any], directory: Path
) -> dict[str, Any]:
    """Create (once) and append missing tracks. Dry run unless ``api.apply``."""
    state_path = _state_path(directory, plan["source_id"])
    posts_at_start = api.posts
    user_id = api.user_id()
    intent: dict[str, Any] | None = None
    if state_path.exists():
        intent = json.loads(state_path.read_text())
        if intent["user_id"] != user_id or intent["source_id"] != plan["source_id"]:
            raise SyncError(
                "Saved TIDAL playlist intent belongs to a different account or source; "
                "manual review required."
            )
    desired = plan["track_ids"]
    rename = plan["name"] if intent and intent["name"] != plan["name"] else None

    if not api.apply:
        if intent is None:
            return {
                "mode": "dry_run",
                "action": "create",
                "name": plan["name"],
                "would_add": len(desired),
            }
        if not intent["remote_id"]:
            return {
                "mode": "dry_run",
                "action": "recover_uncertain_create",
                "name": intent["name"],
                "would_add": len(desired),
            }
        api.verify_target(intent)
        before = api.members(intent["remote_id"])
        return {
            "mode": "dry_run",
            "action": "update",
            "name": intent["name"],
            "playlist_id": intent["remote_id"],
            "would_add": len(plan_additions(desired, before)),
            "not_in_spotify": sorted(set(before) - set(desired)),
            "rename_pending": rename,
        }

    fresh = intent is None
    if intent is None:
        key = str(uuid.uuid4())
        intent = {
            "source_id": plan["source_id"],
            "user_id": user_id,
            "name": plan["name"],
            "description": (
                f"Managed by DJ Library Sync from Spotify playlist {plan['source_id']}. "
                f"Intent: {key}"
            ),
            "create_key": key,
            "create_started": time.time(),
            "remote_id": None,
            "events": [],
        }
        save_json(state_path, intent)
    if not intent["remote_id"]:
        recovered = None if fresh else api.find_intent(intent)
        if recovered:
            intent["remote_id"] = recovered
        else:
            if time.time() - intent["create_started"] > CREATE_REPLAY_WINDOW:
                raise SyncError(
                    "Uncertain playlist creation is outside the replay window; "
                    "manual review required."
                )
            intent["remote_id"] = api.create(intent)
        save_json(state_path, intent)

    playlist_id = str(intent["remote_id"])
    attrs = api.verify_target(intent)
    before = api.members(playlist_id)
    if attrs.get("numberOfItems") is not None and attrs["numberOfItems"] != len(before):
        raise SyncError("Playlist size changed during inspection; rerun to reconcile.")
    missing = plan_additions(desired, before)
    for start in range(0, len(missing), api.max_batch):
        batch = missing[start : start + api.max_batch]
        key = str(uuid.uuid4())
        event: dict[str, Any] = {
            "idempotency_key": key,
            "ids": batch,
            "status": "pending",
            "started": time.time(),
        }
        intent["events"].append(event)
        save_json(state_path, intent)
        api.add(playlist_id, batch, key)
        event["status"] = "sent"
        save_json(state_path, intent)

    api.verify_target(intent)
    after = api.members(playlist_id)
    expected = Counter(before)
    expected.update(missing)
    if Counter(after) != expected:
        raise SyncError(
            "TIDAL readback did not confirm exactly the expected additions; rerun to reconcile."
        )
    for event in intent["events"]:
        if event["status"] != "verified_by_readback" and set(event["ids"]) <= set(after):
            event["status"] = "verified_by_readback"
    result = {
        "mode": "applied",
        "source_id": plan["source_id"],
        "name": intent["name"],
        "playlist_id": playlist_id,
        "url": f"https://tidal.com/browse/playlist/{quote(playlist_id, safe='')}",
        "visibility": "UNLISTED",
        "report_run_id": plan.get("report_run_id"),
        "before_count": len(before),
        "after_count": len(after),
        "added_count": len(missing),
        "post_requests": api.posts - posts_at_start,
        "verified_ids": after,
        "not_in_spotify": sorted(set(after) - set(desired)),
        "preexisting_duplicates": sorted(t for t, n in Counter(before).items() if n > 1),
        "unresolved_in_spotify": plan.get("unresolved", 0),
        "rename_pending": rename,
        "verified_at": time.time(),
    }
    summary = {k: v for k, v in result.items() if k != "verified_ids"}
    intent.update(status="verified", last_result=summary)
    save_json(state_path, intent)
    return result


def sync_mapped_playlists(
    api: TestPlaylistAPI, plans: list[dict[str, Any]], directory: Path, ledger: Ledger
) -> dict[str, Any]:
    """Apply (or preview) every plan. Authorization or rate-limit errors stop the run."""
    summary: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "mode": "applied" if api.apply else "dry_run",
        "playlists": [],
        "post_requests": 0,
    }
    try:
        for plan in plans:
            entry: dict[str, Any] = {
                "source_id": plan["source_id"],
                "name": plan["name"],
                "group": plan["group"],
            }
            summary["playlists"].append(entry)
            if not plan["track_ids"]:
                entry.update(status="skipped", reason="No confident matches to add yet.")
                continue
            posts_before = api.posts
            try:
                result = apply_mapped_playlist(api, plan, directory)
            except ApiError as exc:
                entry.update(status="stopped", error=str(exc))
                if exc.status in {401, 403, 429}:
                    raise
                continue
            except SyncError as exc:
                entry.update(status="failed", error=str(exc))
                continue
            finally:
                summary["post_requests"] = api.posts
            entry["post_requests"] = api.posts - posts_before
            if result["mode"] == "applied":
                ledger.record_tidal_playlist(plan, result)
                entry.update(
                    status="verified",
                    **{
                        k: result[k]
                        for k in (
                            "playlist_id",
                            "url",
                            "before_count",
                            "after_count",
                            "added_count",
                            "not_in_spotify",
                            "unresolved_in_spotify",
                            "rename_pending",
                        )
                    },
                )
            else:
                entry.update(status="preview", **{k: v for k, v in result.items() if k != "mode"})
    finally:
        write_tidal_summary(directory / "reports", summary)
    return summary


def write_tidal_summary(directory: Path, summary: dict[str, Any]) -> Path:
    stem = directory / f"tidal-sync-{summary['id']}"
    save_json(stem.with_suffix(".json"), summary)
    lines = [
        f"# TIDAL mapped playlist sync ({summary['mode']})",
        "",
        f"POST requests: {summary['post_requests']}. Additions only; nothing removed, "
        "renamed or reordered.",
        "",
        "| TIDAL playlist | Status | Before | Added / would add | After | Not in Spotify |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for e in summary["playlists"]:
        name = e["name"].replace("|", "\\|")
        if e.get("url"):
            name = f"[{name}]({e['url']})"
        lines.append(
            f"| {name} | {e['status']} | {e.get('before_count', '—')} | "
            f"{e.get('added_count', e.get('would_add', '—'))} | {e.get('after_count', '—')} | "
            f"{len(e.get('not_in_spotify', []))} |"
        )
    notes = [e for e in summary["playlists"] if e.get("error") or e.get("reason")]
    if notes:
        lines.extend(["", "## Not applied", ""])
        lines.extend(f"- {e['name']}: {e.get('error') or e.get('reason')}" for e in notes)
    lines.append("")
    atomic_text(stem.with_suffix(".md"), "\n".join(lines))
    return stem.with_suffix(".md")
