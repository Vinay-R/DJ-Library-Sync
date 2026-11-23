# DJ Library Sync — Decisions

> Append-only log of load-bearing decisions: choices a future maintainer would otherwise
> re-litigate or accidentally reverse. **Never rewrite or delete entries** — if a decision
> is reversed, append a new entry and link the old one ("supersedes YYYY-MM-DD …").
> Newest first. **One decision per entry; title ≤ 120 characters; one row at the top of
> [decisions/INDEX.md](decisions/INDEX.md)** — the index is the one place supersession is recorded.
> When this file passes ~100 KB, move the oldest entries verbatim to `decisions/archive-<range>.md`.
> Heading format is a hook anchor: `## YYYY-MM-DD — <title> (<author>)`.
> Authors: `owner` = the user decided; `claude` / `codex` = the agent decided within scope.
> Entries dated before 2026-10-15 were written retroactively from the worklogs and git history.

## 2026-11-04 — Energy is a 1-5 star rating per track, relative to one home playlist; the user's stars win (owner)

- **Decision:** Music.app's star rating carries energy (1 Chill … 5 Peak). The tool guesses from the audio, ranked within the track's home playlist (DJ before Vibes, then the smallest; the user can move it), and the user corrects on the Energy Ratings page. A rating the user set is never replaced by a guess.
- **Why:** User's choice of carrier (stars, already out of 5, sortable on CDJs) and of option A for tracks in several playlists. Rejected: Comments codes (`E1`-`E5`); one folder-wide scale (loses spread inside a playlist).
- **Consequences:** A new write surface (ratings) inside the managed playlists only. Spotify's audio features are unavailable, so guesses come from ffmpeg measures.

## 2026-10-20 — Repair covers of the tool's imports by rewriting the cover frame and setting it via Music.app (owner)

- **Decision:** Covers are written as Latin-1 APIC frames. `music artwork --apply` rewrote the cover frame in 1,049 library copies of tool downloads and handed each cover to Music.app; new imports hand their cover over right after import.
- **Why:** All 1,068 imports showed no cover. The file fix alone was not shown, so Music.app's own store has to be set. The user approved after testing one track and spot-checking 10. Rejected: Music.app-only (rekordbox and CDJs read the file); leaving them blank.
- **Consequences:** A narrow exception to INV-2: only the cover frame, only on files whose TIDAL URL names a tool download, audio checksummed. No duplicates: library file list (24,019) and track count (24,924) were unchanged.

## 2026-10-20 — Set playlists go to Music.app `Sets` as one-time copies named "YYYY MM - Event" (owner)

- **Decision:** "Summer Festival ‘26" was copied once into `Sets` as `2026 10 - Summer Festival`, in Spotify order. It is not in `config.yaml`, has no TIDAL mirror and is not kept in sync. `Sets` stays outside the managed folders: the write used a temporary `music_folders` row, removed afterwards.
- **Why:** The user's choice for this set. Existing set playlists remain protected. Rejected: putting it in `Nu DJ Vibes`; keeping it synced.
- **Consequences:** INV-3 is unchanged for `run`. Repeating this for another set is a manual, scripted step until a supported "set" mode exists.

## 2026-10-16 — Merge legacy playlists in the managed folders into their linked versions, then delete them (owner)

- **Decision:** The 5 unlinked pre-tool playlists in `Nu DJ Vibes` were paired with the linked playlist sharing the most tracks; their 21 missing tracks were added to it; they were renamed `[OLD] …`, shown to the user and deleted on the user's go-ahead. Done with one-off scripts, not tool code.
- **Why:** The user wants only the Spotify-driven playlists in the folders, without losing any track the old ones held. Rejected: deleting outright; pairing by name (one pair has a different name); adding a removal feature to `music apply`.
- **Consequences:** INV-4 is unchanged (no code path removes anything). The managed folders now hold exactly the 48 linked playlists. The 21 added tracks are Music.app-only (not on Spotify, so not on TIDAL) and the additive sync keeps them.

## 2026-10-16 — Rename the context-system folder from `Vibe Engineering/` to `vibe-engineering/` (owner)

- **Decision:** The folder is `vibe-engineering/` at the repo root. Current-state files and links point there; dated worklogs and earlier entries keep the old name as history.
- **Why:** The user's choice. A path without spaces needs no quoting or `%20` escaping in shell scripts, hooks and Markdown links. Rejected: keeping `Vibe Engineering/`.
- **Consequences:** Supersedes the folder path in the 2026-10-15 adoption entry; the decision itself stands. Any future hook or `ve-check.sh` should use `vibe-engineering/`.

## 2026-10-15 — Adopt the Vibe Engineering context system, docs first, automation later (owner)

- **Decision:** Project knowledge lives in `Vibe Engineering/` (STATUS / ARCHITECTURE / DECISIONS / worklogs / plans / reference). The SessionStart hook and `scripts/ve-check.sh` from the template are not installed yet; headings are kept so they can be added.
- **Why:** Seven sessions of history were spread across `docs/AGENT_HANDOFF.md`, `docs/VALIDATION.md`, a machine-local plan file and chat. Rejected: keeping the growing handoff file (mixes state, history and rationale; already stale twice).
- **Consequences:** `docs/` stays as the Codex-era record; new state goes here. Sessions read STATUS → ARCHITECTURE → DECISIONS by hand until the hook exists.

## 2026-10-15 — Defer writing Spotify's "added" date into rekordbox (owner)

- **Decision:** Do not store or export Spotify's per-entry `added_at` for now.
- **Why:** The CDJs filter on rekordbox's own Date Added. Getting Spotify's date there needs either a rekordbox XML import (reported not to update tracks already in the collection) or editing rekordbox's encrypted `master.db`. Both need a test on the user's setup; the user chose to skip.
- **Consequences:** Options and sources are in the 2026-10-15 worklog. One track has one Date Added in rekordbox, so a future version must pick the earliest (or latest) Spotify date across playlists.

## 2026-10-14 — After imports, re-read the playlist and add what is missing instead of trusting `add … to` (claude)

- **Decision:** `music apply` treats Music.app's `add file to playlist` as a library import only. After imports it polls membership (up to 30 s) and adds every expected track still missing.
- **Why:** Live, Music.app imported three files without placing them in the target playlist, and a run-wide "already added" flag skipped a shared import in a second playlist. Rejected: a per-playlist "added to" flag (still trusts the call).
- **Consequences:** Never skips and never doubles; costs one extra membership read per playlist with imports.

## 2026-10-14 — Reduce 32-bit FLAC sources to 24-bit AIFF under the user's 24/48 cap (claude)

- **Decision:** 16 → `pcm_s16be`, 24 and 32 → `pcm_s24be`; any other bit depth fails loudly. Reduced or resampled files are verified by length (±50 ms) instead of decoded checksum.
- **Why:** TIDAL served one 32-bit FLAC; the user's chosen cap is "24-bit / 48 kHz max". Rejected: refusing 32-bit (leaves tracks unconverted forever).
- **Consequences:** Implements the 2026-10-13 conversion decision for a case it did not foresee.

## 2026-10-14 — A song is present in a playlist if any linked library copy or the same title + lead artist is there (claude)

- **Decision:** Before adding, `music apply` checks every library copy linked to the Spotify track, then title + lead artist, against the playlist's current entries.
- **Why:** The library holds several copies of some songs; adopted playlists already had them through a different copy, and exact-ID checks produced doubled entries. Rejected: exact persistent ID only.
- **Consequences:** Doubled entries now only appear where Spotify itself lists a song twice.

## 2026-10-14 — Music.app entries without a file never count as owning a recording (claude)

- **Decision:** Ownership links ignore Music.app tracks whose location is empty; stale links are cleared on re-index.
- **Why:** Music.app refuses to add a file-less entry to a playlist ("Tune 36", Artist 30), so treating it as owned blocked the track forever. Rejected: report-only.
- **Consequences:** Such tracks get downloaded again like any missing track.

## 2026-10-14 — Reconcile interrupted Music.app imports by metadata and date added, not filename (claude)

- **Decision:** An import intent left in `add_sent` is resolved by searching Music.app for title, lead artist, length (±2 s) and date added after the intent; filename is a fallback. More than one candidate stops for the user.
- **Why:** Music.app renames copied files to `Artist/Album/NN Title`, so filename matching re-imported. Rejected: re-import and dedupe later (violates INV-4/INV-6).
- **Consequences:** Also used when an add returns no identity or no location yet.

## 2026-10-14 — Add back files that are in the library folder but missing from Music.app (owner)

- **Decision:** `music apply` imports such files (Music.app keeps them in place) and places them; they are never archived.
- **Why:** The user asked for removed tracks to come back into their playlists. Rejected: leave them flagged in the report.
- **Consequences:** 17 tracks qualified at the time.

## 2026-10-14 — Create Music.app playlists with AppleScript `make … at <folder>`, then verify the parent (claude)

- **Decision:** Playlist creation is the one AppleScript call; everything else stays JXA.
- **Why:** JXA `push` + `move` failed live with -1700. Rejected: creating at the root and moving.
- **Consequences:** The script runner chooses the language from the script text.

## 2026-10-13 — The tool runs Tidaler itself on a URL list; no download-queue playlists (owner)

- **Decision:** `download` writes the TIDAL track URLs of tracks matched on TIDAL and not owned, and runs `tidaler dl -l FILE`. Placement comes from the ledger, not from TIDAL playlists or Tidaler folders.
- **Why:** The user: "the point of the database was to track the song and know where to place it". Tidaler accepts URL lists. Rejected: TIDAL queue playlists per group or per playlist; downloading whole mirror playlists (would duplicate ~1,000 owned tracks).
- **Consequences:** TIDAL mirrors are for listening only.

## 2026-10-13 — A download counts only when its TIDAL URL tag names the requested track and its length matches (claude)

- **Decision:** Tidaler's exit code is ignored. Files must parse, be quiescent 30 s, carry the expected ID in the URL tag, and match the catalog length. Same ISRC with a different ID → review; anything else → unexpected; failures retry at most twice.
- **Why:** Tidaler exits 0 when everything fails; an imported wrong file is permanent under INV-2. Rejected: trusting exit codes; ISRC-only matching (several TIDAL IDs per ISRC).
- **Consequences:** Tidaler runs in its own process group with a timeout; files already in its folder are recorded as `preexisting` and never moved.

## 2026-10-13 — Convert downloads to AIFF capped at 24-bit / 48 kHz, keep the FLAC, write tags ourselves (owner)

- **Decision:** ffmpeg converts audio only; our code writes ID3v2.3 (title, artist, album, ISRC, cover, TIDAL URL) and reads it back; without resampling, decoded checksums must match.
- **Why:** The user wants AIFF in Music.app and chose the 24/48 cap (≈17 MB/min vs ≈60 MB/min at 24/192). ffmpeg's AIFF tag mapping loses ISRC. Rejected: source resolution; 16/44.1.
- **Consequences:** FLAC masters are kept; extended by the 2026-10-14 32-bit entry.

## 2026-10-13 — Add `[{track_id}]` to Tidaler's filename template via its own `cfg` command (owner)

- **Decision:** One approved change to Tidaler's settings: `format_track` ends in `[{track_id}]`.
- **Why:** "Artist - Title" filenames collide across releases and Tidaler silently skips the second. Rejected: detecting collisions afterwards.
- **Consequences:** The GUI app shares the setting. No other Tidaler setting is ever changed.

## 2026-10-13 — Install the Tidaler CLI in its own Python 3.13 venv (claude)

- **Decision:** `tools/setup-tidaler.sh` creates `.venv-tidaler` with `tidaler==0.1.8`.
- **Why:** Tidaler requires Python <3.14 and the project runs on 3.14; only the GUI app was installed. Rejected: driving the GUI app.
- **Consequences:** `tidaler_bin` in config points at the venv; the login token is shared with the GUI.

## 2026-10-13 — Identify the managed Music.app folders once by persistent ID, and index Music.app before downloading (claude)

- **Decision:** `music index --confirm-folders` requires exactly one folder per name and stores both IDs; every write checks ancestry against them. The read-only index runs before downloads so Music.app's own holdings count as owned.
- **Why:** Names repeat ("Vibes" legacy folder; "Late Night Vibes" vs protected "Old Trax"), and mutagen cannot read 133 library files. Rejected: name-based targeting; disk scan only.
- **Consequences:** Pipeline order is fixed (see ARCHITECTURE § Pipeline order).

## 2026-10-13 — Archive tool-downloaded originals to `imported/` after a verified copy-import (owner)

- **Decision:** With `--archive-imported`, FLAC and AIFF move to `imported/` subfolders once Music.app holds a copy elsewhere. Never deleted; pre-existing and referenced-in-place files never move.
- **Why:** Keeps staging folders clean without losing masters. Rejected: deleting; leaving everything in place.
- **Consequences:** About 70 GB sits in the two `imported/` folders for the user to keep or delete.

## 2026-10-13 — Gate each write surface in `run` with its own flag; soft outcomes continue, exceptions stop (claude)

- **Decision:** `--apply-tidal`, `--apply-download`, `--apply-music`, `--archive-imported`.
- **Why:** One `--apply` would couple API writes, downloads and Music.app changes. Rejected: a single flag; stop-on-any-nonzero (stages exit 2 routinely on partial results).
- **Consequences:** `run` with no flags is a full dry run.

## 2026-10-13 — Local ownership: read tags only; same matcher rules or same ISRC + length (claude)

- **Decision:** `local scan` reads tags and length with mutagen (never writes); unreadable files are indexed by path as `possibly_found`. `local match` uses the TIDAL matcher's rules, or ISRC plus length.
- **Why:** The user wanted no duplicates of files already owned (1,046 found). Rejected: skipping unreadable files (silent duplicates); name-only matching.
- **Consequences:** `possibly_found` blocks downloads; review shows the file.

## 2026-10-13 — Review decisions `local` and `skip` settle a track without TIDAL (owner)

- **Decision:** Besides `match` and `no_match`, the review page and `review import` accept "Already in Music.app" (`local`) and "Skip".
- **Why:** The user has some files already and doesn't want some songs. Rejected: forcing a TIDAL choice.
- **Consequences:** Settled tracks leave review counts and plans; `clear` reopens them.

## 2026-10-08 — Resolve identical releases automatically (same ISRC, title, version, explicit flag, length ±3 s) (owner)

- **Decision:** Prefer the release matching Spotify's album, then the earliest full date, then the lowest ID. Conflicts are never resolved this way.
- **Why:** 224 review items were the same recording on several releases. Rejected: always asking the user.
- **Consequences:** 227 tracks auto-resolved; listed in `data/review/auto-resolved-releases.csv`.

## 2026-10-08 — Fallback title-plus-lead-artist search, and search TIDAL for Spotify-greyed-out tracks (owner)

- **Decision:** When nothing is eligible, search again with the bare title and lead artist; tracks Spotify marks unplayable are matched like any other.
- **Why:** Long Spotify titles hid real TIDAL tracks; Codex's code skipped unplayable tracks entirely. Rejected: leaving both as unmatched.
- **Consequences:** Surfaced real candidates for review; matched 5 greyed-out tracks.

## 2026-10-08 — Cap TIDAL title search at 3 result pages; ISRC lookups stay uncapped (owner)

- **Decision:** `SEARCH_PAGES = 3` in `tidal_client.py`.
- **Why:** A third of tracks walked ~20 pages (85% of sync time); no plausible candidate ranked outside the top 40; 62 re-matched tracks gave identical outcomes with the cap. Rejected: 20 pages.
- **Consequences:** Full syncs about 4× faster.

## 2026-10-08 — Mirror each mapped playlist to a new unlisted TIDAL playlist "DJ / name", additive only (owner)

- **Decision:** New playlists, never adopting legacy Soundiiz ones; unlisted; append missing tracks; never remove; drift reported.
- **Why:** User's answers to naming, visibility and adoption. Rejected: adopting legacy playlists; exact Spotify names.
- **Consequences:** 48 mirrors, 2,611 tracks verified on 2026-10-08.

## 2026-10-08 — DJ/Vibes group mapping comes from the user, never from playlist names (owner)

- **Decision:** The 48 mappings in `config.yaml` were taken from the user's screenshots of their Spotify folders.
- **Why:** Spotify's API does not expose folders; names are ambiguous ("Vibey…" playlists sit in DJ). Rejected: name heuristics.
- **Consequences:** New playlists need a manual mapping (`playlists` writes a worksheet).

## 2026-10-08 — Confident matches cached 30 days, review items 24 hours (claude)

- **Decision:** `MATCHED_TTL` 30 days; non-matched results expire after 24 h; `--retry-review` re-checks them on demand.
- **Why:** Re-querying ~2,000 matched tracks daily wastes hours. Rejected: 24 h for everything.
- **Consequences:** Matcher changes need `--retry-review` or `--refresh` to apply to old results.

## 2026-10-08 — TIDAL writes: durable intent, idempotency key, owner/marker checks and readback (codex)

- **Decision:** Save intent before every POST; verify owner, name, marker and UNLISTED before adding; recover lost creates by marker within TIDAL's idempotency window; success only after membership readback.
- **Why:** Lost responses must never create a second playlist or add duplicates. Rejected: retry-on-error.
- **Consequences:** Pattern reused for mirrors and Music.app imports (INV-6).

## 2026-10-08 — Spotify is the source of truth; one-way, dry-run by default, official APIs, conservative matching (codex)

- **Decision:** Membership flows Spotify → TIDAL → Music.app only; nothing writes without `--apply`; no private APIs; ISRC never overrides version, length or explicit conflicts.
- **Why:** From the spec's non-negotiables and the safety rules the user set. Rejected: two-way sync; name-only matching.
- **Consequences:** Ambiguous tracks go to review instead of guessing.
