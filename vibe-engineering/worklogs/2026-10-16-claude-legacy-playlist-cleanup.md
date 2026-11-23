# 2026-10-16 — claude — Folder rename; legacy Music.app playlists merged and deleted

## Goal

Rename the context-system folder to `vibe-engineering/`. Then, at the user's request, find
the old (pre-tool) playlists inside `Nu DJ Vibes` / `Nu Vibes Vibes`, fold any tracks they
hold into the tool-managed versions, mark them, and delete them after the user confirms.

## What happened

1. Folder renamed with `git mv` (see the 2026-10-16 decision).
2. `music index` found 53 playlists in the managed folders: the 48 linked ones plus 5 unlinked
   legacy playlists, all in `Nu DJ Vibes` (none in `Nu Vibes Vibes`). Each was paired with the
   linked playlist sharing the most tracks:
   - `Floor fillers` → `Floor Fillers`
   - `Bass Breaks (legacy)` → `Bass Breaks`
   - `Hard DNB (legacy)` → `Hard DNB`
   - `Rolling DNB (legacy)` → `Rolling DNB`
   - `Chill Bass (legacy)` → `Chill Bass` (64/64 shared)
3. 25 old entries were present in the new playlist as another library copy of the same song;
   each was checked by title and length (all identical; one differed only in case).
4. 21 entries were genuinely missing (13 + 3 + 2 + 3 + 0). They were added as existing library
   tracks (`duplicate` by persistent ID, ancestry guard, readback-verified; no imports). A
   re-index and re-compare showed 0 missing.
5. The 5 old playlists were renamed `[OLD] …`, listed to the user, and deleted on their
   go-ahead. Library track count unchanged (24,918); managed folders now hold exactly the
   48 linked playlists.

## Decisions

| Decision | Why |
|---|---|
| Pair old→new by shared tracks, not name | One pair (`Chill Bass (legacy)`) has a different name |
| Count same title + lead artist + length as present | Avoids adding a second copy of the same song |
| Mark, then delete only on explicit go-ahead | Removals are per-case user instructions (INV: additive by default) |

The 21 added tracks are not on Spotify, so they are Music.app-only and not on TIDAL; the
additive sync keeps them.

## Verification

Pre-change membership backup: `data/backups/music-old-playlists-1792202463.json` (git-ignored).
Readback after adds; re-index after delete: 48 managed playlists, `music report` 0 writes.
One-off scripts lived in the session scratchpad; no tool code changed. Afterwards STATUS,
ARCHITECTURE (two Music.app gotchas), DECISIONS + index and the README were updated.

## Next

Unchanged: review page, then steady-state `run`.
