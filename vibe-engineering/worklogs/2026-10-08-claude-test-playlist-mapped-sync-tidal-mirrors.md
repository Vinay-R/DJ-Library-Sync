# 2026-10-08 — claude — Test playlist live, complete snapshots, 48 playlists mapped, TIDAL mirrors created

Written retroactively on 2026-10-15 from git history (`0dd889e` … `cce7684`) and the session.

## Goal

Pick up Codex's handoff: finish the approved TIDAL test, then continue into Phase 2
(complete, mapped Spotify → TIDAL sync).

## What happened

1. Pre-flight: 99 tests green; no leftover process, intent or ledger rows from the aborted run.
   Dry run reproduced the frozen plan hash.
2. **Test playlist live:** first apply created one UNLISTED playlist and added 10 tracks
   (2 POSTs); the identical second apply made 0 POSTs, same ID, 0 duplicates.
3. First commit of the whole project (`0dd889e`).
4. Phase 2 read-only: `SpotifyClient.full()` (50-item pages, snapshot check), `sync --full`,
   `sync --mapped`, a mapping worksheet that never guesses groups, 30-day cache for confident
   matches. Live on the 39-track test playlist: 38 matched, 1 held (different ISRC).
5. The user supplied screenshots of the Spotify DJ (20) and Vibes (28) folders; all 48 names
   resolved to unique owned IDs and went into `config.yaml`.
6. Built `tidal-sync`: one new UNLISTED playlist per mapped playlist named `DJ / <name>` or
   `Vibes / <name>`, additive, reusing the test writer's guards with 50-item batches. User's
   answers: create new (ignore legacy Soundiiz playlists), prefix names, unlisted.
7. The first full mapped sync took well over an hour. Six tracks failed with TIDAL HTTP 400: Spotify returned
   lowercase/hyphenated ISRCs. Fixed by normalising at both boundaries; rerun clean.
8. Live: `tidal-sync --apply` created 48 playlists and added 2,324 tracks (121 POSTs); repeat
   apply 0 POSTs, same IDs, 2,324 memberships each once.

## Decisions

| Decision | Why |
|---|---|
| Mapping from screenshots, never names | API has no folders |
| New unlisted "DJ / name" mirrors, additive | User's choice |
| Normalise ISRCs (upper-case, no hyphens) | TIDAL 400s on Spotify's raw values |
| Matched results cached 30 days, others 24 h | Daily re-matching of ~2,000 tracks wastes hours |

## Surprises

- Spotify ISRC casing (above).
- Deep TIDAL title-search pagination made the first full sync slow; investigated next session.
- Music.app and local files were not touched in this session.

## Verification

Tests 99 → 143. Live numbers above; each apply read back from TIDAL.

## Commits

`0dd889e`, `88588e8`, `3f51fe8`, `3fd56c9`, `cce7684`.
