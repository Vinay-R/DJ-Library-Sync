# 2026-10-13 — claude — Acquisition plan (two staff reviews), Music.app index, Tidaler download + AIFF, orchestrator

Written retroactively on 2026-10-15 from the plan ([../plans/2026-10-13-acquisition-plan.md](../plans/2026-10-13-acquisition-plan.md)),
git history (`0a4553c` … `14742b6`) and the session.

## Goal

The user: the tool should run Tidaler as part of the automation; audit the plan as a staff
engineer; convert FLAC to AIFF before Music.app; 50 tracks per run.

## What happened

1. Research: only the Tidaler GUI app (0.1.8) was installed; the CLI needs Python <3.14; it
   exits 0 on failure; it blocks on a PKCE prompt when logged out; it writes ISRC and the TIDAL
   URL into tags. Its download path has a trailing space.
2. Plan written and reviewed by two independent staff-level agents; their findings were merged
   (attribution by URL tag, tag-preserving conversion, v6 migration, folder IDs, import
   idempotency, process-group kill, never moving pre-existing files). User decisions: archive
   originals, download Spotify's version of the 12 "other version" tracks, add `[{track_id}]` to
   filenames, 50 per run, AIFF capped at 24-bit / 48 kHz.
3. Step 0: unreadable files indexed by path (`possibly_found`); ledger v6 rebuilt `local_assets`
   with UNIQUE(track_id, path) (29 lost links restored); backup in `data/backups/`.
4. Step 1: Music.app read-only index (JXA bulk reads, 8 s for 23,859 tracks); both managed
   folders found exactly once and confirmed; 881 tracks owned per Music.app.
5. Step 2: `.venv-tidaler` with Python 3.13; `tidaler cfg format_track` set (approved).
6. Step 3: `download` + `convert`. First live run downloaded fine but stopped: Tidaler's
   "You are logged in." matched the login detector. Fixed. Ramp 1 → 10 → 50: 60 of 61 verified
   and converted; 1 unavailable on TIDAL.
7. Step 4: `music apply` built and tested against a fake Music.app (not run live this session).
8. Step 5: `dj-sync run` with per-surface flags. End-to-end dry run: first attempt stopped hard
   on a Music.app entry without a name (index hardened); second attempt all 7 stages ok in
   ~7 minutes, 0 writes.

## Decisions

See DECISIONS 2026-10-13 entries (URL list, tag verification, AIFF cap, `[{track_id}]`, own venv,
folder IDs + index-before-download, archive, per-surface flags).

## Surprises

- Login false positive (above); `pgrep -f tidaler` matched the agent's own shell.
- ffmpeg drops AIFF tags; ffmpeg decodes 24-bit FLAC as s32.

## Verification

Tests 183 → 235. Live: Music.app index, 60 AIFFs (16/24-bit, 44.1/48 kHz, covers + ISRC
kept), repeat plan requests nothing, logs contain no token-like strings.

## Commits

`0a4553c`, `92fef51`, `4f2087e`, `8eadb74`, `0c1b107`, `d2224e3`, `59918eb`, `c672528`,
`0d03fb4`, `c198fff`, `14742b6`.
