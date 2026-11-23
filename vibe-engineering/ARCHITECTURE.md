# DJ Library Sync — Architecture Reference

> Structural map of the codebase + always-true rules. Read this first to get oriented;
> read [STATUS.md](STATUS.md) for current state/next steps, [DECISIONS.md](DECISIONS.md)
> for *why* load-bearing choices were made, and the dated files in [worklogs/](worklogs/)
> for session stories (on demand only).
> Product spec: [../SPEC.md](../SPEC.md). Approved acquisition plan:
> [plans/2026-10-13-acquisition-plan.md](plans/2026-10-13-acquisition-plan.md).
> Keep this current as phases land — see Maintenance rules at the bottom.

## Invariants (violations are bugs)

- **INV-1 (dry run by default):** every write needs `--apply`; `run` gates each surface with its own flag (`--apply-tidal`, `--apply-download`, `--apply-music`, `--archive-imported`) — `cli.py`.
- **INV-2 (library audio is read-only):** existing audio is never deleted, overwritten, retagged or moved; scanners open files read-only — `local_library.read_file`, `acquire._probe`. Only tool-downloaded staging files move, after a verified copy-import — `music_apply._archive`. One exception, user-approved 2026-10-20: the cover frame of a library file whose TIDAL URL tag names one of the tool's own downloads may be rewritten, with the audio chunk checksummed before and after — `artwork.fix_file_cover`.
- **INV-3 (managed folders only):** Music.app writes happen only under the confirmed `Nu DJ Vibes` / `Nu Vibes Vibes` persistent IDs, checked live before each write — `music_apply._guard`, `music_folders`.
- **INV-4 (additive only):** no code path removes TIDAL or Music.app playlist entries, playlists or library tracks; drift is reported for the user — `tidal_sync`, `music_apply`.
- **INV-5 (success = readback):** a write counts only after reading the result back (TIDAL membership, Music.app membership, download tags + length, AIFF tags) — `tidal_sync`, `music_apply`, `acquire.verify_downloads`, `acquire._convert_one`.
- **INV-6 (journal before uncertain writes):** TIDAL create/add and Music.app imports save a durable intent first and reconcile it on rerun, so nothing is created twice — `tidal_sync`, `tidal_test_playlist`, `music_apply._import`.
- **INV-7 (groups are user-given):** DJ/Vibes membership comes only from `config.yaml`, never inferred from names — `config.py`.
- **INV-8 (secrets stay local):** tokens and `.env` never reach logs, reports or git; Tidaler's token is only checked for existence — `.gitignore`, `acquire.REDACT`, `TidalerSettings.token_state`.
- **INV-9 (official interfaces only):** Spotify and TIDAL official APIs; Tidaler only through its public CLI; no private APIs, cookies or DRM workarounds.

## Stack

- Python 3.11+ CLI `dj-sync` (dev venv `.venv` on 3.14); httpx, mutagen, PyYAML, python-dotenv; pytest, ruff, mypy.
- SQLite ledger `data/ledger.sqlite3`, schema `user_version` 6 (explicit migration from ≤5).
- Spotify Web API (PKCE user auth; `/playlists/{id}/items`, Feb 2026 shape).
- TIDAL OpenAPI v2 (spec 1.10.157): client credentials for catalog, separate user-PKCE token for playlist writes.
- Tidaler 0.1.8 CLI in its own venv `.venv-tidaler` (Python 3.13); shares `~/.config/tidaler/` with the GUI app.
- ffmpeg 8 (Homebrew) for FLAC → AIFF; Music.app 1.6.3 via `osascript` (JXA, plus AppleScript for playlist creation); macOS 26.

## Layout

```
.
├── src/dj_sync/        — 24 modules, ~6.2k lines (see § Modules)
├── tests/              — 16 files, 247 tests, all offline (mocked HTTP, fake Tidaler, fake Music.app);
│   └── fixtures/       — 16-bit/44.1k and 24-bit/96k FLAC clips for conversion tests
├── tools/
│   ├── review-page/    — template + build script for the TIDAL Match Review artifact
│   └── setup-tidaler.sh — creates .venv-tidaler with tidaler==0.1.8
├── docs/               — Codex-era docs: PHASE_0 research, VALIDATION log, test-playlist notes, handoff
├── SPEC.md             — original product spec (docs/dj-library-sync-SPEC.md is a byte-identical copy)
├── config.example.yaml / .env.example — templates; real config.yaml and .env are git-ignored
├── data/               — git-ignored private state: ledger, tokens, reports, logs, intents (see § Data model)
└── vibe-engineering/   — this context system (README.md explains the files)
```

### Modules

| Module | Responsibility |
|---|---|
| `cli.py` | All commands (see § Commands), `doctor`, the `run` pipeline wiring |
| `config.py` | Validated `config.yaml` (unknown keys rejected), credentials from env/.env |
| `auth.py`, `http.py`, `storage.py` | OAuth/PKCE + refresh; read-only API client with bounded retries; private atomic files + process lock |
| `spotify_client.py` | Playlist discovery, first-10 samples, full snapshots with snapshot-ID consistency |
| `tidal_client.py` | Catalog: ISRC lookup + title search (3-page cap) + fallback search |
| `track_matcher.py` | Conservative scoring; identical-release rule; normalisation |
| `pipeline.py`, `planner.py` | Per-playlist and mapped runs, checkpoints, manual decisions, TIDAL plans |
| `tidal_test_playlist.py`, `tidal_sync.py` | Guarded TIDAL writer API; one test playlist; 48 mirror playlists |
| `review.py` | Review export/import (`match`, `no_match`, `local`, `skip`, `clear`) |
| `local_library.py` | Read-only tag scan of `music_dirs`; Spotify → local file matching |
| `music_app.py` | Read-only Music.app index, folder resolution, reconciliation report, scripts |
| `acquire.py` | Tidaler plan / run / tag verification / FLAC → AIFF conversion |
| `music_apply.py` | Guarded Music.app placement, import intents, archive of originals |
| `orchestrate.py` | Stage runner with soft/hard outcomes and the run report |
| `reports.py`, `models.py`, `db.py` | Markdown/JSON reports; shared types; ledger schema + migration |

## Data model

Ledger tables (`db.py`, `SCHEMA` + `MIGRATE_V6`). Grouped by stage:

| Area | Tables | Key facts |
|---|---|---|
| Identity | `tracks`, `service_tracks` | One internal track per Spotify ID; TIDAL candidates stored as service rows |
| Spotify | `spotify_playlists`, `observed_memberships`, `desired_memberships` | Samples never replace complete desired membership; `spotify_playlists.music_persistent_id` = linked Music.app playlist |
| Matching | `matches`, `match_reviews`, `manual_matches` | Cache: matched 30 days, review items 24 h; manual decisions override search |
| TIDAL | `tidal_memberships`, `tidal_test_memberships`, `tidal_test_runs` | Verified remote membership only |
| Local files | `local_files`, `local_assets` | `local_assets` UNIQUE(track_id, path) with `source` (`scan` / `music`); `music_persistent_id` set when Music.app holds the file |
| Downloads | `downloads` | PK(track_id, tidal_id); status `downloaded`/`converted`/`imported`/`failed`/`corrupt`/`review`/`unavailable`; `origin` `tool`/`preexisting` |
| Music.app | `music_folders`, `music_playlists`, `music_tracks`, `music_memberships`, `music_artwork` | `music_folders` holds the two confirmed folder IDs; `music_artwork` = library tracks whose cover was handed to Music.app |
| Audit | `runs`, `sync_status`, `sync_events` | Stages: `catalog_match`, `tidal_playlist_added`, `local_file_detected`, `downloaded`, `converted`, `music_imported`, `music_playlist_member` |

Other private state under `data/`: `reports/` (every run, JSON + Markdown), `logs/` (redacted
Tidaler output), `downloads/` (URL lists + plans), `tidal-playlists/` and `test-playlists/`
(TIDAL intents), `music-imports/` (Music.app import intents), `review/` (review export and
built page), `backups/` (ledger before v6; Music.app playlist exports before first write).

## Commands

| Command | Writes? | Purpose |
|---|---|---|
| `doctor` | no | Config, credentials present, Tidaler/ffmpeg/GUI/session checks |
| `auth spotify` / `auth tidal [--user\|--write]` | tokens | Browser OAuth; `--write` = TIDAL playlist scope |
| `playlists` | no | Discovery + mapping worksheet `data/playlist-mapping-worksheet.yaml` |
| `sync --playlist ID [--full]` / `sync --mapped [--retry-review]` | ledger | Read-only Spotify read + TIDAL catalog matching |
| `tidal-sync [--apply]` | TIDAL | Create/append the 48 "DJ / …" "Vibes / …" mirrors |
| `test-playlist --run-id ID [--apply]` | TIDAL | The one-off Phase 1 test playlist |
| `review export` / `review import FILE` | ledger | Decisions from the review page |
| `local scan [--dir P]` / `local match` | ledger | Read-only tag index; on-disk detection |
| `music index [--confirm-folders]` / `music report` | ledger | Read-only Music.app index; reconciliation report |
| `download [--apply] [--limit N] [--only IDS]` | disk | Plan, run Tidaler, verify, convert |
| `convert` | disk | FLAC → AIFF only |
| `music apply [--apply] [--archive-imported] [--playlist ID] [--max-writes N]` | Music.app | Place tracks; ramp limits for careful runs; hands each import's cover to Music.app |
| `music artwork [--apply] [--limit N]` | library files (cover frame only) + Music.app | Repair covers of earlier imports |
| `run [--apply-tidal] [--apply-download] [--apply-music] [--archive-imported] [--limit N]` | per flag | All stages in order (see § Pipeline order) |

### Pipeline order

`spotify_sync → tidal_sync → local → music_index → download → music_report → music_apply`.
Owned recordings are known (disk + Music.app) before anything is downloaded; placement is last.
Soft outcomes (deferred playlist, failed download, skipped playlist) continue with exit 2;
exceptions stop the run and skip later stages.

## Environment

| Variable | Scope | Purpose | Blank = ? |
|---|---|---|---|
| `SPOTIFY_CLIENT_ID` | local `.env` | Spotify PKCE app | `auth spotify` fails with a clear error |
| `SPOTIFY_REDIRECT_URI` | local `.env` | `http://127.0.0.1:8765/callback` | default used |
| `TIDAL_CLIENT_ID`, `TIDAL_CLIENT_SECRET` | local `.env` | TIDAL catalog (client credentials) + write PKCE | TIDAL commands fail closed |
| `TIDAL_REDIRECT_URI` | local `.env` | `http://127.0.0.1:8766/callback` | default used |
| `XDG_CONFIG_HOME` | process | Moves Tidaler's config dir (Tidaler's own rule) | `~/.config/tidaler` |

`config.yaml` keys: `data_dir`, `country_code` (US), `tidal_auth`, `request_interval` (0.5 s),
`playlists` (48 Spotify ID → `DJ`/`Vibes`), `music_dirs` (`~/Music/Music/iTunes`),
`tidaler_bin`, `aiff_dir` (`~/Music/Tidal-DL/AIFF`), `download_limit` (50), `ffmpeg_bin`.
Tidaler's own settings: download path `~/Music/Tidal-DL/FLAC ` (trailing space,
read verbatim), `format_track` ends in `[{track_id}]`, HI_RES_LOSSLESS, `extract_flac`.

## Run & verify

```
.venv/bin/pytest -q && .venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy src
.venv/bin/dj-sync doctor
.venv/bin/dj-sync run                                   # every stage as a dry run
.venv/bin/dj-sync run --apply-tidal --apply-download --apply-music --archive-imported
tools/setup-tidaler.sh                                  # once, Tidaler CLI venv
.venv/bin/python tools/review-page/build.py data/reports/sync-<id>.json   # rebuild review page
```

"Done" for any write path means: unit tests green, then a live ramp (1 item → a few → all),
each step read back, and a repeat run showing zero writes.

## Conventions & gotchas

> Grouped by area. Past ~40 bullets total, spill a group to `architecture/conventions-<area>.md`.

### Process & scope

- **Commits:** one per phase, local only, on `main`. **Never push without explicit user go-ahead** (no remote is configured).
- **One agent session per working tree.**
- **User approval per write surface.** New kinds of writes (removals, retagging, rekordbox) need an explicit, scoped yes. The 2026-10-14 removal of two doubled entries and the 2026-10-16 deletion of 5 legacy playlists were one-offs by the user's instruction, not features.
- **Ramp live writes:** `--only`, `--limit`, `--playlist`, `--max-writes` exist for this.
- **Verify what the user sees, not only what the file holds.** Cover art passed every file-level check (mutagen, AVFoundation) and still did not show in Music.app; for anything visible, the user's eye on one item is the final check (2026-10-20).

### Spotify and matching

- Spotify sometimes returns lowercase or hyphenated ISRCs; TIDAL rejects them (HTTP 400) — normalise at both boundaries (2026-10-08).
- Spotify "(with A & B)" credits are stripped like "feat." only when they name credited artists; "(Prod. …)" credits are stripped too (2026-10-08, 2026-10-13).
- Popularity, label and audio features (BPM/key) are not available from the API; `added_at` per playlist entry is, but is not stored (2026-10-15).

### TIDAL and Tidaler

- Deep title-search pagination is wasted time; 3 pages lose nothing (62/62 identical outcomes) (2026-10-08).
- Tidaler exits 0 even when every URL fails; its "You are logged in." line must not be read as a login prompt (2026-10-13).
- Tidaler's `skip_existing` checks one path in its own folder only; the ledger is the real dedupe (2026-10-13).
- Some TIDAL tracks report "Media not found" when withdrawn; they become `unavailable` and are never retried (2026-10-14).
- `pgrep -f tidaler` matches the agent's own shell; detect the GUI by its bundle path (2026-10-13).

### Local files and conversion

- mutagen cannot parse ~133 library files (mostly AIFF); they are indexed by `Artist/Album/NN Title` path and count as `possibly_found` (2026-10-13).
- ffmpeg's AIFF muxer drops or renames tags (`ISRC` → `TXXX`); tags are written by our code and read back. ffmpeg decodes 24-bit FLAC as s32, so bit depth comes from the FLAC header (2026-10-13).
- TIDAL occasionally serves 32-bit FLAC; it is reduced to 24-bit AIFF, verified by length (2026-10-14).

### Music.app scripting

- JXA playlist creation fails (-1700); use AppleScript `make new user playlist at <folder>` (2026-10-14).
- Bulk reads on an empty playlist raise -1728; check `tracks().length` first (2026-10-14).
- Music.app renames copied files (Artist/Album/NN Title) and can return an import before the copy finishes, sometimes with no usable reference; reconcile by title, artist, length and date added (2026-10-14).
- `add … to playlist` does not reliably place the import in that playlist, and membership reads lag; re-read with polling, then add what is missing (2026-10-14).
- The library holds several copies of some songs and some entries with no file; presence is checked by any linked copy or title + lead artist, and file-less entries never count as owned (2026-10-14).
- `music index`'s `managed_playlists` counts every playlist under the two folders, linked or not; a number above the linked count means unlinked (legacy or hand-made) playlists are there (2026-10-16).
- Music.app cannot read an ID3v2.3 APIC whose description is UTF-16 (mutagen's `encoding=3` under v2.3) and shows no cover; write Latin-1 with an empty description. Even a readable cover in the file is not shown until the image is set through Music.app (`set data of artwork 1`), which fills its own artwork store (2026-10-20).
- Music.app can fail to report `parent`, flattening the playlist tree (once: 0 managed of 233). `index_music` rejects an index where a linked DJ/Vibes destination is outside the managed folders, retries twice, then stops without writing (2026-10-20).
- Deleting a user playlist (`app.delete(pl)`) leaves its tracks in the library; the library count was unchanged across 5 deletions (2026-10-16).

## Maintenance rules (for the agents that update this file)

- Decisions and their rationale do NOT live here — they go to [DECISIONS.md](DECISIONS.md).
- **No stale islands:** if you update one representation of a fact (data model, commands,
  layout tree), update its other representations too.
- If any single section outgrows ~100 lines, split it into
  `vibe-engineering/architecture/<section>.md` and leave a one-line pointer here.
- Never duplicate STATUS.md content here — this file is the map, STATUS is the state.
- The `## Invariants` … `## Stack` headings are hook anchors for the SessionStart hook the
  README describes (not installed yet). Keep them.
