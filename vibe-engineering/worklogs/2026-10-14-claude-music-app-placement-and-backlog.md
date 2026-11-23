# 2026-10-14 — claude — Music.app placement live, full download backlog, ten live fixes

Written retroactively on 2026-10-15 from git history (`670e096` … `f0d7f13`) and the session.

## Goal

The user said "go for it" (Music.app placement), confirmed Music.app copies files into its
media folder, then asked to add removed tracks back, archive originals, remove two spurious
entries, and "continue working" through the download backlog.

## What happened

1. Added `--playlist` / `--max-writes` ramp limits. Baile ramp: creation failed (-1700, JXA) →
   AppleScript `make … at folder`; empty-playlist read failed (-1728) → guarded. Then 1 import,
   then 4 more writes, readback ok, repeat 0 writes. The user approved Baile.
2. Full apply with archiving needed several resumptions, each a live defect fixed with a test
   first; intents meant nothing was imported twice:
   - interrupted import not recognised by filename (Music.app renames) → metadata + date added;
   - import returned before the copy finished → succeed on ID, look up location later;
   - add response with no identity → same lookup;
   - doubled entries via a second library copy → presence by any linked copy or title + artist;
   - readback lag → re-read for up to 30 s;
   - file-less library entry ("Tune 36") → never counts as owned.
   Result: 48 playlists linked (40 created, 8 adopted), 68 imports + 1 added back, 60+60
   originals archived, repeat 0 writes.
3. Two spurious doubled entries ("Tune 4" in Late Night Vibes from a probe; "Tune 15" in
   Chill Trap) removed on the user's instruction by a one-off guarded script, only
   where another copy remained. Library tracks kept.
4. Backlog loop: 21 batches of 50, ~6 min each. 1,002 downloaded and converted. 13 withdrawn
   from TIDAL. A 32-bit FLAC ("Tune 17") → capped at 24-bit (batches 7–11 retried it before the
   fix). Disk checked first: 2.0 TB free.
5. Final placement stopped twice more: a shared import was skipped in its second playlist, then
   Music.app imported 3 files without placing them → membership re-read after imports.
   Final: 937 imports + 327 adds, 994 originals archived, all 48 ok, repeat 0 writes,
   2,681 entries; only Spotify-mirroring duplicates remain.

## Withdrawn from TIDAL (13)

Artist 20 — Tune 2, Tune 16, Tune 27; Artist 7 — Tune 5; Artist 11 —
Tune 20; Artist 36 — Tune 23; Artist 28 — Tune 25; Artist 15 —
Radio; Artist 3 — Tune 32, Tune 38; Artist 9 — Tune 42; Artist 24 — Tune 39; Artist 32 — Tune 40.

## Decisions

See DECISIONS 2026-10-14 entries.

## Verification

Tests 235 → 247. Every playlist read back; repeat runs 0 writes. Archives: 42 GB AIFF, 28 GB FLAC.

## Commits

`670e096` … `f0d7f13` (20 commits).
