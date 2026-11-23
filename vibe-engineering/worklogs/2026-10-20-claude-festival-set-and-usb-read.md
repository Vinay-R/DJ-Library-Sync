# 2026-10-20 — claude — USB read (rekordbox); one-time copy of "Summer Festival ‘26" into Sets

## Goal

(1) Read-only check of the rekordbox USB after an export that seemed incomplete.
(2) Push the Spotify playlist "Summer Festival ‘26" (`<spotify-playlist-id>`) into Music.app.

## What happened

1. **USB ("DJ USB", 239 GB FAT32):** 43 MB free. All 20 Nu DJ Vibes playlists exported;
   the export ran out of space in the last one: Rolling DNB 63/88 (24 missing,
   ~1.1 GB) and 1 track missing from Melodic Breaks. Nu Vibes Vibes is not on the
   USB. The USB holds two rekordbox databases: `exportLibrary.db` (2,562 tracks) and
   the older `export.pdb` (~5,955 paths, read by most CDJs). Of 5,743 audio files, 3,148
   are used only by `export.pdb`, 3 only by the Engine DJ library, and **83 (3.2 GB) by
   nothing**. First answer wrongly called 135 GB "unused" (only `exportLibrary.db` was
   checked); corrected the next turn. Read from copies of the databases (pyrekordbox in
   a scratchpad venv); nothing on the USB changed.
2. **Summer Festival ‘26:** the user chose a new playlist in Music.app's `Sets` folder named in
   their style, as a **one-time copy** (not mapped in `config.yaml`, not mirrored to TIDAL).
   - Spotify read + match: 24/24 matched; 18 already on disk.
   - 6 downloaded through Tidaler, verified and converted to AIFF.
   - Placement: a temporary `Sets` row in `music_folders` (removed in `finally`), a build
     playlist for `apply_music` (6 imported, archived), then `2026 10 - Summer Festival`
     created in `Sets` by duplicating in Spotify order. Order read back equal (24/24);
     build playlist deleted; `Sets` went from 43 to 44 playlists.
3. **Found:** one `index_music` run returned `managed_playlists: 0` / `memberships: 0`
   while 233 playlists were indexed; an immediate rerun was normal (48 / 2,702).

4. **Missing covers:** every tool import showed no cover in Music.app. Cause: the converter
   wrote an ID3v2.3 cover frame with a UTF-16 description, which Music.app registers but
   cannot read (all 160 sampled older tracks use Latin-1, or UTF-16 only under v2.4). The
   converter was fixed (`3d3558d`). Fixing the file alone was not shown; setting the image
   through Music.app was. Added `music artwork` and a cover hand-off after each import
   (`7d737d7`). Ramp: 1 track (Tune 31), 10 spot-checked by the user, then 1,050.
   1,049 files fixed, 1,060 covers handed over, 0 skipped. No duplicates: library files
   24,019 → 24,019 (identical list), Music.app tracks 24,924 → 24,924; 40 sampled tracks
   have exactly 1 cover each.

5. **Review + full sync:** page refreshed (115 items; new "same recording, label differs"
   filter with bulk accept, `c5359e4`); 117 decisions imported. `run` with all apply flags:
   TIDAL +79 tracks (48 verified), 53 requested, 52 downloaded/converted (Tune 33 by Artist 39
   withdrawn on TIDAL), 57 imports placed, 52 originals archived. 37 cover hand-offs were
   refused while Music.app finished imports; `music artwork --apply` did them later; the
   hand-off now retries (`see git log`). Genre: none from Spotify (artist genres removed),
   TIDAL (empty) or Tidaler tags; Tidaler FLACs do carry BPM/key, dropped by conversion.
   Both `imported/` archive folders were found empty (cleared outside the tool).

## Decisions

| Decision | Why |
|---|---|
| New set playlist in `Sets`, one-time copy | User's choice |
| Build playlist first, then ordered copy | `apply_music` imports first, then adds, which loses set order |
| Partial-index guard (stop unless 48 managed) in the one-off script | See the gap in Next |

## Verification

Tidaler: 6 requested, 6 downloaded, 6 converted, 0 failed. Final playlist read back in Spotify
order. `music_folders` back to DJ + Vibes only. One-off scripts in the session scratchpad.

## Next

- **Gap closed (`565ab3d`):** `index_music` now rejects a flattened tree (linked destination
  outside the managed folders), retries twice, then stops without writing. 257 tests.
- USB: user's call whether to remove the 83 unreferenced files or prune in rekordbox, then
  re-sync Nu DJ Vibes (~1.1 GB needed).
- Optional: a `dj-sync usb check` read-only command; a supported "set" mode.
- Lesson: the cover bug survived because checks read tags with mutagen, which accepts
  UTF-16; nothing checked what Music.app displays (now an ARCHITECTURE convention).
- Session closed 2026-10-20; tests 254 passing; working tree clean.
