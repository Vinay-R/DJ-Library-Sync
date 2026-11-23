import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from . import energy
from .acquire import (
    TidalerSettings,
    _ps_output,
    convert_downloads,
    download_plan,
    gui_running,
    run_tidaler,
    verify_downloads,
)
from .artwork import repair_artwork
from .auth import TokenProvider
from .config import DESTINATIONS, Config, playlist_id
from .db import Ledger
from .errors import SyncError
from .http import ReadClient
from .local_library import index_files, match_local, scan
from .logging_setup import configure_logging
from .music_app import MusicApp, build_report, confirmed_folders, index_music
from .music_apply import apply_music
from .orchestrate import exit_code, run_stages, write_run_report
from .pipeline import run_mapped, run_playlist
from .reports import (
    mapping_worksheet,
    write_download_report,
    write_local_report,
    write_music_report,
    write_report,
)
from .review import import_decisions, review_items
from .spotify_client import SpotifyClient
from .storage import atomic_text, process_lock, save_json
from .tidal_client import TidalClient
from .tidal_sync import load_mapped_plans, sync_mapped_playlists
from .tidal_test_playlist import MAX_ADD_BATCH, TestPlaylistAPI, apply_test_plan, load_plan


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Read-only Spotify→TIDAL matching, plus explicitly applied writes to "
        "one new TIDAL test playlist."
    )
    root.add_argument("--config", type=Path, default=Path("config.yaml"))
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check local settings without network calls or secret output")
    auth = sub.add_parser(
        "auth", help="Authorize API access; read-only unless --write is specified"
    )
    auth.add_argument("service", choices=["spotify", "tidal"])
    auth.add_argument(
        "--user", action="store_true", help="TIDAL user OAuth instead of app catalog token"
    )
    auth.add_argument("--write", action="store_true", help="TIDAL test-playlist write consent")
    test = sub.add_parser(
        "test-playlist", help="Preview or apply a new unlisted TIDAL test playlist"
    )
    test.add_argument("--run-id", required=True, help="Completed matching report run ID")
    mode = test.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Explicitly create/add to this new test")
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    tidal_sync = sub.add_parser(
        "tidal-sync",
        help="Preview or apply additions to one new unlisted TIDAL playlist per mapped playlist",
    )
    tidal_mode = tidal_sync.add_mutually_exclusive_group()
    tidal_mode.add_argument(
        "--apply", action="store_true", help="Create missing playlists and add missing tracks"
    )
    tidal_mode.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    sub.add_parser("playlists", help="List Spotify playlists and mapping review queue")
    run = sub.add_parser(
        "run", help="Whole pipeline: sync, TIDAL mirrors, local+Music.app index, download, place"
    )
    run.add_argument(
        "--apply-tidal", action="store_true", help="Add missing tracks to TIDAL mirrors"
    )
    run.add_argument("--apply-download", action="store_true", help="Run Tidaler for missing tracks")
    run.add_argument(
        "--apply-music", action="store_true", help="Write playlists/imports to Music.app"
    )
    run.add_argument(
        "--archive-imported",
        action="store_true",
        help="Move tool-downloaded originals after import",
    )
    run.add_argument(
        "--limit", type=int, help="Maximum downloads this run (config: download_limit)"
    )
    sync = sub.add_parser(
        "sync", help="Read-only: match Spotify playlist items against the TIDAL catalog"
    )
    target = sync.add_mutually_exclusive_group(required=True)
    target.add_argument("--playlist", type=playlist_id, help="One playlist ID, URI or URL")
    target.add_argument(
        "--mapped", action="store_true", help="Every playlist mapped in config.yaml (complete)"
    )
    sync.add_argument(
        "--full", action="store_true", help="Read the complete playlist, not the first items"
    )
    sync.add_argument("--source", choices=["spotify"], default="spotify")
    sync.add_argument(
        "--limit", type=int, choices=range(1, 11), help="First-items sample size (default 10)"
    )
    sync.add_argument("--dry-run", action="store_true", default=True)
    sync.add_argument("--refresh", action="store_true", help="Ignore the 24-hour match cache")
    sync.add_argument(
        "--retry-review",
        action="store_true",
        help="Search again for cached tracks that are not confident matches",
    )
    local = sub.add_parser("local", help="Read-only check of local audio files (never modified)")
    local_sub = local.add_subparsers(dest="local_command", required=True)
    local_scan = local_sub.add_parser("scan", help="Index tags and lengths of local audio files")
    local_scan.add_argument(
        "--dir", type=Path, action="append", help="Folder to scan (default: music_dirs in config)"
    )
    local_sub.add_parser("match", help="Report which mapped Spotify tracks are already on disk")
    download = sub.add_parser(
        "download", help="Plan (default) or run Tidaler for TIDAL matches that are not on disk"
    )
    download.add_argument(
        "--apply",
        action="store_true",
        help="Run Tidaler, verify the files by tags, convert to AIFF",
    )
    download.add_argument(
        "--limit", type=int, help="Maximum tracks this run (config: download_limit)"
    )
    download.add_argument(
        "--only", help="Comma-separated TIDAL track IDs to restrict this run to (first runs)"
    )
    sub.add_parser("convert", help="Convert verified FLAC downloads to tagged AIFF (no network)")
    music = sub.add_parser("music", help="Read-only Music.app index and reconciliation report")
    music_sub = music.add_subparsers(dest="music_command", required=True)
    music_index = music_sub.add_parser("index", help="Copy playlists, tracks and memberships")
    music_index.add_argument(
        "--confirm-folders",
        action="store_true",
        help="Store the persistent IDs of Nu DJ Vibes and Nu Vibes Vibes as the managed folders",
    )
    music_sub.add_parser("report", help="Compare mapped Spotify playlists with Music.app")
    music_apply = music_sub.add_parser(
        "apply", help="Create playlists, add tracks and import AIFFs inside the managed folders"
    )
    music_apply.add_argument(
        "--apply", action="store_true", help="Write to Music.app (default: dry run)"
    )
    music_apply.add_argument("--playlist", help="Restrict to one Spotify playlist ID (ramp runs)")
    music_apply.add_argument(
        "--max-writes", type=int, help="Cap track adds and imports this run (ramp runs)"
    )
    music_apply.add_argument(
        "--archive-imported",
        action="store_true",
        help="After a verified copy-import, move tool-downloaded FLAC/AIFF originals to imported/",
    )
    music_artwork = music_sub.add_parser(
        "artwork", help="Make covers of the tool's own imports show in Music.app"
    )
    music_artwork.add_argument(
        "--apply", action="store_true", help="Fix files and Music.app (default: dry run)"
    )
    music_artwork.add_argument("--limit", type=int, help="Fix at most N tracks this run")
    energy = sub.add_parser(
        "energy", help="Energy star ratings: analyse, export page, import, apply"
    )
    energy_sub = energy.add_subparsers(dest="energy_command", required=True)
    energy_analyse = energy_sub.add_parser("analyse", help="Measure tracks in managed playlists")
    energy_analyse.add_argument("--limit", type=int, help="Measure at most N tracks this run")
    energy_sub.add_parser("export", help="Write data/energy/energy-data.json for the energy page")
    energy_import = energy_sub.add_parser("import", help="Store stars chosen on the energy page")
    energy_import.add_argument("file", type=Path)
    energy_apply = energy_sub.add_parser("apply", help="Write star ratings to Music.app")
    energy_apply.add_argument(
        "--apply", action="store_true", help="Write to Music.app (default: dry run)"
    )
    energy_apply.add_argument("--limit", type=int, help="Write at most N ratings this run")
    review = sub.add_parser("review", help="Export unresolved matches or import decisions")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_sub.add_parser("export", help="Write data/review/review-data.json (no network)")
    review_import = review_sub.add_parser("import", help="Store decisions from a JSON file")
    review_import.add_argument("file", type=Path)
    return root


def run_pipeline(
    args: argparse.Namespace,
    config: Config,
    client: httpx.Client,
    spotify: SpotifyClient,
    ledger: Ledger,
) -> int:
    reports = config.data_dir / "reports"

    def stage_spotify() -> dict[str, Any]:
        tidal = TidalClient(
            ReadClient(
                "tidal",
                "https://openapi.tidal.com/v2/",
                TokenProvider("tidal", config.data_dir, client, config.tidal_auth),
                client,
                config.request_interval,
            ),
            config.country_code,
        )
        summary = run_mapped(spotify, tidal, ledger, config.playlists, config.country_code, reports)
        issues = [
            f"{e.get('name', e['id'])}: {e['status']}"
            for e in summary["playlists"]
            if e["status"] != "completed"
        ]
        return {**summary["totals"], "soft_issues": issues}

    def stage_tidal() -> dict[str, Any]:
        plans, skipped = load_mapped_plans(ledger, config.playlists)
        api = TestPlaylistAPI(
            TokenProvider("tidal", config.data_dir, client, "user_write"),
            client,
            config.request_interval,
            config.country_code,
            apply=args.apply_tidal,
            max_batch=MAX_ADD_BATCH,
        )
        summary = sync_mapped_playlists(api, plans, config.data_dir, ledger)
        issues = [f"{s['id']}: {s['reason']}" for s in skipped] + [
            f"{e['name']}: {e['status']}"
            for e in summary["playlists"]
            if e["status"] not in ("verified", "preview")
        ]
        return {
            "mode": summary["mode"],
            "playlists": len(summary["playlists"]),
            "post_requests": summary["post_requests"],
            "soft_issues": issues,
        }

    def stage_local() -> dict[str, Any]:
        if not config.music_dirs:
            raise SyncError("Set music_dirs in config.yaml before running the pipeline.")
        files, problems = scan(config.music_dirs)
        index_files(ledger, files)
        summary = match_local(ledger, config.playlists)
        write_local_report(reports, summary)
        return {**summary["totals"], "files_indexed": len(files), "unreadable": len(problems)}

    def stage_music_index() -> dict[str, Any]:
        if not confirmed_folders(ledger):
            raise SyncError("Run `dj-sync music index --confirm-folders` once before `run`.")
        return index_music(ledger, MusicApp(), confirm_folders=False)

    def stage_download() -> dict[str, Any]:
        settings = TidalerSettings.load()
        plan = download_plan(
            ledger,
            config.playlists,
            config.country_code,
            settings,
            limit=args.limit or config.download_limit,
            out_dir=config.data_dir / "downloads",
        )
        if not args.apply_download:
            return {
                "mode": "dry_run",
                "requests": len(plan["requests"]),
                "excluded": plan["excluded"],
                "deferred_by_limit": plan["deferred_by_limit"],
            }
        run = run_tidaler(plan, settings, config.tidaler_bin, config.data_dir / "logs")
        verified = verify_downloads(ledger, plan, settings, run)
        converted = convert_downloads(ledger, config.aiff_dir, config.ffmpeg_bin)
        write_download_report(reports, plan, verified, converted)
        issues = [
            f"{verified[k]} {k}"
            for k in ("failed", "corrupt", "sibling_release", "unexpected")
            if verified[k]
        ]
        if verified["timed_out"]:
            issues.append("Tidaler timed out")
        if converted["failed"]:
            issues.append(f"{converted['failed']} conversion failures")
        return {
            "mode": "applied",
            "requested": verified["requested"],
            "downloaded": verified["downloaded"],
            "converted": converted["converted"],
            "soft_issues": issues,
        }

    def stage_music_report() -> dict[str, Any]:
        report = build_report(ledger, config.playlists)
        write_music_report(reports, report)
        return {"totals": report["totals"]}

    def stage_music_apply() -> dict[str, Any]:
        outcome = apply_music(
            ledger,
            MusicApp(),
            config.playlists,
            config.data_dir,
            apply=args.apply_music,
            archive=args.archive_imported,
        )
        save_json(reports / f"music-apply-{int(time.time())}.json", outcome)
        return {
            "mode": outcome["mode"],
            "writes_performed": outcome["writes_performed"],
            "archived": outcome["archived"],
            "soft_issues": [
                f"skipped: {p['name']}" for p in outcome["playlists"] if p["status"] == "skipped"
            ],
        }

    stages = [
        ("spotify_sync", stage_spotify),
        ("tidal_sync", stage_tidal),
        ("local", stage_local),
        ("music_index", stage_music_index),
        ("download", stage_download),
        ("music_report", stage_music_report),
        ("music_apply", stage_music_apply),
    ]
    flags = {
        "apply_tidal": args.apply_tidal,
        "apply_download": args.apply_download,
        "apply_music": args.apply_music,
        "archive_imported": args.archive_imported,
    }
    results = run_stages(stages)
    path = write_run_report(reports, results, flags)
    print(
        json.dumps(
            {
                "stages": {r.name: r.status for r in results},
                "errors": {r.name: r.error for r in results if r.status == "hard"},
                "report": str(path.resolve()),
                "exit_code": exit_code(results),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return exit_code(results)


def discovery_entry(playlist: dict[str, Any], me: str, mappings: dict[str, str]) -> dict[str, Any]:
    owner = playlist.get("owner") or {}
    # Spotify renamed the playlist "tracks" summary to "items" in 2026; accept both.
    summary = playlist.get("items") or playlist.get("tracks") or {}
    total = summary.get("total") if isinstance(summary, dict) else None
    group = mappings.get(playlist["id"])
    return {
        "id": playlist["id"],
        "name": playlist.get("name", ""),
        "owner": owner.get("display_name") or owner.get("id"),
        "readable": owner.get("id") == me or bool(playlist.get("collaborative")),
        "items": total if isinstance(total, int) else None,
        "group": group,
        "destination": DESTINATIONS.get(group or ""),
        "review_required": group is None,
    }


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    configure_logging()
    args = parser().parse_args(argv)
    if args.command == "sync" and (args.full or args.mapped) and args.limit is not None:
        print(
            "Error: --limit applies only to the first-items sample; omit it with --full "
            "or --mapped.",
            file=sys.stderr,
        )
        return 2
    try:
        config = Config.load(args.config)
        if args.command == "doctor":
            tidaler_status: dict[str, Any] = {"installed": config.tidaler_bin.is_file()}
            if tidaler_status["installed"]:
                try:
                    version = subprocess.run(
                        [str(config.tidaler_bin), "--version"],
                        capture_output=True,
                        text=True,
                        timeout=120,
                        check=False,
                    )
                    tidaler_status["version"] = version.stdout.strip().splitlines()[-1:]
                except (OSError, subprocess.TimeoutExpired):
                    tidaler_status["version"] = "unknown"
                try:
                    settings = TidalerSettings.load()
                    tidaler_status["track_id_in_filenames"] = "{track_id}" in settings.format_track
                    tidaler_status["session"] = settings.token_state()
                    tidaler_status["tracks_dir"] = str(settings.tracks_dir)
                except SyncError as exc:
                    tidaler_status["settings"] = str(exc)
                tidaler_status["gui_running"] = gui_running(_ps_output())
            tidaler_status["ffmpeg"] = shutil.which(config.ffmpeg_bin) or (
                config.ffmpeg_bin if Path(config.ffmpeg_bin).is_file() else None
            )
            names = ["SPOTIFY_CLIENT_ID", "TIDAL_CLIENT_ID"]
            if config.tidal_auth == "client_credentials":
                names.append("TIDAL_CLIENT_SECRET")
            missing = [n for n in names if not os.environ.get(n)]
            print(
                json.dumps(
                    {
                        "read_only": True,
                        "missing_credentials": missing,
                        "mapped_playlists": len(config.playlists),
                        "tidal_auth": config.tidal_auth,
                        "live_api_access": "not_checked",
                        "tidaler": tidaler_status,
                    },
                    indent=2,
                )
            )
            return 2 if missing else 0
        if args.command == "local":
            with process_lock(config.data_dir):
                ledger = Ledger(config.data_dir / "ledger.sqlite3")
                try:
                    if args.local_command == "scan":
                        roots = tuple(args.dir or config.music_dirs)
                        if not roots or not all(r.is_dir() for r in roots):
                            raise SyncError(
                                "Give an existing folder with --dir, or set music_dirs in config."
                            )
                        files, problems = scan(roots)
                        index_files(ledger, files)
                        sources = Counter(f.metadata_source for f in files)
                        print(
                            json.dumps(
                                {
                                    "files_indexed": len(files),
                                    "metadata_from": dict(sources),
                                    "with_isrc": sum(1 for f in files if f.isrc),
                                    "unreadable": len(problems),
                                    "files_modified": 0,
                                },
                                indent=2,
                            )
                        )
                        if problems:
                            save_json(
                                config.data_dir / "reports" / "local-scan-problems.json", problems
                            )
                        return 0
                    summary = match_local(ledger, config.playlists)
                    path = write_local_report(config.data_dir / "reports", summary)
                    print(
                        json.dumps({**summary["totals"], "report": str(path.resolve())}, indent=2)
                    )
                    return 0
                finally:
                    ledger.close()
        if args.command in ("download", "convert"):
            with process_lock(config.data_dir):
                ledger = Ledger(config.data_dir / "ledger.sqlite3")
                try:
                    if args.command == "convert":
                        converted = convert_downloads(ledger, config.aiff_dir, config.ffmpeg_bin)
                        print(json.dumps(converted, indent=2, ensure_ascii=False))
                        return 0 if not converted["failed"] else 2
                    settings = TidalerSettings.load()
                    limit = args.limit or config.download_limit
                    if not 1 <= limit <= 500:
                        raise SyncError("--limit must be between 1 and 500.")
                    plan = download_plan(
                        ledger,
                        config.playlists,
                        config.country_code,
                        settings,
                        limit=limit,
                        out_dir=config.data_dir / "downloads",
                        only={i.strip() for i in args.only.split(",") if i.strip()}
                        if args.only
                        else None,
                    )
                    preview = {
                        "mode": "dry_run",
                        "requests": len(plan["requests"]),
                        "excluded": plan["excluded"],
                        "deferred_by_limit": plan["deferred_by_limit"],
                        "suspect_preexisting": len(plan["suspect_preexisting"]),
                        "unknown_ids": plan["unknown_ids"],
                        "skipped_playlists": plan["skipped_playlists"],
                        "first_requests": [
                            {
                                "tidal_id": e["tidal_id"],
                                "title": e["title"],
                                "artists": e["artists"],
                            }
                            for e in plan["requests"][:10]
                        ],
                        "list_file": plan["list_file"],
                        "writes_performed": 0,
                    }
                    if not args.apply:
                        print(json.dumps(preview, indent=2, ensure_ascii=False))
                        return 0
                    run = run_tidaler(plan, settings, config.tidaler_bin, config.data_dir / "logs")
                    verified = verify_downloads(ledger, plan, settings, run)
                    converted = convert_downloads(ledger, config.aiff_dir, config.ffmpeg_bin)
                    path = write_download_report(
                        config.data_dir / "reports", plan, verified, converted
                    )
                    summary = {
                        "mode": "applied",
                        "requested": verified["requested"],
                        "downloaded": verified["downloaded"],
                        "failed": verified["failed"],
                        "corrupt": verified["corrupt"],
                        "sibling_release": verified["sibling_release"],
                        "unexpected": verified["unexpected"],
                        "timed_out": verified["timed_out"],
                        "converted": converted["converted"],
                        "convert_failed": converted["failed"],
                        "log_file": run.get("log_file"),
                        "report": str(path.resolve()),
                    }
                    print(json.dumps(summary, indent=2, ensure_ascii=False))
                    clean = (
                        not verified["failed"]
                        and not verified["corrupt"]
                        and not verified["timed_out"]
                        and not converted["failed"]
                    )
                    return 0 if clean else 2
                finally:
                    ledger.close()
        if args.command == "music":
            with process_lock(config.data_dir):
                ledger = Ledger(config.data_dir / "ledger.sqlite3")
                try:
                    if args.music_command == "index":
                        summary = index_music(
                            ledger, MusicApp(), confirm_folders=args.confirm_folders
                        )
                        print(json.dumps(summary, indent=2, ensure_ascii=False))
                        if not args.confirm_folders:
                            print(
                                "Folders found but not stored. Rerun with --confirm-folders "
                                "once the names above are the right folders.",
                                file=sys.stderr,
                            )
                        return 0
                    if args.music_command == "artwork":
                        outcome = repair_artwork(
                            ledger, MusicApp(), config.data_dir, apply=args.apply, limit=args.limit
                        )
                        print(json.dumps(outcome, indent=2, ensure_ascii=False))
                        return 0
                    if args.music_command == "apply":
                        outcome = apply_music(
                            ledger,
                            MusicApp(),
                            config.playlists,
                            config.data_dir,
                            apply=args.apply,
                            archive=args.archive_imported,
                            only={args.playlist} if args.playlist else None,
                            max_writes=args.max_writes,
                        )
                        save_json(
                            config.data_dir / "reports" / f"music-apply-{int(time.time())}.json",
                            outcome,
                        )
                        print(
                            json.dumps(
                                {
                                    "mode": outcome["mode"],
                                    "playlists": {
                                        p["name"]: {
                                            "destination": p["destination"]["how"],
                                            "planned": p["planned"],
                                            "done": p["done"],
                                            "status": p["status"],
                                        }
                                        for p in outcome["playlists"]
                                    },
                                    "writes_performed": outcome["writes_performed"],
                                    "referenced_in_place": outcome["referenced_in_place"],
                                    "archived": outcome["archived"],
                                },
                                indent=2,
                                ensure_ascii=False,
                            )
                        )
                        any_skipped = any(p["status"] == "skipped" for p in outcome["playlists"])
                        return 2 if any_skipped else 0
                    report = build_report(ledger, config.playlists)
                    path = write_music_report(config.data_dir / "reports", report)
                    print(
                        json.dumps(
                            {
                                "totals": report["totals"],
                                "destinations": Counter(
                                    e["destination"]["how"] for e in report["playlists"]
                                ),
                                "report": str(path.resolve()),
                                "writes_performed": 0,
                            },
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                    return 0
                finally:
                    ledger.close()
        if args.command == "energy":
            with process_lock(config.data_dir):
                ledger = Ledger(config.data_dir / "ledger.sqlite3")
                try:
                    if args.energy_command == "analyse":

                        def progress(done: int, total: int) -> None:
                            if done % 50 == 0 or done == total:
                                print(f"analysed {done}/{total}", file=sys.stderr, flush=True)

                        outcome = energy.analyse(
                            ledger, config.ffmpeg_bin, limit=args.limit, progress=progress
                        )
                    elif args.energy_command == "import":
                        outcome = energy.import_choices(ledger, json.loads(args.file.read_text()))
                    elif args.energy_command == "apply":
                        outcome = energy.apply_ratings(
                            ledger, MusicApp(), apply=args.apply, limit=args.limit
                        )
                    else:
                        app = MusicApp()
                        tracks = sorted({m for m, _, _, _ in energy._memberships(ledger)})
                        data = energy.page_data(ledger, app.ratings(tracks))
                        path = config.data_dir / "energy" / "energy-data.json"
                        save_json(path, data)
                        outcome = {
                            "playlists": len(data["playlists"]),
                            "file": str(path.resolve()),
                        }
                    print(json.dumps(outcome, indent=2, ensure_ascii=False))
                    return 0
                finally:
                    ledger.close()
        if args.command == "review":
            with process_lock(config.data_dir):
                ledger = Ledger(config.data_dir / "ledger.sqlite3")
                try:
                    if args.review_command == "export":
                        data = review_items(ledger, config.playlists)
                        path = config.data_dir / "review" / "review-data.json"
                        save_json(path, data)
                        print(
                            json.dumps(
                                {
                                    "needs_review": len(data["items"]),
                                    "auto_resolved": len(data["auto_resolved"]),
                                    "file": str(path.resolve()),
                                },
                                indent=2,
                            )
                        )
                        return 0
                    payload = json.loads(args.file.read_text())
                    decisions = payload.get("decisions") if isinstance(payload, dict) else payload
                    if not isinstance(decisions, list):
                        raise SyncError('Decision file must be a list or {"decisions": [...]}.')
                    counts = import_decisions(ledger, decisions, config.country_code)
                    print(
                        json.dumps(
                            {
                                "imported": counts,
                                "next": "Run sync --mapped, then tidal-sync --apply.",
                            },
                            indent=2,
                        )
                    )
                    return 0
                finally:
                    ledger.close()
        with (
            process_lock(config.data_dir),
            httpx.Client(timeout=30, follow_redirects=False) as client,
        ):
            if args.command == "auth":
                if args.write and args.service != "tidal":
                    raise SyncError("Spotify write authorization is not supported.")
                mode = "user" if args.service == "spotify" or args.user else "client_credentials"
                if args.write:
                    mode = "user_write"
                TokenProvider(args.service, config.data_dir, client, mode).login()
                purpose = "TIDAL test-playlist" if args.write else "Read-only"
                print(f"{purpose} authentication succeeded. Tokens saved privately under data_dir.")
                return 0
            if args.command == "tidal-sync":
                ledger = Ledger(config.data_dir / "ledger.sqlite3")
                try:
                    plans, skipped = load_mapped_plans(ledger, config.playlists)
                    token = TokenProvider("tidal", config.data_dir, client, "user_write")
                    api = TestPlaylistAPI(
                        token,
                        client,
                        config.request_interval,
                        config.country_code,
                        apply=args.apply,
                        max_batch=MAX_ADD_BATCH,
                    )
                    summary = sync_mapped_playlists(api, plans, config.data_dir, ledger)
                finally:
                    ledger.close()
                statuses = [e["status"] for e in summary["playlists"]]
                print(
                    json.dumps(
                        {
                            "mode": summary["mode"],
                            "playlists": dict(Counter(statuses)),
                            "not_planned": skipped,
                            "post_requests": summary["post_requests"],
                            "tracks_added": sum(
                                e.get("added_count", 0) for e in summary["playlists"]
                            ),
                            "would_add": sum(e.get("would_add", 0) for e in summary["playlists"]),
                            "report": str(
                                (
                                    config.data_dir / "reports" / f"tidal-sync-{summary['id']}.md"
                                ).resolve()
                            ),
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                ok = not skipped and all(s in {"verified", "preview"} for s in statuses)
                return 0 if ok else 2
            if args.command == "test-playlist":
                ledger = Ledger(config.data_dir / "ledger.sqlite3")
                try:
                    plan = load_plan(ledger, args.run_id, config.country_code)
                    if not args.apply:
                        print(
                            json.dumps(
                                {"mode": "dry_run", "plan": plan, "writes_performed": 0},
                                indent=2,
                                ensure_ascii=False,
                            )
                        )
                        return 0
                    token = TokenProvider("tidal", config.data_dir, client, "user_write")
                    api = TestPlaylistAPI(
                        token, client, config.request_interval, config.country_code, apply=True
                    )
                    result = apply_test_plan(api, plan, config.data_dir)
                    ledger.record_test_verification(result)
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                    return 0
                finally:
                    ledger.close()
            spotify_token = TokenProvider("spotify", config.data_dir, client)
            spotify = SpotifyClient(
                ReadClient(
                    "spotify",
                    "https://api.spotify.com/v1/",
                    spotify_token,
                    client,
                    config.request_interval,
                )
            )
            ledger = Ledger(config.data_dir / "ledger.sqlite3")
            try:
                if args.command == "run":
                    return run_pipeline(args, config, client, spotify, ledger)
                if args.command == "playlists":
                    playlists = spotify.playlists()
                    me = spotify.user_id()
                    ledger.discover_playlists(playlists, config.playlists)
                    review = [discovery_entry(p, me, config.playlists) for p in playlists]
                    save_json(config.data_dir / "playlist-discovery.json", review)
                    worksheet = config.data_dir / "playlist-mapping-worksheet.yaml"
                    atomic_text(worksheet, mapping_worksheet(review))
                    print(json.dumps(review, indent=2, ensure_ascii=False))
                    print(f"Mapping worksheet: {worksheet.resolve()}", file=sys.stderr)
                    return 0
                tidal_token = TokenProvider("tidal", config.data_dir, client, config.tidal_auth)
                tidal = TidalClient(
                    ReadClient(
                        "tidal",
                        "https://openapi.tidal.com/v2/",
                        tidal_token,
                        client,
                        config.request_interval,
                    ),
                    config.country_code,
                )
                if args.mapped:
                    summary = run_mapped(
                        spotify,
                        tidal,
                        ledger,
                        config.playlists,
                        config.country_code,
                        config.data_dir / "reports",
                        args.refresh,
                        args.retry_review,
                    )
                    summary_path = config.data_dir / "reports" / f"sync-{summary['id']}.md"
                    print(
                        json.dumps(
                            {
                                "playlists": {
                                    e.get("name", e["id"]): e["status"]
                                    for e in summary["playlists"]
                                },
                                "totals": summary["totals"],
                                "report": str(summary_path.resolve()),
                                "writes_performed": 0,
                            },
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                    ok = all(e["status"] == "completed" for e in summary["playlists"])
                    return 0 if ok else 2
                report = run_playlist(
                    spotify,
                    tidal,
                    ledger,
                    args.playlist,
                    config.country_code,
                    config.playlists.get(args.playlist),
                    None if args.full else (args.limit or 10),
                    args.refresh,
                    args.retry_review,
                )
                path = write_report(config.data_dir / "reports", report)
                print(
                    json.dumps(
                        {
                            "counts": report["counts"],
                            "report": str(path.resolve()),
                            "writes_performed": 0,
                        },
                        indent=2,
                    )
                )
                return 2 if report["counts"].get("error") else 0
            finally:
                ledger.close()
    except SyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        print(
            "Error: local state or API response is invalid. Check configuration, permissions "
            "and API compatibility. If applying a test playlist, preserve its saved intent "
            "and rerun the same plan to reconcile any uncertain outcome.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
