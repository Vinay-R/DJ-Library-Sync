# Validation — 2026-10-08

## Completed

- Read the entire supplied specification; project SPEC.md is byte-identical.
- Reviewed official Spotify documentation through July 2026 and TIDAL OpenAPI 1.10.157. Findings and primary sources are in PHASE_0.md.
- Reviewed Tidaler's published interface/configuration at the commit recorded in PHASE_0.md, without running it.
- Python 3.14 local virtual environment installed successfully.
- `pytest -q`: **99 passed**. All account/API data in tests are synthetic or mocked.
- `ruff check src tests`: passed. Formatting check passed.
- `mypy src`: passed for all 17 source modules.
- The CLI integration test runs the complete ten-item pipeline twice; verifies GET-only service traffic, OAuth-only POST, ten matched synthetic rows, one deduplicated destination proposal, cache reuse and two separate audit reports.
- Other tests cover version/ISRC conflicts, shared guest artists, explicit editions, missing duration, pagination, cursor/filter retention, source snapshot changes, short/partial snapshots, incomplete identities, transaction rollback, interrupted runs, refresh failures, rate-limit deferral, OAuth state and PKCE exchanges, private storage and safe error output.
- Initialized Git on main. Credentials, config, token caches, SQLite files, reports and audio extensions are ignored. No commit or remote was created.
- Initialized the private `data/ledger.sqlite3`; it now contains ten real source recordings and their live catalog evidence.

## Live read-only acceptance passed

Spotify user OAuth and TIDAL client-credentials OAuth succeeded using the locally configured apps. Spotify discovery returned 193 playlists. At the user's request to select a playlist, the first ten positions of a DJ playlist were processed without assigning an unconfirmed folder mapping.

- Successful run: `2a4594ea-c474-42c5-a286-6b0b6c2f9dda`.
- Repeated run: `b8c2d7d7-740f-4b79-a65c-68d1c5496e18`.
- Result in both runs: **8 matched, 2 ambiguous, 0 unmatched, 0 errors**.
- All ten rows came from the live Spotify API; candidate evidence came from the official TIDAL catalog. All eight selected matches have agreeing ISRCs and compatible artist/title/version/duration evidence.
- The two ambiguous recordings have multiple eligible TIDAL IDs with the same ISRC and near-identical duration; no candidate was selected arbitrarily.
- The repeat reused all ten cached checkpoints and produced the identical list of eight unique proposed TIDAL IDs.
- Ledger counts before and after repeat were identical: 10 tracks, 640 service-track catalog records (including rejected candidates), 10 observed memberships, 0 complete desired memberships, 10 match results, 2 review rows, 0 local assets. A ten-track sample of a larger playlist intentionally does not populate full desired membership.
- **External playlist writes: 0.** Real destination deduplication remains untested because no write was authorized or implemented.
- Private human-readable results: `data/reports/live-poc-results.md`. Full evidence remains in the per-run JSON reports; it is excluded from Git.

The initial run exposed root-relative TIDAL pagination links omitting `/v2`. The client now corrects only recognized catalog paths on the same official origin. Four new regression cases cover these links and continued rejection of external/unrecognized destinations. The failed initial run remains in the audit ledger.

## Remaining boundaries

Published TIDAL playlist endpoints are documented, but actual app/account playlist-write entitlement and remote write idempotency are **not tested**. The pure planner's duplicate avoidance and local ledger idempotency are tested; that does not establish remote mutation reliability.

Music.app scripting declarations and installed versions were inspected on disk only. Runtime Apple Events permissions, folder ancestry, ordering, library import and membership-only removal remain Phase 3+ validation work. No Music library, historical playlist, audio file or DJ metadata was read or modified.


## New test-playlist implementation — 2026-10-08

After the user requested continuation, the two duplicate-release cases were investigated through official catalog reads. Each Spotify single has a unique TIDAL single with matching album title and full release date. Matcher version 2 uses this only to break ties among already-compatible same-ISRC recordings. Live run `91340b2d-6c96-466c-9da9-925c27e12959` now has **10 confident matches and no errors**. Full evidence remains private under data/reports.

Implemented a separate `test-playlist` command, dry-run by default and requiring explicit `--apply`. It pins a completed recent report, creates only a new UNLISTED test playlist, checks ownership/marker/name before adding, skips duplicates on the server, journals intents before POSTs and verifies exact remote membership. See TIDAL_TEST_PLAYLIST.md. SQLite schema version 2 adds verified test membership/audit tables without changing existing data.

Twenty added regression cases cover release tie-breaking, new playlist creation, no-op repeats, lost create/add responses, mismatched account/target/visibility, unexpected entries, stale recovery, explicit apply, OAuth scope boundaries, guarded pagination and idempotent SQLite verification. Total: **99 tests passing**, lint/format/type checks passing.

The exact live plan contains ten unique IDs for one unlisted playlist named with the `DJ Library Sync TEST` prefix. Its dry run succeeded with zero writes. TIDAL write OAuth was started, but the first browser callback timed out. A later consent attempt succeeded; see the live write section below.


## Live TIDAL test-playlist write — 2026-10-08

Before going live, the baseline was rechecked: **99 tests passed**, Ruff lint and format checks passed and mypy passed all 17 source modules. No leftover process, intent file or test ledger rows existed from the earlier aborted attempt.

The dry run for run `91340b2d-6c96-466c-9da9-925c27e12959` reproduced the frozen plan hash `e4d94960b71bae9119974d6397c088301ca03781431ff97d1c6c5b054c38eed5` with ten unique IDs and no skipped rows.

| Apply | Same playlist | Before | After | Added | POST requests | Duplicates |
| --- | --- | --- | --- | --- | --- | --- |
| First | new | 0 | 10 | 10 | 2 | 0 |
| Second | yes | 10 | 10 | 0 | 0 | 0 |

- The app/account **is entitled** to the official `POST /v2/playlists` and `POST /v2/playlists/{id}/relationships/items` endpoints with the public `playlists.write` scope. No 403 occurred.
- Both results came from a full remote membership readback, not from POST status codes. The readback matched the plan exactly, once per ID, in source order.
- The repeated apply re-verified owner, name, marker and UNLISTED visibility, found nothing missing and sent no mutation.
- SQLite now holds 10 verified `tidal_test_memberships` rows and 2 `tidal_test_runs` rows. The intent file is `verified`, and its single add event is `verified_by_readback`.
- The playlist link and per-apply JSON results are private under `data/`. The playlist is unlisted, so its link is kept out of committed docs.

Not exercised live: lost create/add responses, idempotency-key replay and marker-based recovery. Those paths remain covered only by mocked tests. Removals, reordering, existing-playlist adoption and any Spotify or Music.app writes remain unimplemented.


## Phase 2 read-only sync — 2026-10-08

Added complete playlist snapshots, `sync --full`, `sync --mapped` and a mapping worksheet. Confident matches are now cached for 30 days; review items are retried after 24 hours. Eighteen new tests cover full pagination, empty playlists, concurrent edits, truncated pagination, the cache policy, mapped runs with shared tracks, unreadable playlists, rate-limit deferral, Spotify auth failure, idempotent repeats and argument validation. Total: **117 tests passing**, Ruff and mypy clean.

Live checks, all read-only:

- `playlists` returned 192 playlists, 184 readable by this app and 8 owned by others. Every entry had an item count. The worksheet assigns no groups.
- `sync --playlist <DJ - Set Picks> --full` read all 39 items with a stable snapshot: **38 matched, 1 ambiguous, 0 errors**, in about 80 seconds. The ten earlier matches came from cache.
- The ambiguous item is Artist 1 "Tune 14". TIDAL US has only a later compilation release with a different ISRC, so the matcher correctly held it for review.
- An immediate repeat took about 2.5 seconds with all 39 items cached. It compared against the first complete baseline and found no additions, removals or reordering. Ledger rows stayed at 39 tracks, 39 desired memberships and 39 matches.

`sync --mapped` has not run live because no playlists are mapped yet. External writes in this phase: **0**.


## Mapped TIDAL playlist writer — 2026-10-08

Added `tidal-sync`, which creates one UNLISTED TIDAL playlist per mapped Spotify playlist and appends missing confident matches. It reuses the test writer's guarded API with a batch size of 50, the documented `maxItems` in TIDAL OpenAPI 1.10.157. Eighteen new tests cover batching, Spotify order, no-op repeats, incremental additions, extras and Spotify removals left in place, dry runs, lost create and add responses, bad readback, account mismatch, rename reporting, plan freshness and grouping checks, ledger recording, and stopping on access denial. Total: **135 tests passing**, Ruff and mypy clean.

### Live mapped sync and TIDAL apply — 2026-10-08

The user confirmed folder membership from screenshots: 20 DJ and 28 Vibes playlists, each resolved by exact name to a unique owned playlist ID.

The first `sync --mapped` read all 48 playlists (about 2,760 entries, 2,184 unique Spotify tracks, 465 in more than one playlist). Six tracks failed with TIDAL HTTP 400 because Spotify returned lowercase or hyphenated ISRCs. ISRCs are now normalized at both API boundaries, with regression tests (143 total). The rerun completed all 48 playlists with no errors:

| Status | Rows |
| --- | --- |
| matched | 2,342 |
| ambiguous | 372 |
| unavailable | 30 |
| unmatched | 22 |

About a third of tracks hit the 20-page title-search cap at roughly 15 seconds each. Across 271 such tracks, no candidate outside the top 40 by score was plausible. Capping search depth was implemented afterwards; see below.

`tidal-sync` preview planned 48 new playlists and 2,324 additions with zero writes. The applies:

| Apply | Playlists verified | Tracks added | POST requests |
| --- | --- | --- | --- |
| First | 48 created | 2,324 | 121 |
| Second | 48, same IDs | 0 | 0 |

Every playlist was verified by full readback. The ledger links all 48 Spotify playlists to their TIDAL playlist and holds 2,324 TIDAL memberships, each occurring once. No existing TIDAL, Spotify or Music.app playlist was touched. Playlist links are in the private `data/reports/tidal-sync-*.md` reports.

### Title-search depth cap — 2026-10-08

Title/artist search now follows at most 3 result pages; ISRC lookups stay uncapped. Two new tests cover both. Total: **145 tests passing**. Live check: 62 tracks that had used the deep search were re-matched with the cap (45 matched, 15 ambiguous, 2 unmatched, sampled from 878). All 62 produced the identical status and selected TIDAL ID. Time per track dropped from about 13–15 seconds to 2.8 seconds.

### Matching improvements and review page — 2026-10-08

At the user's request: identical-release resolution, a bare title-plus-artist fallback search, searching Spotify-unavailable tracks, Spotify "(with …)" guest credits treated like "feat." when they name credited artists, and manual review decisions (ledger v4). Total: **165 tests passing**.

Live, `sync --mapped --retry-review` re-checked only the unresolved tracks:

| Unique tracks | Before | After |
| --- | --- | --- |
| Needing review | 345 | 114 |
| Auto-resolved as identical releases | 0 | 225 |
| Spotify-unavailable but matched on TIDAL | 0 | 5 |

The fallback search surfaced the real Artist 31 "Tune 11" and Artist 4 "Left Hand" as top review candidates; both have a different ISRC from Spotify, so they stay for human review. `tidal-sync --apply` then added 287 tracks with 41 POSTs, all readback-verified; a repeat apply sent zero POSTs. The auto-resolved list is in the private `data/review/auto-resolved-releases.csv`. A private review page stores decisions per Spotify track; decisions are imported with `review import`.

### Review decisions for local files and skips — 2026-10-13

At the user's request, the review page and `review import` accept two more decisions: `local` (already in Music.app) and `skip` (not wanted). Both settle the track without adding anything to TIDAL, and both are excluded from "needs review" counts, the plan's unresolved positions and the summary report. Three new tests; total **168 passing**. The review page was republished to the same link; the one decision already saved there was preserved.

### Read-only local library check — 2026-10-13

Added `local scan` and `local match` (mutagen tag reading, ledger v5 `local_files`). Fixes found while validating: semicolon-separated artist tags, "_High_Quality" download suffixes, "(Prod. …)" credits, and Artist 35 now compare like spaces. Total: **183 tests passing**, including one proving a scanned file is byte-for-byte unchanged.

Live, against `~/Music/Music/iTunes`: 23,825 audio files indexed in about 27 seconds with zero files modified; 133 could not be parsed (mostly AIFF). Of the 2,185 mapped Spotify tracks:

| Result | Tracks |
| --- | --- |
| Already on disk | 1,046 |
| Already on disk and already in the TIDAL playlists | 1,006 |
| Matched on TIDAL, not on disk | 1,065 (12 of them with a different version on disk) |
| Under review and already on disk | 40 |

Spot checks: 12 random "found" matches were all the exact file at the same length. A loose title-and-artist search over the 1,123 "not found" tracks turned up 3 real misses, which the fixes above now catch; the rest were different versions (VIP, alternate take) or unrelated songs.

## Automated acquisition — 2026-10-13

Plan reviewed by two independent staff-level reviews before implementation (see `docs/AGENT_HANDOFF.md`). Tests: **218 passing**, Ruff and mypy clean.

- **Step 0, local index holes:** 133 unreadable files are now indexed by path (none hid a mapped track); ledger v6 rebuilt `local_assets` with UNIQUE(track_id, path) via an explicit migration (backup kept under `data/backups/`); shared-file links restored (1,201 asset rows).
- **Step 1, Music.app read-only index:** after the one-time Automation prompt, `music index` read 192 playlists, 23,859 file tracks and 729 memberships under the managed folders in about 8 seconds. Both `Nu DJ Vibes` and `Nu Vibes Vibes` matched exactly one folder each; their persistent IDs are stored. 881 Spotify tracks were recognised as already in Music.app from its own metadata. `music report`: 4 Spotify playlists already have a same-named playlist inside the managed folders, 44 would be created; per playlist entry, 319 tracks already in place, 1,090 library tracks to add, 1,282 to download, 77 needing review. Zero writes.
- **Step 2, Tidaler CLI:** installed 0.1.8 in `.venv-tidaler` (Python 3.13). With the user's approval, `tidaler cfg format_track` now ends in `[{track_id}]` so filenames cannot collide. `doctor` reports the install, session, template, ffmpeg and GUI state.
- **Step 3, download + verify + convert:** a fresh `sync --mapped` (now 2,198 unique tracks) gave a plan of 1,067 recordings, 996 excluded as owned. Live ramp with the GUI closed:

| Run | Requested | Verified by tag | Failed | Converted | Time |
| --- | --- | --- | --- | --- | --- |
| 1 (`--only`) | 1 | 1 | 0 | 1 | 5 s + convert |
| 2 (`--limit 10`) | 10 | 10 | 0 | 10 | 93 s |

  The first run exposed a bug: Tidaler's success line "You are logged in." matched the login-prompt detector, so the run stopped before verifying although the file was fine. Detection is now limited to the real PKCE prompt, verification runs first, and a regression test covers it. The verified files were 16- and 24-bit, 44.1 and 48 kHz; every AIFF kept its bit depth, cover art, ISRC and TIDAL URL, and a repeat plan for a downloaded track requests nothing. No source above 48 kHz appeared live, so resampling is covered by the 96 kHz fixture test only. The redacted logs contain no token-like strings.

| 3 (`--limit 50`) | 50 | 49 | 1 | 49 | about 20 min |

  The one failure, Artist 7 "Tune 5" (TIDAL 363088379), was reported by Tidaler as "Media not found"; it is recorded as unavailable and will not be requested again. After the ramp: 60 AIFFs (2.5 GB) from 60 FLACs (1.8 GB): 26 at 16/44.1, 2 at 16/48, 20 at 24/44.1, 12 at 24/48. No source above 48 kHz has appeared, so the resampler is exercised only by the 96 kHz fixture test.

- **Step 4, Music.app placement:** `music apply` implemented with 9 tests against an in-memory Music.app (creation inside the folder, adoption by name, ancestry guard, ambiguous names skipped, interrupted import adopted on rerun, archive policy, referenced-in-place detection, idempotent rerun). Live dry run: 44 playlists to create, 4 to adopt; 1,090 library tracks to add, 69 AIFF imports, 19 files on disk but not in Music.app (report-only), 73 unresolved, 1,208 still to download. **No live write to Music.app has been made.**
- **Step 5, orchestrator:** `dj-sync run` with per-surface flags and soft/hard outcomes; 6 tests. Total: **234 tests passing**, Ruff and mypy clean.

- **End-to-end dry run:** `dj-sync run` with no write flags completed all seven stages in about 7 minutes with zero writes: Spotify sync 2,198 tracks (144 s), TIDAL mirror preview (151 s), local index 23,958 files (78 s), Music.app index 23,859 tracks and 882 owned (28 s), download plan 50 of 1,006 remaining with 996 on disk, 60 downloaded and 1 unavailable excluded (2 s), Music.app report and placement dry run (2 s each). A first attempt had stopped hard at the Music.app index on a transient missing playlist name, which the orchestrator handled by skipping the later stages as designed; the index now tolerates that. Total: **235 tests passing**.

## First live Music.app writes — 2026-10-14

User confirmed Music.app's media folder (`~/Music/Music/iTunes/iTunes Music`) with "Keep organized" and "Copy files to Media folder" both on, then approved the ramp. Two live defects surfaced and were fixed before any track was placed: JXA playlist creation failed with -1700 (replaced by AppleScript `make new user playlist at <folder>` with a move fallback and parent verification), and reading the track IDs of an empty playlist raised -1728 (now guarded). The failed attempts changed nothing except creating the intended `Baile` playlist inside `Nu DJ Vibes`, which later runs adopted by its stored persistent ID.

Ramp on `Baile` (7 Spotify tracks): run 1 imported one AIFF (copied by Music.app into `iTunes Music/Artist 13/…`, verified by readback); run 2 imported three more and added one existing library track by persistent ID. Readback shows all five in the playlist; the remaining two are files on disk that Music.app does not list (report-only). A third run performed zero writes. Ledger: 4 downloads `imported`, intents `verified`, memberships recorded. A playlist backup was exported before the first write. Tests: **236 passing**.

## Full Music.app placement — 2026-10-14

After the Baile ramp the user approved the full apply, asked for files on disk but missing from Music.app to be added back, and chose archiving. The full run across all 48 mapped playlists needed five resumptions; each stop was a real defect found live, fixed with a test, and committed before resuming, and the durable intents meant no import ever happened twice:

1. Music.app renames copied files (Artist/Album/NN Title), so reconciliation by filename could not recognise an interrupted import; it now matches title, lead artist, length and date added.
2. A large AIFF can still be copying when the add call returns; the import now succeeds on the track ID and looks the location up afterwards.
3. One add response carried no identity at all; the import now falls back to the same lookup.
4. Adopted playlists already held some songs through a different library copy of the file, producing doubled entries; a track is now present when any linked copy, or an entry with the same title and lead artist, is already in the playlist.
5. Readback immediately after a batch of adds lagged Music.app; verification now re-reads for up to 30 s.
6. A library entry whose file is gone ("Tune 36", Artist 30) cannot be added; such entries no longer count as owning a recording and stale links are cleared on re-index.

Final state: all 48 Spotify playlists are linked to Music.app playlists (8 pre-existing inside the folders adopted by name, 40 created); 1,503 entries across them; 68 AIFF imports and 1 file added back from disk, all readback-verified; a repeat apply performs zero writes. 60 FLAC and 60 AIFF originals were moved to `imported/` subfolders (1.8 GB and 2.5 GB); the user's own FLAC in Tidaler's folder was not touched. Of 26 doubled entries, all but two mirror songs Spotify itself lists more than once; the two spurious ones ("Tune 4" in Late Night Vibes from a diagnostic probe, "Tune 15" in Chill Trap from before fix 4) were reported to the user for a decision, since the tool never removes entries. Tests: **244 passing**.

On the user's instruction, the two spurious doubled entries were removed by persistent ID through a one-off guarded script that deleted a playlist entry only when another entry with the same title and lead artist remained ("Tune 4" in Late Night Vibes, "Tune 15" in Chill Trap). Each song keeps one entry; both library tracks still exist. The tool itself still has no removal code path.

## Backlog downloaded and placed — 2026-10-14

The user asked to continue after the full placement. A loop ran `download --apply` in 50-track batches (about 6 minutes each, 21 batches): 1,002 tracks downloaded, verified by tags and converted; 13 were withdrawn from TIDAL ("Media not found") and are recorded as unavailable, so they are never requested again. Disk: 2.0 TB free before the run.

Three defects surfaced live, each fixed with a test before continuing:

1. A 32-bit FLAC source ("Tune 17", Artist 18 and Artist 34) was refused by the converter. In line with the user's 24-bit / 48 kHz cap, 32-bit sources are now reduced to 24-bit and verified by length. Batches 7–11 retried the same file until the fix; it converted in batch 12.
2. One staged file belonging to two playlists was imported for the first and then skipped for the second, because a run-wide "already added" flag ignored which playlist the add call targeted.
3. Music.app imported three files into the library without placing them in the target playlist. The tool no longer trusts the add call: after imports it waits for Music.app to reflect them (up to 30 s), then adds whatever expected track is still missing, so nothing is skipped or doubled.

Final placement pass: 937 imports and 327 adds across all 48 playlists, 994 originals archived, every playlist readback-verified; a repeat apply wrote nothing. The managed playlists hold 2,681 entries. All 1,062 downloads are `imported`. A scan for doubled entries found only songs that Spotify itself lists more than once. Archived originals: 42 GB of AIFF and 28 GB of FLAC under the two `imported/` folders. Tests: **247 passing**.
