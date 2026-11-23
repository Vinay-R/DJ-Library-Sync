# 2026-10-08 — codex — Phase 0 research, Phase 1 read-only proof of concept, guarded test-playlist writer

Written retroactively on 2026-10-15 from `docs/AGENT_HANDOFF.md`, `docs/VALIDATION.md`,
`docs/PHASE_0.md` and the first commit. Codex ran this session; the user then handed the
project to Claude Code.

## Goal

Phase 0 and Phase 1 of [../../SPEC.md](../../SPEC.md): verify the official Spotify and TIDAL
APIs and permissions, then match a ten-track Spotify sample against the TIDAL catalog with a
SQLite ledger and evidence reports. No playlist writes, no Music.app, no audio.

## What happened

1. Researched Spotify's 2026 API changes (playlist reads moved to `/items`; folders not
   exposed; 5-user dev apps) and TIDAL OpenAPI 1.10.157 (client credentials for catalog,
   user PKCE for playlists). Reviewed Tidaler's repo without running it. Inspected Music.app's
   scripting dictionary on disk only. Findings: `docs/PHASE_0.md`.
2. Built the `dj-sync` CLI: OAuth/PKCE with private token caches, read-only HTTP client with
   bounded retries, Spotify sampling with snapshot-ID consistency, TIDAL ISRC + title search,
   a conservative matcher, transactional ledger, JSON/Markdown reports.
3. Live: Spotify OAuth worked, 193 playlists discovered; the user chose "DJ - Set Picks". First run: 8 matched, 2 ambiguous (duplicate releases). A pagination bug (TIDAL links
   without `/v2`) was fixed narrowly. An album-title + full-date tie-break then resolved all 10.
4. Built a separate `test-playlist` writer for one new UNLISTED TIDAL playlist, with durable
   intent, idempotency keys, owner/marker checks and readback. TIDAL write consent first timed
   out, then succeeded.
5. The user approved creating the test playlist and repeating it. The first live apply was
   aborted by the user, who asked for a handoff document instead.

## Decisions

| Decision | Why |
|---|---|
| Spotify → TIDAL → Music.app, one-way, dry run by default | Spec non-negotiables and the user's safety rules |
| ISRC never overrides version, length or explicit conflicts | Duplicate ISRCs and reissues exist |
| A 10-item sample never replaces complete desired membership | Prevents inferring removals from partial data |
| TIDAL writes journaled before POST, verified by readback | Lost responses must not duplicate |

## Verification

99 tests passing, Ruff and mypy clean. Live read-only runs `2a4594ea…` and `b8c2d7d7…`
(8 matched / 2 ambiguous), then `91340b2d…` (10 matched). External writes: 0. The aborted apply
left no intent file and no ledger rows.

## Next (as handed off)

Resume the approved test playlist; document results. See `docs/AGENT_HANDOFF.md`.
