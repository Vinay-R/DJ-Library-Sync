# DJ Library Sync — Status

> Current state + next steps. Structural map: [ARCHITECTURE.md](ARCHITECTURE.md).
> Decisions + rationale: [DECISIONS.md](DECISIONS.md) — check before re-deciding anything.
> Session history: [worklogs/](worklogs/) (dated files; recent index below, full index in
> [worklogs/INDEX.md](worklogs/INDEX.md) — read on demand only).
> Update at every session end; **rewrite, don't append** — but only ever rewrite your own
> owner-block and rows. On merge conflict, rewrite the merged truth.
> **Size budget: ≤ 20 KB; owner blocks ≤ 40 lines; milestone rows ≤ ~300 bytes.** If a block
> outgrows its budget, roll rows and history into `STATUS-archive.md`; never raise the budget.
> Not installed in this repo yet: the SessionStart hook and `scripts/ve-check.sh` that the
> [README](README.md) describes. Read STATUS → ARCHITECTURE → DECISIONS by hand at session start.

## Mission

Make Spotify the only place the user curates DJ music: the 48 Spotify playlists in the user's
`DJ` and `Vibes` folders are mirrored, incrementally and safely, into unlisted TIDAL playlists
and into Music.app playlists inside `Nu DJ Vibes` / `Nu Vibes Vibes`, acquiring missing audio
through the user's own Tidaler install and converting it to AIFF, without ever deleting,
overwriting or retagging existing library audio (spec: [../SPEC.md](../SPEC.md)).

## Active work

> Coordination point. Claim = add a row. Release = delete your row and record the outcome +
> evidence in the milestone table. Any collaborator may reap a row >7 days stale.

| Owner | Claimed | Branch | What | State / blocker |
|---|---|---|---|---|
| — | — | — | No work claimed | — |

## Milestones

> Active + recently-shipped rows only; **Verified** = evidence + worklog link, ~1 line.

| # | Phase | Status | Verified |
|---|---|---|---|
| 6 | Tidaler download → tag verification → AIFF (24-bit/48 kHz cap) | ✅ | Ramp 1→10→50, then backlog: 1,062 downloaded+converted, 13 unavailable on TIDAL ([2026-10-14](worklogs/2026-10-14-claude-music-app-placement-and-backlog.md)) |
| 7 | Music.app placement (`music apply`) inside confirmed folders | ✅ | 48 playlists linked (40 created, 8 adopted), 2,702 entries (2,681 + 21 merged from legacy playlists), 1,062 imports readback-verified, repeat run 0 writes ([2026-10-14](worklogs/2026-10-14-claude-music-app-placement-and-backlog.md)) |
| 8 | Orchestrator `dj-sync run` with per-surface apply flags | ✅ | All 7 stages passed live as a dry run, 0 writes ([2026-10-13](worklogs/2026-10-13-claude-acquisition-plan-and-build.md)); not yet run with all flags in one go |
| 9 | Review of unresolved matches | ✅ | 117 decisions imported 2026-10-20 (45 match, 43 local, 21 not on TIDAL, 8 skip); queue empty; 22 still unmatched after re-read ([2026-10-20](worklogs/2026-10-20-claude-festival-set-and-usb-read.md)) |
| 10 | Context system docs (this folder) | ✅ | Written from code, ledger and git history ([2026-10-15](worklogs/2026-10-15-claude-context-system-docs.md)); folder renamed `vibe-engineering/` ([2026-10-16](worklogs/2026-10-16-claude-legacy-playlist-cleanup.md)) |
| 11 | Legacy playlists in the managed folders | ✅ | 5 old DJ playlists merged (+21 tracks, readback-verified) and deleted on the user's go-ahead; folders hold only the 48 linked ([2026-10-16](worklogs/2026-10-16-claude-legacy-playlist-cleanup.md)) |
| 12 | One-time set copy into `Sets` | ✅ | Summer Festival ‘26 → `2026 10 - Summer Festival`, 24 tracks in Spotify order (6 downloaded) ([2026-10-20](worklogs/2026-10-20-claude-festival-set-and-usb-read.md)) |
| 13 | Covers show in Music.app for tool imports | ✅ | 1,060 repaired (`music artwork`), new imports hand covers over; no duplicate files or tracks ([2026-10-20](worklogs/2026-10-20-claude-festival-set-and-usb-read.md)) |
| 14 | Energy star ratings | 🟡 | 2,159 tracks analysed; page live; waiting on the user's pass and the rekordbox rating test ([2026-11-04](worklogs/2026-11-04-claude-energy-ratings.md)) |
| — | Scheduled runs (launchd) | ⏸ | Not set up; needs the user's go-ahead |
| — | rekordbox "Date Added" from Spotify's added date | ⏸ | Explored, deferred by the user; options in the 2026-10-15 worklog |

**Shipped roll-up:** Codex built Phase 0 research and the read-only Phase 1 matcher plus a
guarded TIDAL test-playlist writer ([2026-10-08 codex](worklogs/2026-10-08-codex-phase0-phase1-poc.md)).
The test playlist then went live (10 tracks, repeat = 0 POSTs), complete snapshots and mapped
sync were added, the user's 48 playlists were mapped from screenshots, and 48 unlisted TIDAL
mirrors were created with 2,611 tracks ([2026-10-08](worklogs/2026-10-08-claude-test-playlist-mapped-sync-tidal-mirrors.md)).
Matching improved (ISRC normalisation, 3-page search cap, identical-release rule, fallback
search, review page) ([2026-10-08](worklogs/2026-10-08-claude-matching-improvements-review-page.md)).
A read-only local library check found 1,046 tracks already on disk
([2026-10-13](worklogs/2026-10-13-claude-review-decisions-and-local-library.md)), and the
acquisition plan was reviewed twice and built ([2026-10-13](worklogs/2026-10-13-claude-acquisition-plan-and-build.md)).

## Current status → immediate next

### Shared / project-wide

Everything runs locally on the user's Mac (macOS 26, Music.app 1.6.3, rekordbox 6.8.4
installed but untouched). Live state as of 2026-10-20: 2,205 unique Spotify tracks across 48
mapped playlists; 48 TIDAL mirrors (verified 2026-10-20); every track matched on TIDAL at the 2026-10-20 Spotify read is on disk and placed in Music.app, except 14 withdrawn from TIDAL. Originals are archived after import to `~/Music/Tidal-DL/AIFF/imported/` and `~/Music/Tidal-DL/FLAC /Tracks/imported/` (both were emptied outside the tool before 2026-10-20; that is fine) (note the trailing space in `FLAC `). The user's own `My Own Track.flac` in Tidaler's
Tracks folder must never be moved. Close the Tidaler GUI app before download runs; keep
Music.app open for `music` commands. `Nu DJ Vibes` / `Nu Vibes Vibes` contain exactly the 48
linked playlists (2,765 entries); 21 of those entries are Music.app-only, merged from deleted
legacy playlists. All three write surfaces (TIDAL mirrors, Tidaler
downloads, Music.app placement) are user-approved and additive only; no removal path exists.

### @owner

**Now:** nothing running; session closed 2026-10-20. Library synced; covers show for all tool
imports; Summer Festival ‘26 copied into Sets.
**Open:**
- 22 tracks still have no TIDAL match after the review; 14 are withdrawn from TIDAL (13 in the
  2026-10-14 worklog, plus Tune 33 by Artist 39). They are placed automatically if the files
  reach the library another way.
- rekordbox USB is full (43 MB free): Rolling DNB 63/88 on the stick; ~83 files (3.2 GB)
  are referenced by no USB database. A move-to-Mac script is ready; waiting for the USB.
- Genre: no source has it (2026-10-20 worklog); playlist names as genre is the user's call.
**Next (user's call):**
1. Plug in the USB → re-check and move unreferenced files → re-sync Nu DJ Vibes + Summer Festival.
2. Steady state after Spotify edits (Tidaler closed, Music.app open):
   `.venv/bin/dj-sync run --apply-tidal --apply-download --apply-music --archive-imported`.
3. Optional: genre from playlists; carry BPM/key into AIFFs; launchd schedule; rekordbox Date Added.

## Worklog index (recent — full index: [worklogs/INDEX.md](worklogs/INDEX.md))

| Date | File | What |
|---|---|---|
| 2026-10-08 | [2026-10-08-codex-phase0-phase1-poc.md](worklogs/2026-10-08-codex-phase0-phase1-poc.md) | Codex: Phase 0 research, read-only 10-track matcher, guarded test-playlist writer |
| 2026-10-08 | [2026-10-08-claude-test-playlist-mapped-sync-tidal-mirrors.md](worklogs/2026-10-08-claude-test-playlist-mapped-sync-tidal-mirrors.md) | Test playlist live; full snapshots; 48 playlists mapped; 48 TIDAL mirrors created |
| 2026-10-08 | [2026-10-08-claude-matching-improvements-review-page.md](worklogs/2026-10-08-claude-matching-improvements-review-page.md) | Search cap, identical-release rule, fallback search, review page |
| 2026-10-13 | [2026-10-13-claude-review-decisions-and-local-library.md](worklogs/2026-10-13-claude-review-decisions-and-local-library.md) | local/skip decisions; read-only local library check (1,046 on disk) |
| 2026-10-13 | [2026-10-13-claude-acquisition-plan-and-build.md](worklogs/2026-10-13-claude-acquisition-plan-and-build.md) | Acquisition plan (2 reviews); Music.app index; Tidaler download+convert; orchestrator |
| 2026-10-14 | [2026-10-14-claude-music-app-placement-and-backlog.md](worklogs/2026-10-14-claude-music-app-placement-and-backlog.md) | Music.app placement live; backlog of 1,002 downloaded and placed; 10 live fixes |
| 2026-10-15 | [2026-10-15-claude-context-system-docs.md](worklogs/2026-10-15-claude-context-system-docs.md) | Spotify metadata + rekordbox question; this context system written |
| 2026-10-16 | [2026-10-16-claude-legacy-playlist-cleanup.md](worklogs/2026-10-16-claude-legacy-playlist-cleanup.md) | Folder renamed; 5 legacy DJ playlists merged (+21 tracks) and deleted |
| 2026-10-20 | [2026-10-20-claude-festival-set-and-usb-read.md](worklogs/2026-10-20-claude-festival-set-and-usb-read.md) | USB read (out of space; 83 unreferenced files); Summer Festival ‘26 copied into Sets |
| 2026-11-04 | [2026-11-04-claude-energy-ratings.md](worklogs/2026-11-04-claude-energy-ratings.md) | Energy stars: audio analysis, per-playlist guesses, rating page |
