"""Entire read-only command twice using official-shaped synthetic HTTP fixtures."""

import json
import time

import httpx

from dj_sync.auth import SCOPES
from dj_sync.cli import main
from dj_sync.storage import save_json


def test_cli_twice_uses_only_get_and_oauth_post(tmp_path, monkeypatch, capsys):
    playlist = "a" * 22
    config = tmp_path / "config.yaml"
    config.write_text(f"request_interval: 0\nplaylists:\n  {playlist}: DJ\n")
    for key in ["SPOTIFY_CLIENT_ID", "TIDAL_CLIENT_ID", "TIDAL_CLIENT_SECRET"]:
        monkeypatch.setenv(key, "synthetic-test-value")
    save_json(
        tmp_path / "data" / "spotify-user-tokens.json",
        {
            "client_id": "synthetic-test-value",
            "access_token": "spotify-test-token",
            "expires_at": time.time() + 3600,
            "scope": SCOPES["spotify"],
        },
    )
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert str(request.url) == "https://auth.tidal.com/v1/oauth2/token"
            return httpx.Response(
                200, json={"access_token": "tidal-test-token", "expires_in": 3600}
            )
        assert request.method == "GET"
        if request.url.host == "api.spotify.com":
            if request.url.path.endswith("/items"):
                return httpx.Response(
                    200,
                    json={
                        "total": 10,
                        "next": None,
                        "items": [
                            {
                                "item": {
                                    "type": "track",
                                    "id": f"s{i}",
                                    "name": "Song",
                                    "artists": [{"name": "Artist"}],
                                    "duration_ms": 180000,
                                    "is_playable": True,
                                }
                            }
                            for i in range(10)
                        ],
                    },
                )
            assert request.url.params["fields"] == "id,name,snapshot_id"
            return httpx.Response(200, json={"name": "SYNTHETIC TEST", "snapshot_id": "v1"})
        assert request.url.path == "/v2/searchResults"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "opaque",
                        "relationships": {"tracks": {"data": [{"type": "tracks", "id": "42"}]}},
                    }
                ],
                "included": [
                    {
                        "type": "tracks",
                        "id": "42",
                        "attributes": {
                            "title": "Song",
                            "duration": "PT3M",
                            "availability": ["STREAM"],
                        },
                        "relationships": {
                            "artists": {"data": [{"type": "artists", "id": "artist"}]}
                        },
                    },
                    {"type": "artists", "id": "artist", "attributes": {"name": "Artist"}},
                ],
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "dj_sync.cli.httpx.Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    args = ["--config", str(config), "sync", "--playlist", playlist, "--dry-run"]
    assert main(args) == 0
    first_count = len(calls)
    assert main(args) == 0
    assert len(calls) == first_count + 3  # Metadata before/after plus first-ten read.
    reports = list((tmp_path / "data" / "reports").glob("*.json"))
    assert len(reports) == 2
    for path in reports:
        report = json.loads(path.read_text())
        assert report["counts"] == {"matched": 10}
        assert report["plan"]["proposed_track_ids"] == ["42"]
        assert report["plan"]["writes_performed"] == 0
    assert "test-token" not in capsys.readouterr().out
