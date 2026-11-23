"""Acquisition through the user's Tidaler install: plan, run, verify by tags, convert.

Tidaler is a third-party downloader the user already uses. This module drives only
its public CLI (`tidaler dl -l FILE`) and reads its settings file. It never reads the
session token beyond checking that the file exists, never edits Tidaler's files, and
never implements downloading itself. Tidaler's exit code is not trusted: a file counts
as downloaded only when the TIDAL URL written into its tags names the expected track
and its length matches the catalog. Conversion to AIFF keeps the source bit depth,
caps the sample rate, writes tags itself and reads them back before reporting success.
Nothing here deletes or moves files.
"""

import json
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, TSRC, TXXX

from .db import Ledger
from .errors import SyncError
from .local_library import read_file
from .models import normalize_isrc
from .storage import atomic_text, private_dir, save_json
from .tidal_sync import MAX_REPORT_AGE

AUDIO_EXTS = {".flac", ".m4a", ".mp4"}
TIDAL_URL = re.compile(r"tidal\.com/(?:browse/)?track/(\d+)")
# Only the interactive PKCE prompt counts; "You are logged in." is Tidaler's success line.
LOGIN_MARKERS = ("paste 'ooops'", "login.tidal.com/authorize", "not logged in", "login_pkce")
UNAVAILABLE = re.compile(r"Media not found \(ID: (\d+)\)")
MAX_ATTEMPTS = 2
GUI_MARKER = "tidaler.app/Contents/MacOS/tidaler"
REDACT = re.compile(r"(?i)(bearer\s+)?[A-Za-z0-9_\-]{40,}")
DONE_STATUSES = ("downloaded", "converted", "imported")
URL_TAG_KEYS = ("URL", "\xa9url", "WOAF", "url")


def tidal_id_from_url(text: str | None) -> str | None:
    match = TIDAL_URL.search(text or "")
    return match[1] if match else None


@dataclass(frozen=True)
class TidalerSettings:
    config_dir: Path
    download_base_path: Path
    format_track: str

    @staticmethod
    def default_config_dir() -> Path:
        # Same rule Tidaler itself uses, so the CLI and the GUI app share one config.
        xdg = os.environ.get("XDG_CONFIG_HOME")
        return Path(xdg) / "tidaler" if xdg else Path.home() / ".config" / "tidaler"

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "TidalerSettings":
        directory = config_dir or cls.default_config_dir()
        path = directory / "settings.json"
        if not path.is_file():
            raise SyncError(
                "Tidaler settings not found. Run `tidaler` once (or the GUI app) so it "
                "creates its configuration, then retry."
            )
        try:
            raw = json.loads(path.read_text())
            base = str(raw["download_base_path"])  # verbatim: may end in a space
            template = str(raw["format_track"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SyncError("Tidaler settings.json is unreadable or incomplete.") from exc
        return cls(directory, Path(base).expanduser(), template)

    @property
    def tracks_dir(self) -> Path:
        parts = Path(self.format_track).parts
        return self.download_base_path / parts[0] if len(parts) > 1 else self.download_base_path

    @property
    def token_path(self) -> Path:
        return self.config_dir / "token.json"

    def token_state(self) -> str:
        return "present" if self.token_path.is_file() else "missing"


def gui_running(ps_output: str) -> bool:
    return any(GUI_MARKER in line for line in ps_output.splitlines())


def _ps_output() -> str:
    try:
        return subprocess.run(
            ["ps", "-axo", "args="], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _read_tag(tags: Any, keys: tuple[str, ...]) -> str:
    if tags is None:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except (KeyError, ValueError, TypeError):
            continue
        if value is None:
            continue
        if hasattr(value, "text"):
            value = value.text
        if hasattr(value, "url"):
            value = value.url
        if isinstance(value, list | tuple):
            value = value[0] if value else ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        text = str(value).strip()
        if text:
            return text
    return ""


def _probe(path: Path) -> dict[str, Any] | None:
    """Tags and length of an audio file, or None if it cannot be parsed."""
    try:
        audio = mutagen.File(path)
    except Exception:  # noqa: BLE001 - partial or foreign files are expected here
        return None
    if audio is None:
        return None
    length = getattr(audio.info, "length", None)
    return {
        "path": str(path),
        "tidal_id": tidal_id_from_url(_read_tag(audio.tags, URL_TAG_KEYS)),
        "isrc": normalize_isrc(_read_tag(audio.tags, ("ISRC", "TSRC", "isrc"))),
        "duration_ms": round(length * 1000) if length else None,
        "mtime": path.stat().st_mtime,
    }


def _within(expected_ms: int | None, actual_ms: int | None) -> bool:
    if expected_ms is None or actual_ms is None:
        return False
    return abs(expected_ms - actual_ms) <= max(3000, expected_ms * 0.01)


def _audio_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")
    )


def _upsert(
    c: Any,
    track_id: int,
    tidal_id: str,
    run_id: str | None,
    status: str,
    origin: str,
    *,
    flac_path: str | None = None,
    verified_at: float | None = None,
    bump: bool = False,
) -> None:
    c.execute(
        """INSERT INTO downloads(track_id,tidal_id,run_id,status,origin,attempts,flac_path,
        aiff_path,verified_at) VALUES(?,?,?,?,?,?,?,NULL,?)
        ON CONFLICT(track_id,tidal_id) DO UPDATE SET run_id=excluded.run_id,
        status=excluded.status, origin=excluded.origin,
        attempts=downloads.attempts + ?,
        flac_path=COALESCE(excluded.flac_path, downloads.flac_path),
        verified_at=COALESCE(excluded.verified_at, downloads.verified_at)""",
        (
            track_id,
            tidal_id,
            run_id,
            status,
            origin,
            1 if bump else 0,
            flac_path,
            verified_at,
            1 if bump else 0,
        ),
    )


def _stage(c: Any, track_id: int, stage: str, status: str, now: float) -> None:
    c.execute(
        """INSERT INTO sync_status VALUES(?,?,?,?,1) ON CONFLICT(track_id,stage) DO UPDATE
        SET status=excluded.status, updated=excluded.updated, attempts=sync_status.attempts+1""",
        (track_id, stage, status, now),
    )


# ---------------------------------------------------------------------------- plan


def download_plan(
    ledger: Ledger,
    mappings: dict[str, str],
    country: str,
    settings: TidalerSettings,
    *,
    limit: int,
    out_dir: Path | None = None,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Tracks matched on TIDAL that nothing local accounts for, grouped by TIDAL ID.

    ``only`` restricts the request list to those TIDAL IDs (for careful first runs).
    """
    c = ledger.connection
    groups: dict[str, dict[str, Any]] = {}
    excluded: dict[str, int] = {}
    skipped_playlists: list[dict[str, str]] = []
    for playlist_id, group in mappings.items():
        latest = ledger.latest_complete_report(playlist_id)
        if not latest or time.time() - latest[1] > MAX_REPORT_AGE:
            skipped_playlists.append({"id": playlist_id, "reason": "no fresh complete report"})
            continue
        report = latest[2]
        if report["playlist"].get("group") != group:
            skipped_playlists.append({"id": playlist_id, "reason": "group changed"})
            continue
        for row in report["rows"]:
            src, match = row["source"], row["match"]
            if src and match["status"] in ("local", "skipped"):
                excluded["decided"] = excluded.get("decided", 0) + 1
                continue
            if not src or match["status"] != "matched" or not match.get("selected_id"):
                continue
            tidal_id = str(match["selected_id"])
            track_row = c.execute(
                "SELECT track_id FROM service_tracks WHERE service='spotify' AND service_id=?",
                (src["id"],),
            ).fetchone()
            if not track_row:
                continue
            entry = groups.setdefault(
                tidal_id,
                {
                    "tidal_id": tidal_id,
                    "track_ids": [],
                    "spotify_ids": [],
                    "title": src["title"],
                    "artists": list(src["artists"]),
                    "isrc": src.get("isrc"),
                    "duration_ms": src.get("duration_ms"),
                    "playlists": [],
                },
            )
            if track_row[0] not in entry["track_ids"]:
                entry["track_ids"].append(track_row[0])
                entry["spotify_ids"].append(src["id"])
            entry["playlists"].append(f"{group} / {report['playlist']['name']}")
    for entry in groups.values():
        meta = c.execute(
            "SELECT metadata_json FROM service_tracks WHERE service='tidal' AND service_id=?",
            (entry["tidal_id"],),
        ).fetchone()
        if meta:
            data = json.loads(meta[0])
            entry["duration_ms"] = data.get("duration_ms") or entry["duration_ms"]
            entry["isrc"] = normalize_isrc(data.get("isrc")) or entry["isrc"]

    now = time.time()
    requests: list[dict[str, Any]] = []
    suspect: list[dict[str, Any]] = []

    def exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    # Files Tidaler already holds (including ones we moved to imported/) are read first.
    known: dict[str, dict[str, Any]] = {}
    prescan_paths: list[str] = []
    for directory in (settings.tracks_dir, settings.tracks_dir / "imported"):
        for path in _audio_files(directory):
            prescan_paths.append(str(path))
            info = _probe(path)
            if info and info["tidal_id"] and info["tidal_id"] not in known:
                known[info["tidal_id"]] = info

    with c:
        for tidal_id, entry in groups.items():
            on_disk, decided = False, 0
            for track_id, spotify_id in zip(entry["track_ids"], entry["spotify_ids"], strict=True):
                status = c.execute(
                    "SELECT status FROM sync_status WHERE track_id=? "
                    "AND stage='local_file_detected'",
                    (track_id,),
                ).fetchone()
                if status and status[0] in ("found", "possibly_found"):
                    on_disk = True
                decision = c.execute(
                    "SELECT decision FROM manual_matches WHERE spotify_id=? AND country=?",
                    (spotify_id, country),
                ).fetchone()
                if decision and decision[0] in ("local", "skip"):
                    decided += 1
            history = c.execute(
                "SELECT status, attempts FROM downloads WHERE tidal_id=?", (tidal_id,)
            ).fetchall()
            if on_disk:
                exclude("on_disk")
                continue
            if decided == len(entry["track_ids"]):
                exclude("decided")
                continue
            if history:
                statuses = {str(r[0]) for r in history}
                attempts = max(int(r[1] or 0) for r in history)
                dl_status = next((s for s in DONE_STATUSES if s in statuses), None) or (
                    "review"
                    if "review" in statuses
                    else "unavailable"
                    if "unavailable" in statuses
                    else "failed"
                )
                if dl_status in DONE_STATUSES:
                    exclude("downloaded")
                    continue
                if dl_status == "review":
                    exclude("review")
                    continue
                if dl_status == "unavailable":
                    exclude("unavailable")
                    continue
                if dl_status in ("failed", "corrupt") and attempts >= MAX_ATTEMPTS:
                    exclude("attempts_exhausted")
                    continue
            info = known.get(tidal_id)
            if info:
                if _within(entry["duration_ms"], info["duration_ms"]):
                    for track_id in entry["track_ids"]:
                        _upsert(
                            c,
                            track_id,
                            tidal_id,
                            None,
                            "downloaded",
                            "preexisting",
                            flac_path=info["path"],
                            verified_at=now,
                        )
                        _stage(c, track_id, "downloaded", "downloaded", now)
                    exclude("preexisting")
                else:
                    suspect.append(
                        {
                            "tidal_id": tidal_id,
                            "path": info["path"],
                            "file_ms": info["duration_ms"],
                            "expected_ms": entry["duration_ms"],
                        }
                    )
                    exclude("suspect_preexisting")
                continue
            requests.append(entry)

    unknown_ids: list[str] = []
    if only is not None:
        present = {e["tidal_id"] for e in requests}
        unknown_ids = sorted(i for i in only if i not in present)
        kept = [e for e in requests if e["tidal_id"] in only]
        if len(kept) != len(requests):
            excluded["not_selected"] = len(requests) - len(kept)
        requests = kept
    deferred = max(0, len(requests) - limit)
    requests = requests[:limit]
    run_id = str(uuid.uuid4())
    plan: dict[str, Any] = {
        "run_id": run_id,
        "created_at": now,
        "requests": requests,
        "excluded": excluded,
        "deferred_by_limit": deferred,
        "unknown_ids": unknown_ids,
        "suspect_preexisting": suspect,
        "skipped_playlists": skipped_playlists,
        "prescan_paths": prescan_paths,
        "tracks_dir": str(settings.tracks_dir),
    }
    directory = out_dir or (ledger.path.parent / "downloads")
    private_dir(directory)
    list_file = directory / f"{run_id}.txt"
    atomic_text(
        list_file,
        "".join(f"https://tidal.com/browse/track/{e['tidal_id']}\n" for e in requests),
    )
    plan["list_file"] = str(list_file)
    plan["plan_file"] = str(directory / f"{run_id}.json")
    save_json(Path(plan["plan_file"]), plan)
    return plan


# ----------------------------------------------------------------------------- run


def run_tidaler(
    plan: dict[str, Any],
    settings: TidalerSettings,
    tidaler_bin: Path,
    log_dir: Path,
    *,
    ps_output: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run `tidaler dl -l FILE` once, non-interactively, and keep a redacted log."""
    if not plan["requests"]:
        return {
            "run_id": plan["run_id"],
            "skipped": True,
            "started_at": time.time(),
            "returncode": 0,
            "timed_out": False,
            "login_required": False,
            "unavailable_ids": [],
            "log_file": None,
        }
    if gui_running(_ps_output() if ps_output is None else ps_output):
        raise SyncError(
            "Tidaler's GUI app is running. Quit it before automated downloads so "
            "the two cannot fight over the session token."
        )
    if settings.token_state() == "missing":
        raise SyncError(
            "No Tidaler session found. Run `tidaler login` once in the Tidaler venv, then retry."
        )
    if "{track_id}" not in settings.format_track:
        raise SyncError(
            "Tidaler's format_track must contain {track_id} so filenames cannot "
            "collide. Run tools/setup-tidaler.sh."
        )
    if not Path(tidaler_bin).is_file() or not os.access(tidaler_bin, os.X_OK):
        raise SyncError("Tidaler CLI not found. Run tools/setup-tidaler.sh.")
    n = len(plan["requests"])
    limit_s = timeout if timeout is not None else 120 + 60 * n
    private_dir(log_dir)
    started = time.time()
    proc = subprocess.Popen(
        [str(tidaler_bin), "dl", "-l", plan["list_file"]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=limit_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill Tidaler and every child (ffmpeg extraction) in its process group.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=10)
                break
            except subprocess.TimeoutExpired:
                continue
        output = ""
        try:
            output, _ = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError):
            pass
    output = output or ""
    log_file = log_dir / f"tidaler-{plan['run_id']}.log"
    atomic_text(log_file, REDACT.sub(lambda m: "[redacted]" if len(m[0]) >= 40 else m[0], output))
    lowered = output.lower()
    return {
        "run_id": plan["run_id"],
        "skipped": False,
        "started_at": started,
        "finished_at": time.time(),
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "login_required": any(m.lower() in lowered for m in LOGIN_MARKERS),
        "unavailable_ids": sorted(set(UNAVAILABLE.findall(output))),
        "log_file": str(log_file),
    }


# -------------------------------------------------------------------------- verify


def verify_downloads(
    ledger: Ledger,
    plan: dict[str, Any],
    settings: TidalerSettings,
    run: dict[str, Any],
    *,
    quiet_seconds: float = 30,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Decide, from files and tags only, which requested tracks really arrived."""
    expected = {e["tidal_id"]: e for e in plan["requests"]}
    by_isrc = {e["isrc"]: e for e in plan["requests"] if e.get("isrc")}
    prescan = set(plan.get("prescan_paths", []))
    started = float(run.get("started_at", plan["created_at"])) - 1
    c = ledger.connection
    now = time.time()
    counts = {
        "downloaded": 0,
        "failed": 0,
        "corrupt": 0,
        "sibling_release": 0,
        "unexpected": 0,
        "unreadable": 0,
    }
    details: dict[str, list[dict[str, Any]]] = {
        "corrupt": [],
        "sibling_release": [],
        "unexpected": [],
        "unreadable": [],
        "failed": [],
    }
    candidates = [
        p
        for p in _audio_files(settings.tracks_dir)
        if str(p) not in prescan and p.stat().st_mtime >= started
    ]
    # Wait until nothing is still being written (an orphaned extractor, a slow flush).
    for _ in range(10):
        newest = max((p.stat().st_mtime for p in candidates if p.exists()), default=0)
        remaining = quiet_seconds - (time.time() - newest)
        if newest and remaining > 0:
            sleep(min(remaining, quiet_seconds))
        else:
            break
    verified: set[str] = set()
    with c:
        for path in candidates:
            info = _probe(path)
            if info is None:
                counts["unreadable"] += 1
                details["unreadable"].append({"path": str(path)})
                continue
            entry = expected.get(info["tidal_id"] or "")
            if entry:
                if _within(entry["duration_ms"], info["duration_ms"]):
                    for track_id in entry["track_ids"]:
                        _upsert(
                            c,
                            track_id,
                            entry["tidal_id"],
                            run["run_id"],
                            "downloaded",
                            "tool",
                            flac_path=str(path),
                            verified_at=now,
                        )
                        _stage(c, track_id, "downloaded", "downloaded", now)
                    verified.add(entry["tidal_id"])
                    counts["downloaded"] += 1
                else:
                    for track_id in entry["track_ids"]:
                        _upsert(
                            c,
                            track_id,
                            entry["tidal_id"],
                            run["run_id"],
                            "corrupt",
                            "tool",
                            flac_path=str(path),
                            bump=True,
                        )
                    verified.add(entry["tidal_id"])  # do not also count it as missing
                    counts["corrupt"] += 1
                    details["corrupt"].append(
                        {
                            "tidal_id": entry["tidal_id"],
                            "path": str(path),
                            "file_ms": info["duration_ms"],
                            "expected_ms": entry["duration_ms"],
                        }
                    )
                continue
            sibling = by_isrc.get(info["isrc"] or "")
            if sibling and _within(sibling["duration_ms"], info["duration_ms"]):
                for track_id in sibling["track_ids"]:
                    _upsert(
                        c,
                        track_id,
                        sibling["tidal_id"],
                        run["run_id"],
                        "review",
                        "tool",
                        flac_path=str(path),
                    )
                verified.add(sibling["tidal_id"])
                counts["sibling_release"] += 1
                details["sibling_release"].append(
                    {
                        "expected_id": sibling["tidal_id"],
                        "file_id": info["tidal_id"],
                        "path": str(path),
                    }
                )
                continue
            counts["unexpected"] += 1
            details["unexpected"].append({"path": str(path), "file_id": info["tidal_id"]})
        for tidal_id, entry in expected.items():
            if tidal_id in verified:
                continue
            status = "unavailable" if tidal_id in run.get("unavailable_ids", []) else "failed"
            for track_id in entry["track_ids"]:
                _upsert(c, track_id, tidal_id, run["run_id"], status, "tool", bump=True)
            counts["failed"] += 1
            details["failed"].append(
                {
                    "tidal_id": tidal_id,
                    "title": entry["title"],
                    "artists": entry["artists"],
                    "status": status,
                }
            )
    if run.get("login_required") and not counts["downloaded"]:
        raise SyncError(
            "Tidaler asked for a login, so nothing was downloaded. Run `tidaler login` once "
            "in the Tidaler venv, then rerun."
        )
    return {
        **counts,
        "requested": len(expected),
        "unavailable_ids": list(run.get("unavailable_ids", [])),
        "timed_out": bool(run.get("timed_out")),
        "details": details,
        "run_id": run["run_id"],
    }


# ------------------------------------------------------------------------- convert


def _bits_per_sample(path: Path) -> int:
    return int(FLAC(path).info.bits_per_sample)


def _decoded_md5(ffmpeg_bin: str, path: Path) -> str | None:
    done = subprocess.run(
        [
            ffmpeg_bin,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a",
            "-af",
            "aformat=sample_fmts=s32",
            "-f",
            "md5",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else None


def _write_aiff_tags(flac: FLAC, aiff_path: Path, url: str) -> None:
    aiff = AIFF(aiff_path)
    if aiff.tags is None:
        aiff.add_tags()
    tags = aiff.tags
    assert tags is not None

    def first(key: str) -> str:
        value = flac.get(key)
        return str(value[0]) if value else ""

    pairs = [
        (TIT2, first("TITLE")),
        (TPE1, first("ARTIST")),
        (TALB, first("ALBUM")),
        (TPE2, first("ALBUMARTIST")),
        (TDRC, first("DATE")),
        (TSRC, first("ISRC")),
    ]
    for frame, text in pairs:
        if text:
            tags.add(frame(encoding=3, text=text))
    number, total = first("TRACKNUMBER"), first("TRACKTOTAL")
    if number:
        tags.add(TRCK(encoding=3, text=f"{number}/{total}" if total else number))
    if url:
        tags.add(TXXX(encoding=3, desc="TIDAL URL", text=url))
    if first("UPC"):
        tags.add(TXXX(encoding=3, desc="UPC", text=first("UPC")))
    for picture in flac.pictures[:1]:
        # Latin-1 with an empty description: Music.app cannot read an ID3v2.3 APIC whose
        # description is UTF-16 (what encoding=3 becomes in v2.3), and then shows no cover.
        tags.add(
            APIC(
                encoding=0,
                mime=picture.mime,
                type=picture.type,
                desc="",
                data=picture.data,
            )
        )
    aiff.save(v2_version=3)


def convert_downloads(
    ledger: Ledger, aiff_dir: Path, ffmpeg_bin: str, *, max_rate: int = 48000
) -> dict[str, Any]:
    """FLAC → AIFF for every verified download that has no AIFF yet. Never overwrites."""
    c = ledger.connection
    rows = c.execute(
        "SELECT flac_path, group_concat(track_id), group_concat(tidal_id) FROM downloads "
        "WHERE status='downloaded' AND aiff_path IS NULL AND flac_path IS NOT NULL "
        "GROUP BY flac_path ORDER BY flac_path"
    ).fetchall()
    private_dir(aiff_dir)
    counts = {"converted": 0, "failed": 0}
    failures: list[dict[str, str]] = []
    now = time.time()
    for flac_path, track_ids, _tidal_ids in rows:
        source = Path(flac_path)
        target = aiff_dir / (source.stem + ".aiff")
        part = aiff_dir / (source.stem + ".aiff.part")
        reason = _convert_one(source, target, part, ffmpeg_bin, max_rate)
        if reason:
            counts["failed"] += 1
            failures.append({"flac": flac_path, "reason": reason})
            if part.exists():
                part.unlink()
            continue
        with c:
            c.execute(
                "UPDATE downloads SET status='converted', aiff_path=? WHERE flac_path=? "
                "AND status='downloaded'",
                (str(target), flac_path),
            )
            for track_id in str(track_ids).split(","):
                _stage(c, int(track_id), "converted", "converted", now)
        counts["converted"] += 1
    return {**counts, "failures": failures, "aiff_dir": str(aiff_dir)}


def _convert_one(
    source: Path, target: Path, part: Path, ffmpeg_bin: str, max_rate: int
) -> str | None:
    """Returns None on success, otherwise the reason the file was left unconverted."""
    if not source.is_file():
        return "source FLAC missing"
    if target.exists():
        return "target AIFF already exists; not overwriting"
    if source.suffix.lower() != ".flac":
        return "only FLAC sources are converted"
    try:
        flac = FLAC(source)
        bits = _bits_per_sample(source)
        rate = int(flac.info.sample_rate)
    except Exception as exc:  # noqa: BLE001
        return f"cannot read FLAC: {type(exc).__name__}"
    # The user's cap is 24-bit / 48 kHz: 32-bit sources are reduced to 24-bit, like rates
    # above 48 kHz are resampled. Anything else unusual fails loudly instead of guessing.
    codec = {16: "pcm_s16be", 24: "pcm_s24be", 32: "pcm_s24be"}.get(bits)
    if not codec:
        return f"unsupported bit depth {bits}"
    resample = rate > max_rate
    reduced = resample or bits > 24
    base = [
        ffmpeg_bin,
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a",
        "-map_metadata",
        "-1",
        "-c:a",
        codec,
    ]
    attempts = (
        [
            base + ["-af", f"aresample={max_rate}:resampler=soxr:precision=28"],
            base + ["-ar", str(max_rate)],
        ]
        if resample
        else [base]
    )
    ok = False
    for cmd in attempts:
        done = subprocess.run(
            cmd + ["-f", "aiff", str(part)],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if done.returncode == 0 and part.exists():
            ok = True
            break
    if not ok:
        return "ffmpeg conversion failed"
    url = str(flac.get("URL", [""])[0]) if flac.get("URL") else ""
    try:
        _write_aiff_tags(flac, part, url)
    except Exception as exc:  # noqa: BLE001
        return f"tag write failed: {type(exc).__name__}"
    back = read_file(part, part.parent)
    if back is None:
        return "converted file is not readable"
    isrc = normalize_isrc(str(flac.get("ISRC", [""])[0]) if flac.get("ISRC") else "")
    if isrc and back.isrc != isrc:
        return "ISRC did not survive conversion"
    tagged_url = _read_tag(AIFF(part).tags, ("TXXX:TIDAL URL",))
    if url and tidal_id_from_url(tagged_url) != tidal_id_from_url(url):
        return "TIDAL URL did not survive conversion"
    source_ms = round(flac.info.length * 1000)
    if reduced:
        if back.duration_ms is None or abs(back.duration_ms - source_ms) > 50:
            return "length changed during resampling or bit-depth reduction"
    else:
        a, b = _decoded_md5(ffmpeg_bin, source), _decoded_md5(ffmpeg_bin, part)
        if not a or a != b:
            return "decoded audio differs from the FLAC"
    part.rename(target)
    return None
