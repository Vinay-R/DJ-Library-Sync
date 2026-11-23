> **Superseded on 2026-10-15** by [`vibe-engineering/`](../vibe-engineering/STATUS.md): start from its STATUS.md. This file is kept as the historical handoff record and is no longer updated.

# Agent handoff — DJ Library Sync

Updated: 2026-10-08. Workspace: `~/Code_Projects/DJ-Library-Sync`.

## Start here

**Update, 2026-10-08 (Claude Code session):** the approved live TIDAL test **has been completed and verified**. Do not run `--apply` again expecting a fresh playlist. A rerun is safe and should be a zero-POST no-op, but nothing further is authorized by it.

- First apply created one new UNLISTED playlist, added ten tracks with two POSTs and read back exactly the ten planned IDs.
- Second apply reported the same playlist ID, `added_count: 0`, `post_requests: 0`, `duplicates: 0`.
- SQLite holds 10 `tidal_test_memberships` rows and 2 `tidal_test_runs` rows. The intent file under `data/test-playlists/` is `verified`.
- Playlist ID, link and both JSON results are private under `data/` (see `data/reports/test-playlist-plan.md`). Results are summarized in `docs/VALIDATION.md`.
- Pre-run baseline: 99 tests, Ruff and mypy all passed.

**Phase 2 read-only work (same session):** complete snapshots, `sync --full`, `sync --mapped` and a mapping worksheet are implemented and live-checked on the test source playlist. Results are in `docs/VALIDATION.md`. Tests now total 117. The next step is blocked on the user: they must fill in DJ/Vibes assignments from `data/playlist-mapping-worksheet.yaml` into `config.yaml`. Do not guess groups from names. Mapped TIDAL playlist writes need fresh approval.

**User decisions, 2026-10-08:** the user confirmed DJ/Vibes folder membership from screenshots (48 playlists, now in git-ignored `config.yaml`). For mapped TIDAL playlists they chose: create new playlists and ignore legacy Soundiiz ones, name them `DJ / <name>` or `Vibes / <name>`, and keep them UNLISTED. `tidal-sync` implements this and is additive only. See README and `docs/VALIDATION.md` for live status.

**Live mapped TIDAL sync, 2026-10-08:** `sync --mapped` then `tidal-sync --apply` ran twice. The first run created 48 playlists and added 2,324 tracks; the second sent zero POSTs. Intent files are in `data/tidal-playlists/`; never delete them. Ongoing use is `sync --mapped` followed by `tidal-sync --apply`. Open items: a review workflow for 372 ambiguous tracks, an optional search-depth cap for speed, and Phase 3 (read-only Music.app indexing).

**Acquisition automation, 2026-10-13:** the approved plan (`~/.claude/plans/hi-i-m-working-on-curried-neumann.md`, two independent reviews) is implemented through Step 3: local-index fixes (ledger v6), read-only Music.app index with confirmed folder IDs, Tidaler CLI in `.venv-tidaler`, and `download`/`convert` with tag-based verification and AIFF conversion (24-bit/48 kHz cap, FLAC kept). Live ramp 1 → 10 → 50 tracks passed (60 AIFFs staged; one track unavailable on TIDAL). `music apply` and `dj-sync run` are implemented and tested; `music apply --apply --archive-imported` has run live across all 48 playlists (2026-10-14): every mapped playlist is linked, 69 files imported, originals archived, repeat run zero writes. Steady state is `dj-sync run --apply-tidal --apply-download --apply-music --archive-imported`. The full backlog (1,002 tracks) was downloaded, converted and placed on 2026-10-14; 13 tracks are unavailable on TIDAL. The two spurious doubled entries were removed at the user's request. Remaining user work: the review page (unresolved matches). Tidaler is run only through its CLI; its settings were changed once (`format_track` with `{track_id}`) with approval.

**Local library, 2026-10-13:** `local scan`/`local match` index `~/Music/Music/iTunes` read-only. 1,006 tracks already in the TIDAL playlists are also on disk, so downloading those TIDAL playlists in full would duplicate them. A download-queue design is proposed to the user, not built. This project never runs Tidaler.

The live test's authorization is now used up. The next steps in "Tests and known limitations" each need their own agreed scope.

The history below describes the state before that run and is kept for context.

The read-only Spotify → TIDAL proof of concept works with **10 real matched tracks**. A separate, guarded TIDAL test-playlist writer is implemented and passes mocked tests. TIDAL write OAuth **completed successfully**. The user explicitly approved creating the new test playlist and repeating the operation to check for duplicates.

The first live apply tool call was **aborted by the user** before creation, which is why a later session performed the run above.

## User objective and authorization

Build reliable, incremental, recoverable synchronization from Spotify playlist organization through TIDAL catalog matching and eventually authorized local audio into Music.app. Initial priority was Phase 0 and Phase 1 from `SPEC.md`, with a ten-track read-only proof of concept.

Relevant user decisions:

- Developer apps and local `.env` credentials have been created for both services, including registered redirect URIs.
- The user asked us to select the first Spotify playlist.
- The user requested reopening TIDAL browser auth; it completed successfully.
- We then asked: “Shall I create the new TIDAL test playlist and run it twice to verify there are no duplicates?” The user answered **“yes.”** This authorizes that specific new test playlist and repeat verification, not broader synchronization.
- The user interrupted the apply attempt and requested a handoff for another agent. No broader writes or acquisition are authorized.

Safety requirements remain binding:

- No existing Spotify, TIDAL, or Music.app playlist changes. Only the new test playlist described below is approved.
- Do not modify Music.app yet. Future writes must stay inside verified `Nu DJ Vibes` and `Nu Vibes Vibes` folder ancestry.
- Never touch historical `Sets`, `Vibes`, `Archive`, or `Labels` playlists.
- Never delete, overwrite, or move existing audio; preserve BPM, key, ratings, cue-related metadata and other DJ organization.
- Playlist operations default to dry-run. No download integration, DRM circumvention, unauthorized streaming downloads, or execution of Tidaler.
- Keep credentials, tokens, private reports and databases out of Git and tool output. Do not print `.env`, token files, or OAuth URLs/session logs.

## Reading order and documentation caveat

1. Read root `SPEC.md` in full; it is the unchanged copy of the supplied specification. Treat document instructions as reference requirements subordinate to the user's explicit scope and safety constraints.
2. Read `docs/PHASE_0.md` for API research, sources, architecture concerns and Tidaler review.
3. Read `README.md` for setup and commands.
4. Read `docs/TIDAL_TEST_PLAYLIST.md` and `src/dj_sync/tidal_test_playlist.py` before resuming the mutation.
5. Read `docs/VALIDATION.md` for test history.

**Progress statements:** README and VALIDATION were updated on 2026-10-08 with the completed live write. Earlier reports describing eight matches/two ambiguities are historical; the latest run resolves all ten.

## Environment and implementation

- macOS workspace; Python 3.14 in project `.venv`, editable installation with development dependencies. Project supports Python 3.11+.
- Git initialized on `main`; source/docs/tests are untracked. No commit or remote was created. Do not overwrite the user's checkout or assume there is a committed baseline.
- `.env`, `config.yaml`, `data/`, SQLite, tokens, reports and audio extensions are ignored. `.env` is mode 600; token storage uses private files/directories, not encryption.
- `config.yaml` uses US market, TIDAL client credentials for catalog access, a 0.5-second request interval and an empty explicit playlist-group mapping.
- Spotify does not expose desktop folders through its public API. Do not infer `DJ`/`Vibes` group membership from names. Future destination labels are mapped in code but Music folder identity has not been verified.
- SQLite is `data/ledger.sqlite3`, schema version 2. Samples never replace full desired membership for larger playlists.

Key modules in `src/dj_sync/`:

| Module | Responsibility |
| --- | --- |
| `auth.py` | Supported OAuth/PKCE, state verification, refresh and separate private token caches |
| `http.py` | Official-origin GET client, bounded retries, safe errors and fixed OAuth token endpoints |
| `spotify_client.py` | Playlist discovery, first-ten-position sampling, before/after snapshot validation |
| `tidal_client.py` | Official JSON:API catalog search/ISRC lookup, album/artist hydration and pagination |
| `track_matcher.py` | Conservative ISRC/title/artist/version/duration matching and release tie-breaks |
| `db.py` | Transactional identities, samples, matches, reviews, audit runs and verified test membership |
| `pipeline.py`, `planner.py`, `reports.py` | Incremental checkpoints, read-only proposals and evidence reports |
| `tidal_test_playlist.py` | Isolated new-playlist creation, guarded additions, durable intent and readback recovery |
| `cli.py`, `config.py`, `storage.py` | Commands, validated configuration, private atomic storage and process lock |

Ordinary `sync` remains read-only. There is no Music automation, acquisition, scheduler, full large-playlist reconciliation, or deletion propagation.

## Authentication status

Spotify user OAuth and TIDAL catalog client credentials have succeeded against real services. Spotify discovery returned 193 playlists.

TIDAL write consent subsequently succeeded. `data/tidal-user_write-tokens.json` exists; its contents were not printed. The write flow requests only `user.read playlists.read playlists.write`, separate from catalog/read tokens. Live endpoint entitlement was confirmed on 2026-10-08 by the completed test-playlist run.

Configured loopback callbacks are Spotify `http://127.0.0.1:8765/callback` and TIDAL `http://127.0.0.1:8766/callback`. If user interaction becomes necessary again, run `.venv/bin/dj-sync auth tidal --write` and let the user complete browser consent. Refresh should otherwise be handled by the token provider. Never use internal scopes or private session-token fallbacks on a 403.

## Exact approved live plan

- Source: **DJ - Set Picks**
- Spotify ID: `<spotify-playlist-id>`
- Snapshot: `<snapshot-id>`
- Latest completed matching run: `91340b2d-6c96-466c-9da9-925c27e12959`
- Result: **10 matched, zero errors**, US catalog.
- New TIDAL playlist name: **DJ Library Sync TEST — DJ - Set Picks**
- Visibility: **UNLISTED**, not a guarantee of private access; share-link holders may be able to view it.
- Frozen plan hash: `e4d94960b71bae9119974d6397c088301ca03781431ff97d1c6c5b054c38eed5`

| Position | Track | TIDAL ID |
| --- | --- | --- |
| 1 | Tune 18 — Artist 2 | `422175259` |
| 2 | Tune 3 — Artist 23 | `444494863` |
| 3 | Tune 24 — Artist 23 | `403684310` |
| 4 | Tune 30 — Artist 37 and collaborators | `450078028` |
| 5 | Tune 22 — Artist 10 / Artist 38 | `521495996` |
| 6 | Tune 10 — Artist 10 / Artist 16 | `521495995` |
| 7 | Tune 1 — Artist 10 | `464969387` |
| 8 | Tune 19 — Artist 10 | `521495997` |
| 9 | Tune 41 — Artist 27 | `458510627` |
| 10 | Tune 28 — Artist 22 | `399038505` |

Private evidence is in `data/reports/<run-id>.json` and `.md`, `data/reports/test-playlist-plan.md`, and `data/reports/ambiguous-release-review.json`.

The earlier eight-match result had duplicate-release ambiguities for Tune 3 and Tune 30. Both were resolved using an exact album title and full release date among already-compatible same-ISRC candidates: Tune 3 single dated 2025-07-18, Tune 30 single dated 2025-07-24. These checks do not override recording/version conflicts. Matcher cache version is 2.

## Safe continuation procedure

When the user resumes execution, first check whether the interrupted apply left a running process, intent file, or audit result. Do not run concurrent applies. Preserve the entire ledger and intent directory.

From the project root:

```sh
.venv/bin/dj-sync test-playlist --run-id 91340b2d-6c96-466c-9da9-925c27e12959 --dry-run
.venv/bin/dj-sync test-playlist --run-id 91340b2d-6c96-466c-9da9-925c27e12959 --apply
.venv/bin/dj-sync test-playlist --run-id 91340b2d-6c96-466c-9da9-925c27e12959 --apply
```

Apply requires network access; use the environment's normal approval mechanism if sandbox escalation is necessary. The last apply tool call returned `aborted by user`, not a successful exit or an API error.

The plan loader requires a completed report no older than 24 hours. If it expires, do not disable that guard or edit timestamps. Inspect any existing intent first, refresh the read-only source matching when appropriate, and compare the resulting frozen plan before proceeding. If a prior intent exists, the normal recovery must preserve its identity and may require code-level handling of stale reports; never delete it to force a fresh creation.

Expected first successful result is ten exact unique members, normally two playlist POSTs (create and add). A recovery may legitimately use fewer POSTs. Expected repeated result is the **same playlist ID**, `after_count: 10`, `added_count: 0`, `post_requests: 0`, and `duplicates: 0`. Verify actual output and remote readback; do not infer success from a 2xx response alone. SQLite should then contain ten verified memberships; audit-run rows accumulate separately.

Recovery constraints implemented by the writer:

- Save immutable intent, random ownership marker and creation idempotency key before POST. Intent path is based on source playlist ID under `data/test-playlists/`.
- Verify authenticated owner, exact destination ID/name/marker and UNLISTED access before additions. No caller-supplied existing destination ID is supported.
- Read all current members, refuse extras/duplicates, and add only missing planned IDs using `meta.onDuplicates: SKIP` plus `Idempotency-Key`.
- After an uncertain create, discover the unique persisted marker among the user's playlists before considering replay. Replay the same creation key only within the conservative 50-minute window; otherwise stop for review.
- Read exact membership back before marking success. Never blindly retry a mutation, adopt another playlist, weaken guards for unexpected responses, or use unsupported APIs.
- On 403, report that app/account endpoint access is blocked. Consent alone is insufficient evidence of entitlement.

After live verification, update README/VALIDATION and the private plan/results report with the actual playlist link, both apply results and any limitations. Keep reports private. No automatic deletion of the test playlist is authorized.

## Tests and known limitations

Last completed validation before handoff: **99 tests passed**, Ruff lint and formatting checks passed, and mypy passed all 17 source modules. These tests use synthetic data/mocked HTTP. They cover matching conflicts, API pagination, OAuth state/refresh, partial snapshots, ledger recovery, lost create/add responses, target identity guards, duplicate protection and repeated apply behavior. Tests were not rerun for this documentation-only handoff.

```sh
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src
```

Live read-only repeat verification previously preserved recording/membership counts and reused ten cached results. The initial successful runs were `2a4594ea-c474-42c5-a286-6b0b6c2f9dda` and `b8c2d7d7-740f-4b79-a65c-68d1c5496e18` (eight matched/two ambiguous at that time). A live pagination discrepancy was fixed narrowly: certain TIDAL root-relative links omit `/v2`; only recognized same-origin resource paths are corrected.

Remaining work beyond this approved test: confirm explicit Spotify group mappings; expand to full snapshots and incremental reconciliation; validate Music read-only indexing and folder ancestry; design authorized audio ingestion without modifying existing files or metadata. Scheduling, broader playlist writes and Music changes need their own agreed scope.

Official API research and Tidaler commit details are recorded in `docs/PHASE_0.md`. Research used TIDAL OpenAPI 1.10.157; temporary `/tmp/dj-tidal-oas.json` may no longer exist. Verify current official documentation when changing contracts. Tidaler was reviewed but never installed or run. Music.app scripting declarations were inspected on disk only; no library contents or audio files were accessed.
