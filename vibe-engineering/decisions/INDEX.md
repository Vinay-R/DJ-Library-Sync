# Decisions index

> One row per entry in [../DECISIONS.md](../DECISIONS.md) and the archives, newest first. Agents
> read this file instead of the bodies and open an entry by date + title only when it matters.
> **Maintained:** every new DECISIONS entry gets a row at the top. **Status** is the one place
> supersession is recorded (`active` / `superseded→YYYY-MM-DD` / `partial→YYYY-MM-DD`); bodies
> are never rewritten. **Area** ∈ process, spotify, matching, tidal, acquisition, conversion,
> music-app, local-files, review, rekordbox. Summary ≤ 100 chars = what was chosen, not why.

| Date | Area | Status | Summary | File |
|---|---|---|---|---|
| 2026-11-04 | music-app | active | Energy = 1-5 stars per track vs its home playlist; guess from audio, user's stars win | DECISIONS.md |
| 2026-10-20 | music-app | active | Fix covers of tool imports: Latin-1 cover frame + set via Music.app (INV-2 exception) | DECISIONS.md |
| 2026-10-20 | music-app | active | Set playlists: one-time copy into Sets as "YYYY MM - Event", in Spotify order | DECISIONS.md |
| 2026-10-16 | music-app | active | Merge 5 legacy folder playlists into linked versions (+21 tracks), then delete them | DECISIONS.md |
| 2026-10-16 | process | active | Rename the context-system folder to vibe-engineering/ (no spaces) | DECISIONS.md |
| 2026-10-15 | process | partial→2026-10-16 | Adopt the Vibe Engineering docs; hook and ve-check not installed yet | DECISIONS.md |
| 2026-10-15 | rekordbox | active | Defer exporting Spotify's added date into rekordbox Date Added | DECISIONS.md |
| 2026-10-14 | music-app | active | After imports, re-read membership and add what is missing; don't trust add-to-playlist | DECISIONS.md |
| 2026-10-14 | conversion | active | Reduce 32-bit FLAC to 24-bit AIFF under the 24/48 cap | DECISIONS.md |
| 2026-10-14 | music-app | active | Present if any linked library copy or same title + lead artist is in the playlist | DECISIONS.md |
| 2026-10-14 | music-app | active | Music.app entries without a file never count as owned | DECISIONS.md |
| 2026-10-14 | music-app | active | Reconcile interrupted imports by title, artist, length and date added | DECISIONS.md |
| 2026-10-14 | music-app | active | Add back library-folder files missing from Music.app and place them | DECISIONS.md |
| 2026-10-14 | music-app | active | Create playlists via AppleScript make-at-folder, then verify parent | DECISIONS.md |
| 2026-10-13 | acquisition | active | Tool runs Tidaler on a URL list from the ledger; no queue playlists | DECISIONS.md |
| 2026-10-13 | acquisition | active | Download counts only if URL tag names the track and length matches; exit code ignored | DECISIONS.md |
| 2026-10-13 | conversion | partial→2026-10-14 | FLAC → AIFF capped 24-bit/48 kHz, FLAC kept, tags written by us | DECISIONS.md |
| 2026-10-13 | acquisition | active | Tidaler format_track gets [{track_id}] via its cfg command (only settings change) | DECISIONS.md |
| 2026-10-13 | acquisition | active | Tidaler CLI in its own Python 3.13 venv | DECISIONS.md |
| 2026-10-13 | music-app | active | Managed folders by confirmed persistent IDs; index Music.app before downloading | DECISIONS.md |
| 2026-10-13 | acquisition | active | Archive tool-downloaded originals to imported/ after verified copy-import | DECISIONS.md |
| 2026-10-13 | process | active | Per-surface apply flags in run; soft outcomes continue, exceptions stop | DECISIONS.md |
| 2026-10-13 | local-files | active | Read tags only; ownership via matcher rules or ISRC + length; unreadable = possibly_found | DECISIONS.md |
| 2026-10-13 | review | active | Review decisions local and skip settle tracks without TIDAL | DECISIONS.md |
| 2026-10-08 | matching | active | Auto-resolve identical releases: Spotify album, then earliest, then lowest ID | DECISIONS.md |
| 2026-10-08 | matching | active | Fallback title+artist search; search Spotify-greyed-out tracks too | DECISIONS.md |
| 2026-10-08 | matching | active | Cap title search at 3 pages; ISRC lookups uncapped | DECISIONS.md |
| 2026-10-08 | tidal | active | New unlisted "DJ / name" mirrors, additive only, never adopt legacy playlists | DECISIONS.md |
| 2026-10-08 | spotify | active | DJ/Vibes mapping from the user's screenshots, never inferred from names | DECISIONS.md |
| 2026-10-08 | matching | active | Cache confident matches 30 days, review items 24 hours | DECISIONS.md |
| 2026-10-08 | tidal | active | TIDAL writes: durable intent, idempotency key, owner/marker checks, readback | DECISIONS.md |
| 2026-10-08 | process | active | Spotify is source of truth; one-way, dry-run default, official APIs, conservative matching | DECISIONS.md |
