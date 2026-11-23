# 2026-10-15 — claude — Spotify metadata and rekordbox question; context system written

## Goal

Answer what metadata Spotify provides (especially the date a track was added) and whether it
can reach rekordbox/the CDJs; then, at the user's request, document all work so far using the
`Vibe Engineering/` template.

## What happened

1. One live read-only request for a Baile item: Spotify returns per-entry `added_at` and
   `added_by`; track title, artists, length, explicit, ISRC, track/disc number, playability;
   album name, type, release date + precision, total tracks, images. Popularity came back
   empty; label is absent; audio features (BPM/key) are closed to new apps. The tool keeps
   title, artists, length, ISRC, explicit, album, release date, playability; `added_at` is
   discarded.
2. rekordbox 6.8.4 is installed. CDJs filter on rekordbox's own Date Added. Options found:
   rekordbox XML import with `DateAdded` (reported not to update tracks already in the
   collection), or editing the encrypted `master.db` (undocumented field, needs backup + test).
   One rekordbox date per track vs one Spotify date per playlist entry → would use the earliest.
   The user chose to skip for now.
3. Wrote this context system from code, ledger counts and git history: STATUS, ARCHITECTURE
   (9 invariants with enforcement points), DECISIONS (27 entries, retroactive ones marked),
   decisions index, worklogs for all 7 sessions, the acquisition plan copied into `plans/`, and
   reference notes for Tidaler and Music.app scripting.

## Decisions

| Decision | Why |
|---|---|
| Defer rekordbox Date Added | User's call; both routes need a live test |
| Keep `docs/` as the Codex-era record; new state here | No history rewrite |
| Hook and `ve-check.sh` not installed | Out of scope for "document the work" |

## Sources (rekordbox)

- https://pyrekordbox.readthedocs.io/en/latest/formats/xml.html
- https://pyrekordbox.readthedocs.io/en/latest/formats/db6.html
- https://community.enginedj.com/t/no-more-xml-export-in-rekordbox-6-blocks-denon-prime-users-to-access-their-rekordbox-collection/21170

## Verification

Counts cross-checked against the ledger and `git log`; 247 tests passing; no code changed.

## Next

User: review page, then steady-state `run`. Optional: install the SessionStart hook and
`scripts/ve-check.sh`; launchd; rekordbox dates.
