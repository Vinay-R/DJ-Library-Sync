"""One command for the whole pipeline, stage by stage, stopping only on hard failures.

Order matters: Spotify is read first, the TIDAL mirrors are updated, local files and
Music.app are indexed so owned recordings are excluded, only then are downloads
requested, and Music.app placement comes last. Each write surface has its own flag;
a stage with partial results (deferred playlists, a failed download) is "soft" and
the run continues. An exception is "hard" and the remaining stages are skipped.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import atomic_text, save_json

STAGE_ORDER = (
    "spotify_sync",
    "tidal_sync",
    "local",
    "music_index",
    "download",
    "music_report",
    "music_apply",
)


@dataclass
class StageResult:
    name: str
    status: str  # ok | soft | hard | skipped
    summary: dict[str, Any]
    error: str | None
    seconds: float


def run_stages(stages: list[tuple[str, Callable[[], dict[str, Any]]]]) -> list[StageResult]:
    results: list[StageResult] = []
    stopped = False
    for name, fn in stages:
        if stopped:
            results.append(
                StageResult(name, "skipped", {}, "not run: an earlier stage stopped the run", 0.0)
            )
            continue
        started = time.time()
        try:
            summary = fn() or {}
        except Exception as exc:  # noqa: BLE001 - any failure is a hard stop, reported by type
            results.append(
                StageResult(
                    name,
                    "hard",
                    {},
                    f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                    time.time() - started,
                )
            )
            stopped = True
            continue
        status = "soft" if summary.get("soft_issues") else "ok"
        results.append(StageResult(name, status, summary, None, time.time() - started))
    return results


def exit_code(results: list[StageResult]) -> int:
    return 0 if all(r.status == "ok" for r in results) else 2


def write_run_report(directory: Path, results: list[StageResult], flags: dict[str, Any]) -> Path:
    stem = directory / f"run-{int(time.time())}"
    payload = {
        "flags": flags,
        "exit_code": exit_code(results),
        "stages": [
            {
                "name": r.name,
                "status": r.status,
                "seconds": round(r.seconds, 1),
                "error": r.error,
                "summary": r.summary,
            }
            for r in results
        ],
    }
    save_json(stem.with_suffix(".json"), payload)
    lines = [
        "# Pipeline run",
        "",
        "Flags: " + ", ".join(f"{k}={v}" for k, v in flags.items()),
        "",
        "| Stage | Status | Seconds | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        notes = r.error or "; ".join(str(x) for x in r.summary.get("soft_issues", [])) or ""
        lines.append(f"| {r.name} | {r.status} | {r.seconds:.0f} | {notes.replace('|', '/')} |")
    lines.append("")
    atomic_text(stem.with_suffix(".md"), "\n".join(lines))
    return stem.with_suffix(".md")
