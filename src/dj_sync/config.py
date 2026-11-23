import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

from .errors import SyncError

DESTINATIONS = {"DJ": "Nu DJ Vibes", "Vibes": "Nu Vibes Vibes"}


@dataclass(frozen=True)
class Config:
    data_dir: Path
    country_code: str
    tidal_auth: str
    request_interval: float
    playlists: dict[str, str]
    music_dirs: tuple[Path, ...] = ()
    tidaler_bin: Path = Path(".venv-tidaler/bin/tidaler")
    aiff_dir: Path = Path("~/Music/Tidal-DL/AIFF").expanduser()
    download_limit: int = 50
    ffmpeg_bin: str = "ffmpeg"

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.is_file():
            raise SyncError("Configuration missing. Copy config.example.yaml to config.yaml.")
        load_dotenv(path.parent / ".env", override=False)
        try:
            raw: Any = yaml.safe_load(path.read_text()) or {}
            if not isinstance(raw, dict):
                raise ValueError
            if set(raw) - {
                "data_dir",
                "country_code",
                "tidal_auth",
                "request_interval",
                "playlists",
                "music_dirs",
                "tidaler_bin",
                "aiff_dir",
                "download_limit",
                "ffmpeg_bin",
            }:
                raise ValueError
            mappings = raw.get("playlists", {})
            if not isinstance(mappings, dict) or any(
                not isinstance(k, str)
                or not re.fullmatch(r"[A-Za-z0-9]{22}", k)
                or v not in DESTINATIONS
                for k, v in mappings.items()
            ):
                raise ValueError
            country = raw.get("country_code", "US")
            if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
                raise ValueError
            mode = raw.get("tidal_auth", "client_credentials")
            interval = float(raw.get("request_interval", 0.5))
            if mode not in {"client_credentials", "user"} or not 0 <= interval <= 60:
                raise ValueError
            directory = path.parent / raw.get("data_dir", "data")
            dirs = raw.get("music_dirs", [])
            if not isinstance(dirs, list) or not all(isinstance(d, str) and d for d in dirs):
                raise ValueError
            music = tuple(Path(d).expanduser() for d in dirs)
            limit = int(raw.get("download_limit", 50))
            if not 1 <= limit <= 500:
                raise ValueError
            tidaler_bin = raw.get("tidaler_bin", ".venv-tidaler/bin/tidaler")
            aiff_dir = raw.get("aiff_dir", "~/Music/Tidal-DL/AIFF")
            ffmpeg = raw.get("ffmpeg_bin") or (
                "/opt/homebrew/bin/ffmpeg"
                if Path("/opt/homebrew/bin/ffmpeg").exists()
                else "ffmpeg"
            )
            if not all(isinstance(v, str) and v for v in (tidaler_bin, aiff_dir, ffmpeg)):
                raise ValueError
            return cls(
                directory,
                country,
                mode,
                interval,
                mappings,
                music,
                path.parent / Path(tidaler_bin).expanduser(),
                Path(aiff_dir).expanduser(),
                limit,
                ffmpeg,
            )
        except (ValueError, TypeError, yaml.YAMLError) as exc:
            raise SyncError(
                "Invalid config: check keys, playlist IDs, groups, market and interval."
            ) from exc


def credential(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SyncError(f"Missing {name}. Set it in the project .env file or environment.")
    return value


def playlist_id(value: str) -> str:
    if value.startswith("https://open.spotify.com/playlist/"):
        value = urlparse(value).path.split("/")[-1]
    elif value.startswith("spotify:playlist:"):
        value = value.split(":")[-1]
    if not re.fullmatch(r"[A-Za-z0-9]{22}", value):
        raise SyncError("Expected a Spotify playlist ID, URI, or open.spotify.com playlist link.")
    return value
