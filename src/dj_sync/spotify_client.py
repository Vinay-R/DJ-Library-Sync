from typing import Any

from .errors import SyncError
from .http import ReadClient
from .models import PlaylistItem, Snapshot, Track, normalize_isrc

# Documented maximum page size for playlist items.
PAGE_SIZE = 50
MAX_ITEMS = 10_000


class SpotifyClient:
    def __init__(self, api: ReadClient) -> None:
        self.api = api

    def playlists(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        path: str | None = "me/playlists?limit=50"
        seen: set[str] = set()
        while path:
            if path in seen:
                raise SyncError("Spotify repeated a pagination link.")
            seen.add(path)
            page = self.api.get(path)
            if not isinstance(page.get("items"), list):
                raise SyncError("Spotify playlist listing is malformed.")
            result.extend(item for item in page["items"] if isinstance(item, dict))
            path = page.get("next")
        return result

    def user_id(self) -> str:
        user = self.api.get("me")
        if not isinstance(user.get("id"), str) or not user["id"]:
            raise SyncError("Spotify did not return the authenticated user's ID.")
        return str(user["id"])

    def sample(self, playlist_id: str, limit: int = 10) -> Snapshot:
        """The first 1–10 positions. Never a complete membership snapshot for longer lists."""
        if not 1 <= limit <= 10:
            raise SyncError("Phase 1 accepts only 1–10 playlist items.")
        return self._fetch(playlist_id, limit, limit)

    def full(self, playlist_id: str) -> Snapshot:
        """Every position, checked against one unchanged Spotify snapshot ID."""
        return self._fetch(playlist_id, None, PAGE_SIZE)

    def _fetch(self, playlist_id: str, limit: int | None, page_size: int) -> Snapshot:
        before = self.api.get(f"playlists/{playlist_id}", {"fields": "id,name,snapshot_id"})
        snapshot = before.get("snapshot_id")
        if not snapshot:
            raise SyncError("Spotify did not return a playlist snapshot ID.")
        items: list[PlaylistItem] = []
        total: int | None = None
        while limit is None or len(items) < limit:
            if len(items) >= MAX_ITEMS:
                raise SyncError("Spotify playlist exceeds the 10,000-item safety limit.")
            requested = page_size if limit is None else min(page_size, limit - len(items))
            page = self.api.get(
                f"playlists/{playlist_id}/items",
                {
                    "offset": len(items),
                    "limit": requested,
                },
            )
            rows = page.get("items")
            if not isinstance(rows, list) or not isinstance(page.get("total"), int):
                raise SyncError("Spotify playlist items are missing or inaccessible.")
            total = page["total"]
            if len(rows) > requested:
                raise SyncError("Spotify returned more items than requested.")
            for row in rows:
                position = len(items)
                raw = row.get("item", row.get("track")) if isinstance(row, dict) else None
                if not isinstance(raw, dict) or not raw.get("id"):
                    items.append(PlaylistItem(position, None, "missing_or_local_item"))
                    continue
                if raw.get("is_local") or row.get("is_local") or raw.get("type") != "track":
                    items.append(PlaylistItem(position, None, "local_or_non_track_item"))
                    continue
                # A relinked playable ID is the catalog item actually being matched.
                # No merging by ISRC; conflicting editions remain separate recordings.
                restrictions = raw.get("restrictions") or {}
                available = raw.get("is_playable")
                if restrictions:
                    available = False
                track = Track(
                    "spotify",
                    str(raw["id"]),
                    raw.get("name", ""),
                    tuple(a.get("name", "") for a in raw.get("artists", [])),
                    raw.get("duration_ms"),
                    normalize_isrc((raw.get("external_ids") or {}).get("isrc")),
                    available=available,
                    explicit=raw.get("explicit"),
                    album_title=(raw.get("album") or {}).get("name", ""),
                    album_release_date=(raw.get("album") or {}).get("release_date", ""),
                )
                items.append(
                    PlaylistItem(
                        position, track, "unavailable_in_spotify" if available is False else None
                    )
                )
            if len(items) >= total:
                break
            if not rows or not page.get("next"):
                raise SyncError("Spotify pagination ended before the stated total.")
        after = self.api.get(f"playlists/{playlist_id}", {"fields": "id,name,snapshot_id"})
        if after.get("snapshot_id") != snapshot:
            raise SyncError("Spotify playlist changed during fetch; rerun for a consistent sample.")
        return Snapshot(
            playlist_id,
            str(before.get("name", "")),
            str(snapshot),
            tuple(items),
            total is not None and len(items) == total,
        )
