"""Only GET is exposed for service APIs; POST is confined to fixed OAuth URLs."""

import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .errors import ApiError, SyncError

log = logging.getLogger("dj_sync")
TOKEN_URLS = {
    "https://accounts.spotify.com/api/token",
    "https://auth.tidal.com/v1/oauth2/token",
}


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError):
                pass
    return 2**attempt + random.uniform(0, 0.5)


def token_post(
    client: httpx.Client, url: str, data: dict[str, str], auth: tuple[str, str] | None = None
) -> dict[str, Any]:
    if url not in TOKEN_URLS:
        raise SyncError("Blocked unrecognized OAuth endpoint.")
    try:
        # Do not blindly replay an exchanged code or rotating refresh token.
        response = client.post(url, data=data, auth=auth) if auth else client.post(url, data=data)
    except httpx.TransportError as exc:
        raise SyncError("OAuth network request failed; retry authentication.") from exc
    if response.status_code != 200:
        raise ApiError("OAuth", response.status_code)
    try:
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise ValueError
        return payload
    except ValueError as exc:
        raise SyncError("OAuth returned an invalid token response.") from exc


class ReadClient:
    def __init__(
        self,
        service: str,
        base: str,
        token: Callable[[bool], str],
        client: httpx.Client,
        interval: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.service, self.base, self.token = service, base.rstrip("/") + "/", token
        self.client, self.interval, self.sleep = client, interval, sleep

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = str(httpx.URL(urljoin(self.base, path)))
        parsed, origin = urlparse(url), urlparse(self.base)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc) or not (
            parsed.path.startswith(origin.path)
        ):
            raise SyncError("Blocked pagination URL outside the official API origin.")
        refreshed = False
        for attempt in range(5):
            self.sleep(self.interval)
            try:
                response = self.client.get(
                    url,
                    params=params or None,
                    headers={
                        "Authorization": f"Bearer {self.token(False)}",
                        "Accept": "application/vnd.api+json"
                        if self.service == "tidal"
                        else "application/json",
                    },
                )
            except httpx.TransportError as exc:
                if attempt == 4:
                    raise SyncError(
                        f"{self.service}: network request failed after retries."
                    ) from exc
                self.sleep(retry_delay(None, attempt))
                continue
            status = response.status_code
            if status == 401 and not refreshed:
                self.token(True)
                refreshed = True
                continue
            if status == 429 or status in {500, 502, 503, 504}:
                delay = retry_delay(response.headers.get("Retry-After"), attempt)
                if attempt == 4 or delay > 60:
                    raise ApiError(self.service, status)
                log.warning(
                    "http_retry",
                    extra={
                        "service": self.service,
                        "status": status,
                        "attempt": attempt + 1,
                        "delay": delay,
                    },
                )
                self.sleep(delay)
                continue
            if status != 200:
                raise ApiError(self.service, status)
            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError
                return payload
            except ValueError as exc:
                raise SyncError(f"{self.service}: invalid API response.") from exc
        raise ApiError(self.service, 401)
