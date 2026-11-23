import hashlib
import json
import logging
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from dj_sync.auth import SCOPES, TokenProvider, pkce, validate_callback
from dj_sync.errors import ApiError, SyncError
from dj_sync.http import ReadClient, retry_delay, token_post
from dj_sync.logging_setup import JsonFormatter


def test_pkce_and_state():
    verifier, challenge = pkce()
    assert 43 <= len(verifier) <= 128
    assert challenge == urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    redirect = "http://127.0.0.1:8765/callback"
    assert validate_callback(redirect + "?code=abc&state=xyz", redirect, "xyz") == "abc"
    for suffix in [
        "?code=abc&state=bad",
        "?code=abc",
        "?error=denied&state=xyz",
        "?code=a&code=b&state=xyz",
    ]:
        with pytest.raises(SyncError):
            validate_callback(redirect + suffix, redirect, "xyz")
    with pytest.raises(SyncError):
        validate_callback("http://evil/callback?code=abc&state=xyz", redirect, "xyz")


def test_refresh_preserves_refresh_token_and_rotates_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == "https://accounts.spotify.com/api/token"
        assert request.method == "POST"
        return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})

    provider = TokenProvider(
        "spotify", tmp_path, httpx.Client(transport=httpx.MockTransport(handler))
    )
    provider.save(
        {
            "access_token": "old",
            "refresh_token": "keep-me",
            "expires_in": 0,
            "scope": SCOPES["spotify"],
        }
    )
    assert provider() == "new-access"
    assert provider.token["refresh_token"] == "keep-me"
    assert provider.path.stat().st_mode & 0o777 == 0o600
    assert json.loads(provider.path.read_text())["access_token"] == "new-access"
    assert len(requests) == 1


def test_missing_scopes_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client")
    provider = TokenProvider("spotify", tmp_path, httpx.Client())
    with pytest.raises(SyncError, match="scopes"):
        provider.save({"access_token": "test", "expires_in": 3600, "scope": ""})
    assert not provider.path.exists()


def test_catalog_oauth_uses_official_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("TIDAL_CLIENT_ID", "client")
    monkeypatch.setenv("TIDAL_CLIENT_SECRET", "secret")

    def handler(request):
        assert request.url == "https://auth.tidal.com/v1/oauth2/token"
        assert request.headers["authorization"].startswith("Basic ")
        assert b"grant_type=client_credentials" in request.content
        return httpx.Response(200, json={"access_token": "catalog", "expires_in": 3600})

    provider = TokenProvider(
        "tidal",
        tmp_path,
        httpx.Client(transport=httpx.MockTransport(handler)),
        "client_credentials",
    )
    assert provider() == "catalog"


def test_retry_after_and_refresh_once():
    replies = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(401),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    refreshes, waits = [], []

    def token(force):
        refreshes.append(force)
        return "token"

    reader = ReadClient(
        "spotify",
        "https://api.spotify.com/v1/",
        token,
        httpx.Client(transport=httpx.MockTransport(lambda r: next(replies))),
        0,
        waits.append,
    )
    assert reader.get("me") == {"ok": True}
    assert 7 in waits
    assert refreshes.count(True) == 1


def test_long_rate_limit_does_not_retry_early():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    reader = ReadClient(
        "tidal",
        "https://openapi.tidal.com/v2/",
        lambda _: "token",
        httpx.Client(transport=httpx.MockTransport(handler)),
        0,
        lambda _: None,
    )
    with pytest.raises(ApiError) as error:
        reader.get("tracks")
    assert error.value.status == 429 and len(calls) == 1


def test_retry_http_date():
    value = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    assert 28 <= retry_delay(value, 0) <= 30


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/collect",
        "http://api.spotify.com/v1/me",
        "https://api.spotify.com.evil/v1/me",
        "https://api.spotify.com/other",
    ],
)
def test_pagination_cannot_exfiltrate_tokens(url):
    def forbidden(request):
        pytest.fail("Request must not be sent")

    reader = ReadClient(
        "spotify",
        "https://api.spotify.com/v1/",
        lambda _: "secret",
        httpx.Client(transport=httpx.MockTransport(forbidden)),
        0,
        lambda _: None,
    )
    with pytest.raises(SyncError, match="Blocked"):
        reader.get(url)


def test_post_is_only_allowed_for_oauth():
    with pytest.raises(SyncError):
        token_post(httpx.Client(), "https://openapi.tidal.com/v2/playlists", {})
    assert not hasattr(ReadClient, "post")


def test_errors_and_logs_exclude_remote_body_and_headers():
    secret = "SUPER_SECRET_ACCESS_TOKEN"
    reader = ReadClient(
        "tidal",
        "https://openapi.tidal.com/v2/",
        lambda _: secret,
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text=secret))),
        0,
        lambda _: None,
    )
    with pytest.raises(ApiError) as error:
        reader.get("tracks")
    assert secret not in str(error.value)
    record = logging.LogRecord("dj_sync", logging.WARNING, "", 1, "http_retry", (), None)
    record.authorization = secret
    assert secret not in JsonFormatter().format(record)


@pytest.mark.parametrize("service", ["spotify", "tidal"])
def test_browser_pkce_login_exchange_without_external_requests(tmp_path, monkeypatch, service):
    import io
    from urllib.parse import parse_qs, urlparse

    from dj_sync.auth import ENDPOINTS

    monkeypatch.setenv(f"{service.upper()}_CLIENT_ID", "test-client")
    captured = {}
    monkeypatch.setattr("dj_sync.auth.webbrowser.open", lambda url: captured.update(url=url))

    class Server:
        def __init__(self, address, handler):
            assert address[0] == "127.0.0.1"
            self.handler = handler

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def handle_request(self):
            query = parse_qs(urlparse(captured["url"]).query)
            captured["challenge"] = query["code_challenge"][0]
            assert query["scope"][0] == SCOPES[service]
            assert query["code_challenge_method"] == ["S256"]
            callback = object.__new__(self.handler)
            callback.path = "/callback?code=test-code&state=" + query["state"][0]
            callback.wfile = io.BytesIO()
            callback.send_response = lambda status: captured.update(status=status)
            callback.send_header = lambda *args: None
            callback.end_headers = lambda: None
            callback.do_GET()
            assert captured["status"] == 200

    monkeypatch.setattr("dj_sync.auth.HTTPServer", Server)

    def handler(request):
        assert str(request.url) == ENDPOINTS[service][1]
        data = parse_qs(request.content.decode())
        assert data["grant_type"] == ["authorization_code"]
        assert data["code"] == ["test-code"]
        challenge = urlsafe_b64encode(hashlib.sha256(data["code_verifier"][0].encode()).digest())
        assert challenge.decode().rstrip("=") == captured["challenge"]
        return httpx.Response(
            200,
            json={
                "access_token": "test-access",
                "refresh_token": "test-refresh",
                "expires_in": 3600,
                "scope": SCOPES[service],
            },
        )

    provider = TokenProvider(
        service, tmp_path, httpx.Client(transport=httpx.MockTransport(handler))
    )
    provider.login()
    assert provider() == "test-access"


def test_unsafe_redirect_rejected_before_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://0.0.0.0:8765/callback")
    provider = TokenProvider("spotify", tmp_path, httpx.Client())
    with pytest.raises(SyncError, match="127.0.0.1"):
        provider.login()
