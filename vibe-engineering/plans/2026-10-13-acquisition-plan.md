> **Copied on 2026-10-15** from the machine-local plan file the 2026-10-13 session used. The
> plan was approved by the user and built; it is kept verbatim below as history.
>
> **Superseded in some respects (2026-10-13 → 2026-10-14)** — see [../DECISIONS.md](../DECISIONS.md):
> - Item 19's open question was answered: AIFF capped at 24-bit / 48 kHz. Item 2's "1 s length
>   check" was replaced by item 14's decoded-checksum check; 32-bit sources are reduced to
>   24-bit (2026-10-14).
> - Item 9's "store folder IDs in `spotify_playlists.music_persistent_id`" was replaced by
>   item 17's `music_folders` table, as built.
> - Step 4 needed six more live fixes (AppleScript creation, empty playlists, metadata-based
>   import reconciliation, delayed locations, presence by any library copy, file-less entries)
>   and later a membership re-read after imports; see the 2026-10-14 worklog.
> - Step 4 also gained "add back files on disk missing from Music.app" (user request) and the
>   `--playlist` / `--max-writes` ramp limits.

---

# Automated acquisition: Tidaler → AIFF → Music.app

## Context

The ledger already decides where every song belongs: for each mapped Spotify track it holds the matched TIDAL ID, whether a matching file is on disk (1,046 of 2,185), and which Spotify playlists (so which Music.app playlists) it belongs to. Missing is the automated path from "matched on TIDAL, not on disk" (1,065 tracks today, plus 12 with a different version on disk) to "AIFF in Music.app, in the right playlists". The user wants the tool to run Tidaler itself, with no download-queue playlists, and to convert every download to AIFF before it enters Music.app.

User decisions (2026-10-13): move tool-downloaded staging originals to an `imported/` subfolder after a verified import; download the Spotify version of the 12 "other version" tracks; add the TIDAL track ID to Tidaler's filenames via its own `cfg` command; 50 tracks per run; convert FLAC → AIFF before import, capped at 24-bit/48 kHz (44.1 kHz sources stay as they are; FLAC master kept).

Reviewed twice by independent staff-level reviewers; their findings are merged into the audit and steps below.

Verified facts that shape the design:
- Tidaler here is only the GUI app (`/Applications/tidaler.app`, 0.1.8). The CLI must be installed separately: PyPI `tidaler==0.1.8` needs Python >=3.12,<3.14 (Homebrew 3.13 exists; project venv is 3.14) → own venv.
- CLI and GUI share `~/.config/tidaler/`: `settings.json` (download_base_path `~/Music/Tidal-DL/FLAC ` with a trailing space; `format_track` `Tracks/{artist_name} - {track_title}{track_explicit}`; HI_RES_LOSSLESS; extract_flac via `/opt/homebrew/bin/ffmpeg`; skip_existing; 3–5 s delay; concurrency 3; ISRC and TIDAL share URL written into tags) and `token.json` (secret: only its expiry field is ever read; never printed).
- `tidaler dl -l FILE`: one URL per line; exit code 0 even when every URL fails; failures only printed; invalid session → PKCE login that blocks on `input()`.
- Music.app copies and organizes imports into `~/Music/Music/iTunes/iTunes Music/Artist/Album/`. The library is mostly AIFF (7,784) and MP3; conversion to AIFF matches the user's practice.
- 133 AIFF/MP3 files cannot be parsed by mutagen and are invisible to local matching today.
- `local_assets.path` is UNIQUE and `_record()` uses INSERT OR IGNORE, so 29 tracks that share a file with another Spotify ID have no asset row; `_record()` also deletes every unverified asset row on each `local match`.

## Hard rules (unchanged)

Dry-run by default, `--apply` for every write. Never delete, move, overwrite or retag existing library audio. Never touch playlists outside the verified `Nu DJ Vibes` / `Nu Vibes Vibes` folders. Removals stay review-only. Tidaler is invoked only through its public CLI; its token is never read beyond expiry; its settings are changed only once, by its public `cfg` command, with the user's approval. Secrets never reach logs or reports.

## Staff-engineer audit (merged with an independent review)

Ranked by risk. Each item names the mitigation the design adopts.

1. **Wrong attribution of a downloaded file is silent and, under "never delete", permanent.** Partial files (concurrency 3, timeouts), ISRC siblings (several TIDAL IDs per ISRC), and filename collisions can all put the wrong audio under a track's identity. Mitigation: a file counts as downloaded only when its URL tag yields the exact expected TIDAL ID, mutagen parses it, its length is within the matcher's tolerance of the TIDAL duration stored in `service_tracks`, and it has not been modified for 30 s. ISRC+length with a different ID → review item ("sibling release"), never `downloaded`. Anything else → `unexpected`.
2. **Music.app needs AIFF, not FLAC.** Tidaler produces FLAC; the user's library is AIFF. Mitigation: a `convert` stage (ffmpeg, already installed): PCM at the source bit depth and sample rate (`pcm_s16be`/`pcm_s24be`), `-map_metadata 0` with ID3v2 in AIFF, cover kept; verify the AIFF's length matches the FLAC within 1 s and its tags read back; stage `converted`. The FLAC is kept.
3. **Tidaler's exit code proves nothing.** Mitigation: filesystem + tags are the source of truth (item 1); stdout is only mined for "not available"/"login" lines.
4. **Tidaler's duplicate check cannot protect the library.** `skip_existing` compares one exact path in its own folder. Mitigation: the ledger is the only dedupe; selection excludes `local_file_detected`, `possibly_found`, `local`/`skip` decisions and `downloaded`. Filename collisions are removed at the source by adding `[{track_id}]` to `format_track` (user-approved, one-time `tidaler cfg`). Before each run, URL tags of files already in `Tracks/` are read; an expected ID already present is recorded without re-requesting.
5. **The 133 unparseable files are a duplicate risk, and `local scan` cannot see files outside `music_dirs`.** Mitigation: index unreadable files from their folder path as `possibly_found` (excluded from downloads, listed for review), and run the read-only Music.app index *before* downloads so Music.app's own knowledge of every library file (including those 133) excludes tracks the library already has.
6. **State model collisions.** Staging rows would be wiped by `_record()` and the 29 missing asset rows would trigger re-downloads. Mitigation: a dedicated `downloads` table; selection keyed on `sync_status.local_file_detected` and `load_mapped_plans()` output (which already enforces report freshness and confident IDs), never on `local_assets`; rebuild `local_assets` with UNIQUE(track_id, path) in the v6 migration.
7. **Login expiry turns automation into a hang; GUI and CLI race on the token.** Mitigation: stop if `token.json` is missing; expiry alone is not fatal (refresh token renews); always run with `stdin=DEVNULL` so the PKCE prompt fails instantly, and treat a "login" line in the log as "run `tidaler login` once"; refuse to start while the GUI process is running (checked by executable path, not a `pgrep -f` substring, which matched our own shell).
8. **Staging cleanup can orphan a referenced file.** If Music's "copy to media folder" were off, Music would reference the staging AIFF and a move would break it. Mitigation: move only after reading the imported track's `location` and confirming it is under the media folder and not the staging path; otherwise leave the file and report.
9. **Name-based Music.app targeting is unsafe.** "Vibes" (legacy) vs "Nu Vibes Vibes"; "Late Night Vibes" (Spotify, DJ) vs protected "Old Trax". Mitigation: resolve each managed folder once in `music index`, require exactly one match, store persistent IDs in the existing `spotify_playlists.music_persistent_id` column after the user confirms, and make every write guard walk `parent` persistent IDs up to the confirmed folder. Fail closed.
10. **`run` would halt routinely.** `tidal-sync` exits 2 when any playlist is skipped; `sync --mapped` exits 2 on deferred errors. Mitigation: per-stage soft vs hard outcomes; the orchestrator continues past soft ones and stops only on hard ones (401/403/429, exceptions, verification failures).
11. **Rate limiting and blast radius.** Tidaler's 3–5 s delay and concurrency 3 are the effective limiter. Mitigation: cap 50 per run (user's choice), subprocess timeout ≈ 120 s + 60 s × n, first live run 1 track, second 10, then 50; `music apply` stays out of the unattended chain until the earlier stages have run manually for a while.

12. **Shared TIDAL files.** 20 Spotify tracks share a TIDAL ID with another Spotify track (2,074 matched rows, 2,054 distinct IDs). Mitigation: one verified file marks every `track_id` with that `tidal_id`; otherwise the second is re-requested forever and Tidaler's skip masks it.
13. **Orphaned ffmpeg children on timeout.** `subprocess.run(timeout=…)` kills only Tidaler; its `extract_flac` ffmpeg keeps writing while we verify. Mitigation: `start_new_session=True` and kill the whole process group; the 30 s quiescence check stays.
14. **Conversion must preserve tags the scanner can read.** ffmpeg's AIFF muxer drops or renames tags (`ISRC` → `TXXX`). Mitigation: ffmpeg converts audio only; our code writes ID3v2.3 onto the AIFF with mutagen from the FLAC's tags (title, artist, album, `TSRC`, cover, `TXXX:TIDAL URL`) and verifies by reading it back through the scanner's own `read_file`. Bit depth comes from `FLAC.info.bits_per_sample` (16 → `pcm_s16be`, 24 → `pcm_s24be`, else fail), never from ffmpeg's decoded `s32`. Losslessness is proven by comparing decoded-audio checksums (`ffmpeg -f md5`) of FLAC and AIFF. Write to `.part`, rename, refuse an existing target.
15. **Pre-existing files in `Tracks/` must never be moved.** The folder already holds user files and the GUI adds more. Mitigation: `downloads.origin` is `tool` only for files created after the run start and absent from the pre-scan; only those are movable. `imported/` is included in the pre-scan so a moved FLAC is never re-requested. Staging folders stay out of `music_dirs` so the scanner never pins a staged path.
16. **The v6 migration must actually run.** `CREATE TABLE IF NOT EXISTS` would leave the live `local_assets` untouched. Mitigation: explicit `if version < 6` rebuild (new table, copy with `source='scan'`, drop, rename) before the schema script; `_record()` switches to plain `INSERT`; `review._local_file()` gets a deterministic `ORDER BY`.
17. **Folder IDs need their own home.** `spotify_playlists.music_persistent_id` is the per-playlist destination; the two folders have no row there. Mitigation: `music_folders(group, persistent_id, name, confirmed_at)`; the ancestry guard reads only that table. `possibly_found` is persisted as a `sync_status` row so `download_plan` reads the ledger, not a report.
18. **Import idempotency in Music.app.** A crash between the `add` call and the ledger write would re-import on rerun. Mitigation: a durable per-import intent file (same pattern as `tidal_sync`), and on resume, search Music for a track whose location basename equals the AIFF stem before calling `add`.
19. **Hi-res disk cost.** HI_RES_LOSSLESS at 24-bit/192 kHz becomes ≈60 MB per minute as AIFF. Decision needed before the 50-track run (user question pending): keep source rate, or convert to 24/48 or 16/44.1 in our ffmpeg step (FLAC master kept either way).

Not changed: Tidaler's download path (trailing space kept, read verbatim); TIDAL mirror playlists (listening only, no part in downloading). Verified: `{track_id}` is a real Tidaler template variable (`str(media.id)` for tracks), so the `cfg` change is safe.

## Implementation (in order)

### Step 0: close the local-index holes (`local_library.py`, `db.py`, `review.py`)
- `scan()`: on mutagen failure, index via `path_metadata()` with `duration_ms=None`, `metadata_source="path-unreadable"`.
- `classify()`: title+artist match with unknown length → `possibly_found` (never `found`); persisted as `sync_status('local_file_detected','possibly_found')`; new bucket in `match_local` totals and `write_local_report`.
- v6 migration in `Ledger.__init__`, run explicitly when `user_version < 6` before the schema script: rebuild `local_assets` as (`id`, `track_id`, `path`, `source`, `fingerprint`, `music_persistent_id`, `import_verified`, UNIQUE(track_id, path)), copying existing rows with `source='scan'`; add `downloads`, `music_folders`, `music_*` tables; accept version 6. `_record()` uses plain `INSERT` and deletes only `source='scan'` rows. `review._local_file()` gets `ORDER BY path`. Staging folders are never part of `music_dirs`.
- Tests in `tests/test_local_library.py`, including a migration test from a v5 fixture database.

### Step 1: Music.app read-only index (`src/dj_sync/music_app.py`, new)
- JXA via `osascript -l JavaScript` returning JSON; `MusicApp` wrapper with an injectable runner so tests use fixtures.
- `index`: playlists (persistent ID, name, kind, parent ID); file tracks (persistent ID, name, artist, duration, location); memberships under the managed folders. Tables `music_playlists`, `music_tracks`, `music_memberships`. Link `music_tracks.location` to `local_files.path` and fill `local_assets.music_persistent_id`. Tracks present in Music.app count as owned for download selection.
- Folder resolution: exactly one `Nu DJ Vibes` and one `Nu Vibes Vibes` folder playlist; `--confirm-folders` stores their persistent IDs in `music_folders(group, persistent_id, name, confirmed_at)`; otherwise stop. The ancestry guard reads only that table. Per-playlist destination IDs go in the existing `spotify_playlists.music_persistent_id`.
- `music report`: per mapped Spotify playlist → existing Music playlist (stored ID, else exact name inside the folder, else "to create"); adds (library track exists), imports (staging AIFF exists), missing, local-only (never removed). Zero writes. First run triggers the macOS automation prompt.
- CLI: `dj-sync music index [--confirm-folders]`, `dj-sync music report`.

### Step 2: Tidaler setup (one-time, scripted)
- `tools/setup-tidaler.sh`: `.venv-tidaler` with Homebrew Python 3.13, `pip install tidaler==0.1.8`; prints version. `config.yaml` gets `tidaler_bin`.
- One-time, user-approved: `tidaler cfg format_track "Tracks/{artist_name} - {track_title}{track_explicit} [{track_id}]"` (public interface; GUI shares it). `doctor` reports: tidaler venv/version, `format_track` contains `{track_id}`, token present, ffmpeg path, GUI not running.

### Step 3: download + verify + convert (`src/dj_sync/acquire.py`, new)
- `TidalerSettings.load()`: Tidaler's config dir by its own rule (`XDG_CONFIG_HOME`/`HOME`); `download_base_path` verbatim; Tracks folder from `format_track`.
- `download_plan(ledger, mappings, limit, include_other_versions=True)`: from `load_mapped_plans()` + `matches.tidal_id`, one entry per distinct TIDAL ID carrying every Spotify `track_id` that maps to it; exclude `local_file_detected` (`found` or `possibly_found`), Music.app-owned, `local`/`skip` decisions, `downloaded`, attempts ≥ 2; pre-scan URL tags of `Tracks/` and `Tracks/imported/` and record already-present IDs as `downloaded` with `origin='preexisting'`. Writes `data/downloads/<run>.txt` (TIDAL track URLs) and `<run>.json`.
- `run_tidaler(plan)`: preflight (GUI executable not running, checked by path; token file present; venv present; `format_track` contains `{track_id}`); `subprocess.run([tidaler, "dl", "-l", file], stdin=DEVNULL, start_new_session=True, timeout=120+60n)`, killing the whole process group on timeout; log to `data/logs/tidaler-<run>.log` with redaction; "login"/"not available" lines classified.
- `verify_downloads(plan, started_at)`: files in `Tracks/` created after start, absent from the pre-scan, quiescent 30 s, mutagen-parseable; URL tag → ID (primary), length within the matcher tolerance of the TIDAL duration in `service_tracks`; ISRC secondary. Outcomes per file: `downloaded` (`origin='tool'`, marks every `track_id` sharing that TIDAL ID) / `sibling_release` (review) / `collision` (review) / `unexpected` / `failed` (attempts++). Permanent "not available" → review as not-on-TIDAL.
- `convert(downloads)`: for `downloaded` rows without an AIFF: bit depth from `FLAC.info.bits_per_sample` (16 → `pcm_s16be`, 24 → `pcm_s24be`, else `failed`); sample rate capped at 48 kHz (`aresample` with SoX resampler; 44.1/48 kHz sources untouched); ffmpeg converts audio only (`-map 0:a`) to `<aiff_dir>/<stem>.aiff.part`, then our code writes ID3v2.3 with `mutagen.aiff` (title, artist, album, album artist, track number, date, `TSRC`, `APIC` cover, `TXXX:TIDAL URL`), renames into place (refusing an existing target), and verifies by reading the AIFF back through `local_library.read_file` (ISRC, URL ID, length). When no resampling happened, decoded checksums (`ffmpeg -f md5`) of FLAC and AIFF must match; when resampled, length must match within 50 ms. Stage `converted`. `aiff_dir` defaults to `~/Music/Tidal-DL/AIFF`.
- Ledger v6 `downloads(track_id, tidal_id, run_id, status, origin, attempts, flac_path, aiff_path, verified_at)`; stages `downloaded`, `converted` in `sync_status`.
- CLI: `dj-sync download [--apply] [--limit N]` (plan → run → verify → convert), `dj-sync convert` (conversion only).
- Tests (`tests/test_acquire.py`): a checked-in minimal silent FLAC fixture (`tests/fixtures/silence.flac`, generated once with ffmpeg) that a fake `tidaler` script copies and tags with mutagen; a fake `ffmpeg` that writes a hand-rolled AIFF (FORM/COMM/SSND; `aifc` no longer exists in Python 3.13+) or the real ffmpeg when present (`skipif` otherwise). Cases: success, exit-0-with-failures, login prompt, timeout with process group kill, GUI running, pre-existing file never moved, shared TIDAL ID marks both tracks, collision, sibling release, unexpected file, partial/still-writing file, attempts cap, 32-bit source refused, tag round-trip through `read_file`, convert verification failure.

### Step 4: Music.app placement (`music apply`, separate approval)
- Create missing playlists inside the confirmed folder; add existing library tracks by persistent ID; import staging AIFFs with `add` behind a durable per-import intent file (same pattern as `tidal_sync`): on resume, first search Music for a track whose location basename equals the AIFF stem and adopt it instead of calling `add` again; capture the returned persistent ID; verify location under the media folder (and ≠ staging path) and duration; add to playlists; stages `music_imported`, `music_playlist_member`. Every write passes the ancestry guard against `music_folders`. JSON backup export of managed playlists before the first apply. After a verified copy-import, move only `origin='tool'` FLAC and AIFF originals to their `imported/` subfolders; never delete; pre-existing files are never moved.

### Step 5: orchestrator
- `dj-sync run`: `sync --mapped` → `tidal-sync` → `local scan`+`match` → `music index` → `download` (incl. verify + convert) → `music report` → `music apply`. Writes are gated separately: `--apply-tidal` (TIDAL playlist additions), `--apply-download` (run Tidaler), `--apply-music` (Music.app changes; kept out of unattended runs at first). Soft/hard outcome per stage; one summary report. launchd only after manual runs succeed and the user asks.

Out of scope for this plan: the `Playlists/` staging folder, the 523 FLACs already inside the library, any Tidaler state change beyond the single `cfg` call, downsampling of existing library files.

## Files
- Modify: `src/dj_sync/local_library.py`, `db.py` (v6), `cli.py`, `config.py` (`tidaler_bin`, `aiff_dir`, `download_limit`), `reports.py`, `tidal_sync.py` (reuse `load_mapped_plans`), `README.md`, `docs/VALIDATION.md`, `docs/AGENT_HANDOFF.md`.
- New: `src/dj_sync/music_app.py`, `src/dj_sync/acquire.py`, `tools/setup-tidaler.sh`, `tests/test_music_app.py`, `tests/test_acquire.py`.
- Reuse: `Ledger`, `plan_additions`, `process_lock` (held across the subprocess), `save_json`/`atomic_text`, `write_report` style, review decisions, `normalize_isrc`, `identity`/`score`.

## Verification
1. After each step: `pytest -q`, `ruff check`, `ruff format --check`, `mypy src` green.
2. Step 0 live: `local scan` lists the 133 files as `path-unreadable`; `local match` shows `possibly_found`; `found` unchanged at 1,046.
3. Step 1 live: user accepts the automation prompt; `music index` finds each managed folder exactly once; `music report` shows the playlist mapping and zero writes; user reviews.
4. Step 2: `doctor` shows tidaler 0.1.8, `{track_id}` in `format_track`, token present, ffmpeg found.
5. Step 3 live, smallest experiment: GUI closed; `download` dry run; `download --apply --limit 1` on one deliberately chosen 24-bit hi-res track with cover art (exercises bit depth, resampling and tag round-trip); confirm the tagged FLAC and a 24/48 AIFF with readable ISRC and URL tags appear, the ledger shows `downloaded` + `converted`, and a repeat `--apply` requests nothing. Then `--limit 10`, then 50.
6. Step 4 live, separate ramp: one playlist, one track, then five; verify in Music.app and inspect the imported track's location by hand each time; repeat apply is a no-op; only tool-origin staging files move, and only after the location check.
