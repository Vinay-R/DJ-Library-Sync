"""Explicitly applied, additive-only writes to one new, provenance-checked test playlist.

Never accepts a destination playlist ID from a command argument. Durable intent is
saved before any POST; lost responses are reconciled against the user's catalog.
"""

import hashlib
import json
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .auth import TIDAL_WRITE_SCOPES, TokenProvider
from .db import Ledger
from .errors import ApiError, SyncError
from .http import ReadClient
from .planner import plan_additions
from .storage import save_json
from .tidal_client import next_link

# Documented maxItems for POST /playlists/{id}/relationships/items (OpenAPI 1.10.157).
MAX_ADD_BATCH = 50


def load_plan(ledger: Ledger, run_id: str, country: str) -> dict[str, Any]:
    row = ledger.connection.execute(
        "SELECT status,started,report_json FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if not row or row[0] != "completed" or not row[2]:
        raise SyncError("Choose a completed matching run with no API errors.")
    if time.time() - row[1] > 86400:
        raise SyncError("Matching report is older than 24 hours; run the read-only sync again.")
    report = json.loads(row[2])
    if report["country"] != country or not 1 <= len(report["rows"]) <= 10:
        raise SyncError("Test plan must contain 1–10 source items in the configured country.")
    ids = []
    skipped = []
    for item in report["rows"]:
        match = item["match"]
        selected = match.get("selected_id")
        if match["status"] != "matched":
            skipped.append(item["source"]["title"] if item["source"] else item["issue"])
            continue
        if not selected or not any(
            c["track"]["id"] == selected and c["eligible"] and c["score"] >= 0.89
            for c in match["candidates"]
        ):
            raise SyncError("Report selection lacks high-confidence compatible evidence.")
        ids.append(selected)
    ids = plan_additions(ids, [])
    if not ids:
        raise SyncError("There are no confident matches to add to a test playlist.")
    plan = {
        "source_id": report["playlist"]["id"],
        "snapshot_id": report["playlist"]["snapshot_id"],
        "country": country,
        "track_ids": ids,
    }
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return {
        **plan,
        "hash": digest,
        "report_run_id": run_id,
        "skipped": skipped,
        "name": ("DJ Library Sync TEST — " + report["playlist"]["name"])[:250],
        "access_type": "UNLISTED",
    }


class TestPlaylistAPI:
    """Only the two documented playlist POSTs, plus guarded API reads."""

    __test__ = False

    def __init__(
        self,
        token: TokenProvider,
        client: httpx.Client,
        interval: float,
        country: str,
        *,
        apply: bool = False,
        max_batch: int = 10,
    ) -> None:
        if token.service != "tidal" or token.mode != "user_write":
            raise SyncError("Test playlist writes require separate TIDAL user-write authorization.")
        if not 1 <= max_batch <= MAX_ADD_BATCH:
            raise SyncError("TIDAL accepts at most 50 items per playlist addition.")
        self.max_batch = max_batch
        self.token, self.client, self.country, self.apply = token, client, country, apply
        self.read = ReadClient("tidal", "https://openapi.tidal.com/v2/", token, client, interval)
        self.posts = 0
        self.verified_id: str | None = None

    def user_id(self) -> str:
        self.token(False)
        if not set(TIDAL_WRITE_SCOPES.split()) <= set(self.token.token.get("scope", "").split()):
            raise SyncError("TIDAL write scopes were not granted. Run auth tidal --write.")
        user = self.read.get("users/me").get("data", {})
        if user.get("type") != "users" or not user.get("id"):
            raise SyncError("TIDAL did not return a usable authenticated user identity.")
        return str(user["id"])

    def _pages(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        url = str(httpx.URL(urljoin(self.read.base, path)).copy_merge_params(params))
        expected_path = urlsplit(url).path
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for _ in range(100):
            if url in seen:
                raise SyncError("TIDAL repeated a playlist pagination link.")
            seen.add(url)
            page = self.read.get(url)
            if not isinstance(page.get("data"), list):
                raise SyncError("TIDAL omitted playlist list or membership data.")
            result.extend(page["data"])
            following = next_link(page)
            if not following:
                return result
            parsed = urlsplit(urljoin(url, following))
            if parsed.path == expected_path.removeprefix("/v2"):
                parsed = parsed._replace(path=expected_path)
            if parsed.path != expected_path:
                raise SyncError("TIDAL playlist pagination changed the requested resource.")
            query = dict(parse_qsl(urlsplit(url).query))
            query.update(parse_qsl(parsed.query))
            url = urlunsplit(parsed._replace(query=urlencode(query)))
        raise SyncError("TIDAL playlist listing exceeded the safety limit; no additions attempted.")

    def find_intent(self, intent: dict[str, Any]) -> str | None:
        rows = self._pages("playlists", {"filter[owners.id]": "me", "countryCode": self.country})
        matches = [
            p for p in rows if p.get("attributes", {}).get("description") == intent["description"]
        ]
        if len(matches) > 1:
            raise SyncError("Multiple playlists match this test intent; manual review required.")
        return str(matches[0]["id"]) if matches else None

    def verify_target(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.verified_id = None
        playlist_id = str(intent["remote_id"])
        resource = self.read.get(
            f"playlists/{quote(playlist_id, safe='')}",
            {
                "countryCode": self.country,
                "include": "owners",
            },
        ).get("data", {})
        attrs = resource.get("attributes", {})
        owners = resource.get("relationships", {}).get("owners", {}).get("data", [])
        if (
            resource.get("id") != playlist_id
            or resource.get("type") != "playlists"
            or attrs.get("name") != intent["name"]
            or attrs.get("description") != intent["description"]
            or attrs.get("accessType") != "UNLISTED"
            or not any(
                o.get("type") == "users" and str(o.get("id")) == intent["user_id"] for o in owners
            )
        ):
            raise SyncError(
                "Test playlist identity, owner, marker or visibility changed; refusing writes."
            )
        self.verified_id = playlist_id
        return attrs

    def members(self, playlist_id: str) -> list[str]:
        rows = self._pages(
            f"playlists/{quote(playlist_id, safe='')}/relationships/items",
            {"countryCode": self.country},
        )
        if any(r.get("type") != "tracks" or not r.get("id") for r in rows):
            raise SyncError("Test playlist contains unknown/non-track items; refusing writes.")
        return [str(r["id"]) for r in rows]

    def _post(self, path: str, payload: dict[str, Any], key: str, expected: int) -> dict[str, Any]:
        allowed_add = (
            f"playlists/{quote(self.verified_id, safe='')}/relationships/items"
            if self.verified_id
            else None
        )
        if not self.apply or path not in ("playlists", allowed_add):
            raise SyncError(
                "Blocked playlist write: explicit apply and verified test target required."
            )
        self.read.sleep(self.read.interval)
        try:
            self.posts += 1
            response = self.client.post(
                self.read.base + path,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.token(False)}",
                    "Content-Type": "application/vnd.api+json",
                    "Accept": "application/vnd.api+json",
                    "Idempotency-Key": key,
                },
            )
        except httpx.TransportError as exc:
            raise SyncError(
                "TIDAL write outcome is uncertain. Rerun this same test plan to reconcile."
            ) from exc
        if response.status_code != expected:
            raise ApiError("tidal test playlist", response.status_code)
        try:
            document = response.json()
            if not isinstance(document, dict):
                raise ValueError
            return document
        except ValueError as exc:
            raise SyncError(
                "TIDAL write response is incomplete; rerun this same plan to reconcile."
            ) from exc

    def create(self, intent: dict[str, Any]) -> str:
        resource = self._post(
            "playlists",
            {
                "data": {
                    "type": "playlists",
                    "attributes": {
                        "name": intent["name"],
                        "description": intent["description"],
                        "accessType": "UNLISTED",
                    },
                }
            },
            intent["create_key"],
            201,
        ).get("data", {})
        if resource.get("type") != "playlists" or not resource.get("id"):
            raise SyncError("Created playlist ID missing; rerun this same plan to reconcile.")
        return str(resource["id"])

    def add(self, playlist_id: str, track_ids: list[str], key: str) -> None:
        if playlist_id != self.verified_id or not 1 <= len(track_ids) <= self.max_batch:
            raise SyncError(
                "Additions must target the verified test or managed playlist, within one batch."
            )
        self._post(
            f"playlists/{quote(playlist_id, safe='')}/relationships/items",
            {
                "data": [{"type": "tracks", "id": t} for t in dict.fromkeys(track_ids)],
                "meta": {"onDuplicates": "SKIP"},
            },
            key,
            200,
        )


def apply_test_plan(api: TestPlaylistAPI, plan: dict[str, Any], directory: Path) -> dict[str, Any]:
    # The caller holds the project process lock. Durable state pins the source,
    # account and exact plan; no arbitrary existing destination ID is accepted.
    state_path = (
        directory
        / "test-playlists"
        / (hashlib.sha256(plan["source_id"].encode()).hexdigest() + ".json")
    )
    user_id = api.user_id()
    fresh = not state_path.exists()
    if not fresh:
        intent = json.loads(state_path.read_text())
        if intent["plan_hash"] != plan["hash"] or intent["user_id"] != user_id:
            raise SyncError(
                "Existing test belongs to a different plan/account; "
                "review it before changing scope."
            )
    else:
        key = str(uuid.uuid4())
        intent = {
            "plan_hash": plan["hash"],
            "user_id": user_id,
            "name": plan["name"],
            "description": f"DJ Library Sync test. Managed intent: {key}",
            "create_key": key,
            "create_started": time.time(),
            "remote_id": None,
            "status": "create_pending",
            "events": [],
            "plan": plan,
        }
        save_json(state_path, intent)
    if not intent["remote_id"]:
        recovered = api.find_intent(intent) if not fresh else None
        if recovered:
            intent["remote_id"] = recovered
        else:
            # TIDAL documents a one-hour idempotency window. Stop before expiry,
            # rather than risk creating a duplicate after a lost response.
            if time.time() - intent["create_started"] > 3000:
                raise SyncError(
                    "Uncertain creation is outside the replay window; manual review required."
                )
            intent["remote_id"] = api.create(intent)
        intent["status"] = "created"
        save_json(state_path, intent)
    attrs = api.verify_target(intent)
    before = api.members(intent["remote_id"])
    if attrs.get("numberOfItems") is not None and attrs["numberOfItems"] != len(before):
        raise SyncError("Playlist size changed during inspection; retry before applying additions.")
    expected_ids = plan["track_ids"]
    if set(before) - set(expected_ids) or len(before) != len(set(before)):
        raise SyncError("Test playlist contains unexpected or duplicate entries; no changes made.")
    missing = plan_additions(expected_ids, before)
    if missing:
        action: dict[str, Any] = {
            "idempotency_key": str(uuid.uuid4()),
            "ids": missing,
            "status": "pending",
            "started": time.time(),
        }
        intent["events"].append(action)
        intent["status"] = "add_pending"
        save_json(state_path, intent)
        api.add(intent["remote_id"], missing, action["idempotency_key"])
    api.verify_target(intent)
    after = api.members(intent["remote_id"])
    if Counter(after) != Counter(expected_ids):
        raise SyncError(
            "TIDAL readback did not confirm exactly the planned entries; rerun to reconcile."
        )
    for event in intent["events"]:
        if set(event["ids"]) <= set(after):
            event["status"] = "verified_by_readback"
    result = {
        "mode": "applied",
        "name": intent["name"],
        "playlist_id": intent["remote_id"],
        "url": f"https://tidal.com/browse/playlist/{quote(intent['remote_id'], safe='')}",
        "visibility": "UNLISTED",
        "report_run_id": plan["report_run_id"],
        "before_count": len(before),
        "after_count": len(after),
        "added_count": len(after) - len(before),
        "post_requests": api.posts,
        "verified_ids": after,
        "duplicates": 0,
        "skipped": plan["skipped"],
        "verified_at": time.time(),
    }
    intent.update(status="verified", last_result=result)
    save_json(state_path, intent)
    save_json(directory / "reports" / f"tidal-test-{uuid.uuid4()}.json", result)
    return result
