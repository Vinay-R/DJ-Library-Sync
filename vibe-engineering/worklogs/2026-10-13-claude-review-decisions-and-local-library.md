# 2026-10-13 — claude — "Already in Music.app" and "Skip" decisions; read-only local library check

Written retroactively on 2026-10-15 from git history (`351d774`, `4110edb`) and the session.

## Goal

1. Let the user mark review tracks as already owned or not wanted.
2. Before any downloading, find which mapped tracks the user already has on disk, to avoid
   duplicates. Library: `~/Music/Music/iTunes`.

## What happened

1. Added review decisions `local` and `skip` (plus `clear`); both settle a track with nothing
   added to TIDAL and drop it from review counts. Review page republished to the same link; the
   one decision already saved there ("Tune 37", Artist 25 → TIDAL 410334946) was preserved.
2. Library survey: ~24k audio files, 467 GB, mostly in `iTunes Music`; Music.app's current
   library is binary (no XML export), so files are read directly with mutagen (new dependency).
3. `local scan` indexed 23,825 files in ~27 s, 0 modified. Raw tags gave far better coverage
   than a first "easy" sample suggested: 23,235 from tags, 6,620 with ISRC; 133 unreadable.
4. `local match`: 1,042 of 2,185 mapped tracks found on disk; 1,002 of those were already in
   the TIDAL mirrors. Spot check: 12/12 found files exact at +0.0 s.
5. A loose title+artist search over "missing" tracks found 3 real misses; fixed semicolon
   artist separators, `_High_Quality` suffixes, "(Prod. …)" credits, and made Artist 35
   compare like spaces (one regression, "zi$$_go.wav", caught and fixed). Final: 1,046 found.
6. Review page gained an "On your disk" line, a disk filter and a bulk "Already in Music.app"
   button (38 review tracks had a file on disk).
7. The user asked whether a download-queue playlist was needed; answer: no — the ledger knows
   placement and Tidaler accepts URL lists. The user then asked for the tool to run Tidaler.

## Decisions

| Decision | Why |
|---|---|
| Read tags only; never write | INV-2 |
| Found = matcher rules or ISRC + length | Same bar as TIDAL matching |
| No queue playlists | User; ledger decides placement |

## Verification

Tests 165 → 183. Live numbers above.

## Commits

`351d774`, `4110edb`.
