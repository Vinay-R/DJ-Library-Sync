"""PKCE with a loopback-only callback, private token files and refresh rotation."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import credential
from .errors import SyncError
from .http import token_post
from .storage import save_json

ENDPOINTS = {
    "spotify": ("https://accounts.spotify.com/authorize", "https://accounts.spotify.com/api/token"),
    "tidal": ("https://login.tidal.com/authorize", "https://auth.tidal.com/v1/oauth2/token"),
}
SCOPES = {
    "spotify": "playlist-read-private playlist-read-collaborative",
    "tidal": "user.read playlists.read search.read",
}
TIDAL_WRITE_SCOPES = "user.read playlists.read playlists.write"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def validate_callback(url: str, redirect_uri: str, state: str) -> str:
    parsed, redirect = urlparse(url), urlparse(redirect_uri)
    if (parsed.scheme, parsed.netloc, parsed.path) != (
        redirect.scheme,
        redirect.netloc,
        redirect.path,
    ):
        raise SyncError("OAuth callback address mismatch.")
    query = parse_qs(parsed.query)
    states = query.get("state", [])
    if len(states) != 1 or not hmac.compare_digest(states[0], state):
        raise SyncError("OAuth state mismatch; restart authentication.")
    if "error" in query:
        raise SyncError("OAuth consent was denied or failed.")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise SyncError("OAuth callback missing authorization code.")
    return codes[0]


class TokenProvider:
    def __init__(
        self, service: str, directory: Path, client: httpx.Client, mode: str = "user"
    ) -> None:
        self.service, self.client, self.mode = service, client, mode
        if mode == "user_write" and service != "tidal":
            raise SyncError("Write authorization is supported only for TIDAL test playlists.")
        self.scopes = TIDAL_WRITE_SCOPES if mode == "user_write" else SCOPES[service]
        self.client_id = credential(f"{service.upper()}_CLIENT_ID")
        self.secret = credential("TIDAL_CLIENT_SECRET") if mode == "client_credentials" else None
        self.path = directory / f"{service}-{mode}-tokens.json"
        self.token: dict[str, Any] = {}
        if self.path.exists():
            if self.path.is_symlink():
                raise SyncError("Token file must not be a symlink.")
            self.path.chmod(0o600)
            try:
                self.token = json.loads(self.path.read_text())
                if self.token.get("client_id") != self.client_id:
                    self.token = {}
            except (ValueError, AttributeError) as exc:
                raise SyncError("Token cache is invalid; authenticate again.") from exc

    def save(self, payload: dict[str, Any]) -> None:
        scopes = set(str(payload.get("scope", self.token.get("scope", ""))).split())
        if self.mode != "client_credentials" and not set(self.scopes.split()) <= scopes:
            raise SyncError("Required scopes were not granted; check developer app settings.")
        try:
            expiry = time.time() + float(payload["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SyncError("OAuth response missing a valid expiration.") from exc
        self.token = {**self.token, **payload, "expires_at": expiry, "client_id": self.client_id}
        save_json(self.path, self.token)

    def __call__(self, force: bool = False) -> str:
        if (
            not force
            and self.token.get("access_token")
            and self.token.get("expires_at", 0) > (time.time() + 60)
        ):
            return str(self.token["access_token"])
        if self.mode == "client_credentials":
            self.save(
                token_post(
                    self.client,
                    ENDPOINTS[self.service][1],
                    {"grant_type": "client_credentials"},
                    (self.client_id, self.secret or ""),
                )
            )
        elif self.token.get("refresh_token"):
            self.save(
                token_post(
                    self.client,
                    ENDPOINTS[self.service][1],
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": self.token["refresh_token"],
                        "client_id": self.client_id,
                    },
                )
            )
        else:
            raise SyncError(f"Run dj-sync auth {self.service} to authorize read access first.")
        return str(self.token["access_token"])

    def login(self, timeout: float = 180) -> None:
        if self.mode == "client_credentials":
            self(True)
            return
        default_port = 8765 if self.service == "spotify" else 8766
        redirect = os.environ.get(
            f"{self.service.upper()}_REDIRECT_URI", f"http://127.0.0.1:{default_port}/callback"
        )
        parsed = urlparse(redirect)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or (parsed.query or parsed.fragment or parsed.username)
        ):
            raise SyncError("Use a registered http://127.0.0.1:PORT/callback redirect URI.")
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(32)
        outcome: dict[str, str] = {}

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if urlparse(self.path).path != parsed.path:
                    self.send_error(404)
                    return
                try:
                    code = validate_callback(f"http://{parsed.netloc}{self.path}", redirect, state)
                except SyncError:
                    self.send_error(
                        400, "Invalid OAuth callback. Restart login if consent was denied."
                    )
                    return
                outcome["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Authorization received. You can close this tab.")

            def log_message(self, format: str, *args: Any) -> None:
                pass  # Never log callback URLs/codes.

        url = (
            ENDPOINTS[self.service][0]
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self.client_id,
                    "redirect_uri": redirect,
                    "scope": self.scopes,
                    "state": state,
                    "code_challenge_method": "S256",
                    "code_challenge": challenge,
                }
            )
        )
        with HTTPServer(("127.0.0.1", parsed.port), Callback) as server:
            server.timeout = 1
            purpose = "TIDAL test-playlist write" if self.mode == "user_write" else "read-only"
            print(f"Opening your browser for {purpose} consent. If it does not open, visit:")
            print(url)
            webbrowser.open(url)
            deadline = time.monotonic() + timeout
            while "code" not in outcome and time.monotonic() < deadline:
                server.handle_request()
        if "code" not in outcome:
            raise SyncError("OAuth callback timed out; rerun the auth command.")
        self.save(
            token_post(
                self.client,
                ENDPOINTS[self.service][1],
                {
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "code": outcome["code"],
                    "redirect_uri": redirect,
                    "code_verifier": verifier,
                },
            )
        )
