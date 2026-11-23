# Phase 0 findings and implementation plan

Reviewed 2026-10-08. The starting directory was empty. SPEC.md is an unmodified copy of the supplied specification. Scope: Phase 0 research and Phase 1 read-only matching; no playlist writes, audio access, Music automation, or scheduling.

## Verified capabilities and concerns (before implementation)

- Spotify's [February migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide) changes playlist reads to GET /v1/playlists/{id}/items, with `item` replacing `track`. New development apps require a Premium owner and allow five users. Item access is restricted to owned/collaborative playlists. Folder membership is unavailable: map stable playlist IDs manually.
- Later changelogs override older guidance: [March](https://developer.spotify.com/documentation/web-api/references/changes/march-2026) retains external_ids/ISRC; [July](https://developer.spotify.com/documentation/web-api/references/changes/july-2026) increases the app limit to 25 and shares development quota across the developer account. Do not assume February's one-app limit remains current.
- Use Spotify [authorization code + PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow), explicit loopback IP redirect, state validation, and only playlist-read-private / playlist-read-collaborative. No Spotify secret is needed. ISRC may still be absent on individual tracks. Local tracks, episodes, null items, and restricted recordings need explicit report rows.
- Spotify's [rate-limit guide](https://developer.spotify.com/documentation/web-api/concepts/rate-limits) describes a rolling 30-second window, variable quotas and HTTP 429 Retry-After. No universal request count is promised. Quota exhaustion must not trigger a tight retry loop.
- TIDAL [OAuth documentation](https://developer.tidal.com/documentation/api-sdk/api-sdk-authorization) supports client credentials for catalog data, and user authorization code + PKCE / refresh for user data. Prefer client credentials for this catalog-only proof of concept. Optional user OAuth uses only third-party read scopes.
- Downloaded the [official TIDAL OpenAPI schema](https://tidal-music.github.io/tidal-api-reference/tidal-api-oas.json), version **1.10.157**. Base URL is https://openapi.tidal.com/v2, JSON:API. Verified GET /tracks with filter[isrc], GET /searchResults with filter[query], and cursor pagination. Search result IDs are opaque: do not construct them from text as older examples do. Artist data must be explicitly included; durations are ISO 8601.
- The same schema marks POST /playlists and POST /playlists/{id}/relationships/items as THIRD_PARTY and lists playlists.write. It also lists internal r_usr/w_usr scopes alongside public scopes; those internal scopes are not available to third-party apps and must never be requested. Published endpoints do not prove this user's entitlement. Verify granted scopes and a new test playlist only after explicit approval. This release has no playlist mutation transport at all.
- TIDAL specifies 429 responses, but no fixed guaranteed quota was found. Serialize requests, throttle modestly, honor Retry-After, bound retries, and stop on long delays. Do not substitute private APIs/cookies on permission failures.
- Duplicate ISRCs and reissues exist. ISRC alone cannot override different mix/version, artist, explicitness, or materially different duration. Near ties need review; confidence is an evidence score, not a calibrated probability.
- A ten-item prefix is NOT a complete desired-membership snapshot. Save sample observations separately and suppress all removal inference until a complete stable snapshot exists. Never mark proposed TIDAL adds as completed actions.
- macOS is 26.3.1 (a); Music.app is 1.6.3. Inspected its bundled com.apple.Music.sdef without launching Music or sending Apple Events. It declares folder playlists, parent relationships, persistent IDs, file locations, BPM and duplicate/add operations. Runtime behavior, permissions, ancestry guards, ordering and membership-only removal remain untested until Phase 3. No Music integration is included in Phase 1.

## Plan

1. Python CLI, validated YAML group mapping, environment credentials, private local state, structured safe logging and bounded read-only HTTP client.
2. Spotify PKCE login / paginated discovery / stable ten-item sample; official TIDAL catalog search and optional read-only user OAuth.
3. Conservative matcher with candidate evidence, SQLite transactions and per-track checkpoints, JSON + Markdown report and deduplicated additive-only plan.
4. Offline transport/auth/matching/recovery/idempotency tests, lint and type checking. Live acceptance requires developer credentials, Spotify consent and a selected playlist; never label mocked data as a live result.
5. Stop. Real TIDAL test writes, Music adoption and acquisition are separate later work.

## Tidaler boundary review

Reviewed [Tidaler](https://github.com/maya-doshi/tidaler) master commit `4ace5d54e3616c401c181622a5ff0cfdc0a62964`, README, cli.py, model/cfg.py and metadata.py. Public CLI includes login, config, URL-based dl, favorites and GUI. Its configuration defaults download_base_path to `~/download`, uses a playlist path template with playlist name, position, artist and title, and can optionally emit `_playlist.m3u8`. Symlink reuse and playlist-file creation are disabled by default. Metadata code supports ISRC tags. These conventions can help a future read-only ingestion adapter, but paths/tags are configurable and are not a guaranteed synchronization manifest. This project does not install, invoke, authenticate to, or alter Tidaler. A future adapter should consume independently supplied authorized files, treat symlinks carefully, and never infer Music import success from a playlist or filename alone.


## Live validation follow-up — 2026-10-08

The user configured both apps and completed Spotify consent. Official catalog access works for this app; a live ten-track sample completed with eight confident matches and two duplicate-recording ambiguities. A repeated dry run preserved ledger state and proposed IDs. TIDAL relationship pagination links were observed without `/v2`; a narrowly scoped compatibility correction was added and regression-tested. See VALIDATION.md. Playlist writes and Music.app runtime automation remain unverified and disabled.
