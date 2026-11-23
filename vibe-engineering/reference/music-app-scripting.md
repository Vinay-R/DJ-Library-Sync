For: anyone touching `music_app.py` / `music_apply.py`. Music.app 1.6.3 (macOS 26) scripting facts and live quirks, written 2026-10-15.

# Music.app scripting

Dictionary: `/System/Applications/Music.app/Contents/Resources/com.apple.Music.sdef`.
All calls go through `osascript -l JavaScript` (JXA), except playlist creation (AppleScript).
macOS asks once for Automation permission for the app running the command; a refusal shows up
as error -1743.

## User's Music.app settings (confirmed 2026-10-14)

- Media folder: `~/Music/Music/iTunes/iTunes Music`.
- "Keep Media folder organized" ON — copies are renamed to `Artist/Album/NN Title.ext`.
- "Copy files to Media folder when adding to library" ON — imports from outside are copied;
  files already inside the media folder are referenced in place.
- Managed folders (persistent IDs stored in `music_folders`): `Nu DJ Vibes` = `2C2B59D89E821452`,
  `Nu Vibes Vibes` = `006DBA037321FCDA`.

## What works

- Bulk property reads (`playlists.persistentID()`, `fileTracks.location()` …): one Apple event per
  property; the whole library (~24k tracks) indexes in about 8–30 s.
- `playlists.whose({persistentID: id})()` to address a playlist; `duplicate(track, {to: pl})`
  to add a library track to a playlist; `add(Path(file), {to: pl})` to import a file.
- AppleScript `make new user playlist at <folder playlist> with properties {name: …}` creates a
  playlist directly inside a folder.

## Live quirks (each cost a failed run once)

- JXA `push` + `move` to create a playlist fails with -1700.
- Bulk reads on an empty playlist fail with -1728; check `tracks().length` first.
- `add … to playlist` may import the file but not place it in that playlist; membership reads
  can also lag a few seconds. Re-read with polling, then add what is missing.
- An import can return before the copy finishes: the track has an ID but no location yet, or
  occasionally no usable reference at all. Look the track up by title, artist, length and
  date added.
- `parent` raises for root-level playlists; catch it.
- The library contains several copies of some songs and entries whose file is gone (location
  empty). File-less entries cannot be added to playlists ("Message not understood").
- `Date Added` and other library-managed fields are read-only from scripts.
