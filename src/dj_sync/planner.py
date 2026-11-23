from collections.abc import Iterable
from typing import Any

from .models import SETTLED_STATUSES


def plan_additions(desired: Iterable[str], existing: Iterable[str]) -> list[str]:
    """Stable set difference. Never infer completion from an earlier proposed action."""
    present = set(existing)
    result = []
    for track_id in desired:
        if track_id not in present:
            result.append(track_id)
            present.add(track_id)
    return result


def test_playlist_plan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "mode": "dry_run",
        "target": "new_test_playlist_after_explicit_approval",
        "target_exists": False,
        "remote_state_verified": False,
        "proposed_track_ids": plan_additions(
            (r["match"]["selected_id"] for r in rows if r["match"]["status"] == "matched"), []
        ),
        "removals": [],
        "writes_performed": 0,
        "note": "Proposal only. Repeat dry runs repeat this plan until a real write is authorized "
        "and verified against the destination. "
        "The separate test-playlist command requires --apply.",
    }


def mapped_playlist_plan(
    rows: list[dict[str, Any]], diff: dict[str, Any], tidal_playlist_id: str | None
) -> dict[str, Any]:
    """Read-only proposal for the TIDAL playlist mirroring one mapped Spotify playlist.

    Without a verified destination readback, every confident match is listed as a
    proposed addition. Removals are never proposed automatically; Spotify removals
    observed between two complete snapshots are surfaced for review only.
    """
    return {
        "mode": "dry_run",
        "target": "tidal_playlist_for_mapped_source",
        "tidal_playlist_id": tidal_playlist_id,
        "target_exists": tidal_playlist_id is not None,
        "remote_state_verified": False,
        "proposed_track_ids": plan_additions(
            (r["match"]["selected_id"] for r in rows if r["match"]["status"] == "matched"), []
        ),
        "removals": [],
        "removal_review_spotify_ids": list(diff.get("removed_ids", [])),
        "unresolved_positions": [
            r["position"] for r in rows if r["match"]["status"] not in SETTLED_STATUSES
        ],
        "writes_performed": 0,
        "note": "Proposal only. No TIDAL playlist is linked or written by sync; mapped "
        "playlist writes need separate approval. Removals stay review-only.",
    }
