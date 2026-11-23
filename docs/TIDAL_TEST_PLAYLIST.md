# Isolated TIDAL test playlist

This extends the read-only matcher with a separate, explicitly applied test command. Ordinary `sync` remains read-only. No Spotify writes, Music.app automation, audio acquisition, removals, renames or existing-playlist adoption are implemented.

## Exact scope

`test-playlist --run-id RUN_ID` previews a frozen, completed matching report from the last 24 hours. It includes only confident, compatible matches from its first 1–10 source positions. Unresolved entries are excluded. It creates one playlist named `DJ Library Sync TEST — <source name>` with **UNLISTED** visibility. Unlisted is not a promise of private access: someone with its share link may be able to open it.

`--apply` is required to create or add. There is no destination-ID argument. A test stays pinned to its source snapshot, market, exact TIDAL IDs and authenticated account; a different plan/account stops for review.

## Run

```sh
.venv/bin/dj-sync test-playlist --run-id RUN_ID --dry-run
.venv/bin/dj-sync auth tidal --write
.venv/bin/dj-sync test-playlist --run-id RUN_ID --apply
.venv/bin/dj-sync test-playlist --run-id RUN_ID --apply
```

The separate write login uses `user.read playlists.read playlists.write`, stored in a separate private token cache. Catalog matching continues to use app credentials by default. Enable those public scopes in the TIDAL developer app and register the configured callback before login. Consent is not evidence that the account can actually use every published endpoint: 403 stops the operation with no private-API fallback.

## Safety and recovery

- Persist an immutable creation intent, random marker and idempotency key atomically before calling TIDAL. State files live under ignored, private `data/test-playlists/`.
- Use only official `POST /v2/playlists` and `POST /v2/playlists/{id}/relationships/items` with JSON:API bodies. No generic mutation method is added to the read-only API client.
- Check the returned playlist ID, exact name, marker, owner and UNLISTED visibility before adding anything. Refuse unexpected tracks, duplicates, identity changes or inaccessible targets. Pagination remains confined to the official origin and requested resource.
- Read current membership before adding. Deduplicate the request locally and send `meta.onDuplicates: SKIP` to handle concurrent duplicate additions. Send an `Idempotency-Key` for every POST.
- Read back all membership after adding. Only an exact once-per-ID match with the frozen plan becomes verified. SQLite `tidal_test_memberships` and `tidal_test_runs` track verified destination state separately from recording matches and local assets.
- Never blindly retry a timed-out mutation. Rerun the **same command and run ID**. Recover an uncertain creation by finding its unique persisted marker among the authenticated user's playlists. If absent, replay the same creation key only within a conservative 50-minute window of TIDAL's documented one-hour idempotency guarantee. After that, stop for manual review rather than create another playlist.
- A lost add response is handled by reading membership again; only still-missing IDs are proposed, again with server-side duplicate skipping. A failed readback remains pending, not successful.
- If the process stops after verification but before the SQLite update, rerunning re-verifies membership and records the result without another addition.
- Retain the ledger and test-intent files together. Do not delete or edit them to force retries. There is no automatic cleanup/delete or claim that unrelated app state can be rolled back.

## Release tie-breaker

ISRC, title, lead artist, version, duration, explicitness and availability checks still run first. When multiple candidates independently pass, an exact album title and full YYYY-MM-DD release-date match may select a unique release, but only with an agreeing ISRC. Missing/partial dates, multiple matching releases and incompatible remixes still require review. Matcher version 2 invalidates prior cached evidence.

The two live ambiguities were resolved this way: Spotify's original single releases each have one TIDAL entry with the same release name/date; the other entries are later releases. The release review is retained privately in `data/reports/ambiguous-release-review.json`.
