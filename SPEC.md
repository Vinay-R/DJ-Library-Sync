# DJ Library Sync — Product & Engineering Specification

**Status:** Planning / implementation handoff  
**Owner:** Personal DJ workflow  
**Target environment:** macOS, Python, Spotify, TIDAL, local audio files, Apple Music desktop application (`Music.app`)  
**Primary principle:** Spotify is the **source of truth for playlist membership**; local files and existing DJ metadata remain protected.

> **Codex: Start here.** Inspect the current repository first. Read this specification, create an implementation plan, verify the live Spotify and TIDAL developer APIs and permissions, and implement **Phase 1 only** before moving on. Use local credentials via OAuth/environment variables, never hard-code secrets. Begin with read-only discovery and a 10-track proof of concept. Do not touch the existing Music.app library until a dry-run reconciliation report has been reviewed.

---

## 1. Problem and desired outcome

The user discovers and organizes DJ music in **Spotify**, using a set of long-lived playlists. Historically, the user periodically created date-stamped Spotify playlist batches, used **Soundiiz** to transfer them to **TIDAL**, used **[Tidaler](https://github.com/maya-doshi/tidaler)** to get local music files, then manually imported and organized tracks in **Music.app**. Date-stamped batches were a workaround to avoid reprocessing and duplicates.

**Target workflow:** Add, remove, reorganize, or rename tracks/playlists in Spotify, and have the designated playlists in TIDAL and Music.app reflect those changes once the corresponding local recording is available. Track-level state eliminates the need for manual date-stamped batches. Reuse existing local audio files; do not make unnecessary copies. The user should only need to curate Spotify.

```text
SPOTIFY (master playlists; user edits here)
    |
    v
Python sync controller + SQLite ledger
    |                       |
    v                       v
TIDAL playlists       Library reconciliation / matching
    |                       |
    v                       |
Tidaler (existing,         |
separate local workflow)   |
    |                       |
    v                       v
Local audio files ----> Music.app managed playlists
```

**Note:** Tidaler is a separate third-party tool, not part of the official TIDAL API; inspect its current repository to understand its documented inputs/outputs. Do not add functionality that circumvents DRM, licensing, or access controls. Build the playlist synchronization and local-file ingestion around audio files the user is authorized to possess.

---

## 2. Confirmed organization and mappings

### Spotify (source)

Spotify desktop playlist folders:

- `DJ` — approximately **20 playlists**
- `Vibes` — approximately **28 playlists**

Spotify's public API does **not** expose playlist-folder membership. Consequently, provide a **one-time playlist-ID-to-group mapping** in a human-editable configuration file. Spotify playlist IDs, rather than names, are stable references. New/unmapped Spotify playlists go to a review queue for group assignment. Do not assume the 20/28 counts are exact or fixed.

### Music.app (destinations)

The user reorganized their local Music.app playlist folders specifically to map to Spotify:

| Spotify folder | Music.app destination folder | Management |
|---|---|---|
| `DJ` | **`Nu DJ Vibes`** | Spotify-synced |
| `Vibes` | **`Nu Vibes Vibes`** | Spotify-synced |

**All other Music.app playlists/folders must be protected and untouched.** Known examples:

- `Vibes` — older legacy vibe playlists
- `Archive` — legacy genre organization
- `Labels` — older label-based organization
- `Sets` — specific DJ performance/set playlists
- `Old Trax` — user's music productions
- Anything else outside `Nu DJ Vibes` and `Nu Vibes Vibes`

The two managed destination folders **already contain playlists**. They must be **adopted/reconciled**, not wiped and recreated.

Music.app is an organizer/destination, not the source of playlist membership. Local track metadata (e.g., BPM, musical key, rating, comments, artwork, cue-related fields where applicable) must not be overwritten by Spotify metadata.

---

## 3. Non-negotiable synchronization behavior

1. **Spotify is authoritative for membership** in mapped playlists, subject to safe reconciliation and explicit review for destructive playlist changes.
2. **Incremental, idempotent sync:** running the job twice with unchanged sources must not add duplicate TIDAL playlist entries, duplicate Music.app entries, or duplicate files.
3. **Distinguish a recording from playlist membership:** one local file / Music.app track can belong to multiple managed playlists.
4. **Additions:** detect a track added to a mapped Spotify playlist. If it already exists in Music.app, attach the existing track to the corresponding local playlist. Otherwise, match it in TIDAL, update the corresponding TIDAL playlist, and mark its local import as pending until a legitimate local file is available.
5. **Moves:** changing the song from Spotify playlist A to Spotify playlist B should update memberships in the corresponding Music.app playlists, without acquiring the same audio again.
6. **Removals:** removing a track from a Spotify playlist can remove it from the corresponding **managed playlist membership only** after safe setup. **Never delete the audio file or its Music.app library track.** Do not alter memberships of protected playlists such as `Sets`.
7. **Renames:** identify mapped Spotify playlists by Spotify ID and rename their Music.app counterparts as needed; don't infer identity from name alone after onboarding.
8. **Playlist creations:** newly discovered Spotify playlists require a one-time `DJ`/`Vibes` assignment because folder membership is not exposed in the Spotify API.
9. **Playlist deletion:** do not automatically delete Music.app playlists; show as an exception requiring approval. No automatic deletion of TIDAL playlists either.
10. **Ordering:** desirable but optional initially. Research/test macOS Music automation support separately; ordering must not block correct membership sync.
11. **No automatic local audio deletion** under any circumstance.
12. **Initial adoption in read-only mode:** compare Spotify, TIDAL, local Music.app, and disk before making any playlist or library changes. Existing local-only tracks must not be removed just because the first matching pass cannot identify them.
13. **Track-level checkpoints:** distinguish `discovered`, `matched`, `tidal_playlist_added`, `local_file_detected`, `music_imported`, and `music_playlist_member` (or equivalent granular states). Only mark a stage successful after confirming it actually succeeded; retry partial failures.
14. **Log and report everything:** dry-run preview, unresolved matches, unauthorized/unavailable tracks, manual-review decisions, sync diffs, retries, warnings, actions taken.
15. **One-way playlist membership sync**, never two-way. Existing non-managed Music.app playlists remain independent.

---

## 4. Track identity and matching

Spotify IDs and TIDAL IDs are different. Never assume same IDs or exact text strings. Implement a cautious matching strategy:

1. Prefer **ISRC**, if both services expose it and it identifies a compatible recording.
2. Validate artist, title, **mix/remix/version**, and duration. Same ISRC alone must not silently override contradictory version/duration evidence.
3. Normalize benign text differences (punctuation, capitalization, featuring-artist formatting), but **do not collapse** `Extended Mix`, `Original Mix`, `Radio Edit`, `Remix`, `Dub`, `VIP`, etc.
4. Require high confidence for auto-matches. Send ambiguous tracks to a review report with candidate TIDAL URLs/IDs and reasons.
5. Never silently substitute a 3-minute radio edit for a 7-minute extended version.
6. Store mappings between Spotify track IDs, TIDAL track IDs, normalized identities, local file fingerprints (if available), and Music.app persistent identifiers.
7. Treat playlist memberships independently of recordings so a reused song gets added to newly assigned genres, even if already downloaded.
8. Handle re-releases, duplicate ISRCs, unavailable tracks, alternate versions, deleted items, and missing metadata defensively.

For existing local Music.app tracks, start with available Music.app metadata and on-disk file tags/duration. Use optional acoustic fingerprints only if simpler identifiers cannot establish reliable matches. Never automatically overwrite local metadata to force a match.

---

## 5. TIDAL integration

Use **official Spotify and TIDAL APIs** for the **playlist sync**, subject to currently supported user OAuth scopes and access restrictions. Verify *actual* developer entitlements and endpoint availability before coding writes; do not rely on earlier chat assumptions. Soundiiz is **not a requirement** if authorized developer APIs can read/search/create/update the relevant playlists.

Expected capabilities to validate:

- Authenticate both personal accounts via an appropriate OAuth flow, securely store/refresh tokens.
- Read specified Spotify playlists and tracks (including pagination).
- Search and retrieve candidate TIDAL recordings.
- Create or adopt corresponding TIDAL playlists and add missing track IDs without duplicates.
- Persist playlist ID maps and track ID maps.
- Respect service rate limits and development-mode restrictions.

If TIDAL playlist-write access is unavailable for this particular app/account, **stop and report the limitation** rather than inventing endpoints or scraping private session cookies. An authorized manual transfer step is an acceptable temporary fallback, but should remain isolated from the rest of the pipeline.

**TIDAL playlists are transport and staging destinations.** Their playlist membership should track Spotify's desired state, but initial implementation should be additive only until reconciliation is proven safe.

---

## 6. Tidaler and local acquisition boundary

The existing user workflow uses **Tidaler**: https://github.com/maya-doshi/tidaler. This is how the user has been getting from TIDAL playlists to local Music.app.

Integration requirements:

- Inspect Tidaler's **current** README, CLI, export paths, playlist output/metadata conventions, and capabilities before planning an adapter. Do not assume specific flags until verified.
- Treat Tidaler as an **independent third-party local tool**; do not implement protected-stream extraction or bypass digital rights controls.
- A local ingestion adapter should watch/read a user-configured incoming directory containing audio files the user is authorized to keep, correlate files with pending tracks, and produce a reviewable import plan.
- If a file already exists in the library, reuse it; no unnecessary duplicates.
- Record missing, ambiguous, or unsupported files and wait for user action rather than fabricating success.
- Avoid direct modification of Tidaler state unless its public interface explicitly supports it.

Important separation: a track being added to a TIDAL playlist **does not mean** the corresponding local file exists or has been imported into Music.app.

---

## 7. macOS Music.app integration

Implement with macOS automation (AppleScript/JXA or another supported local mechanism, verified on the user's macOS/Music version).

Operations to validate independently:

- Enumerate folder playlists and their child playlists, ideally using persistent identifiers and verified folder relationships.
- Enumerate existing track entries, identifiers, names, artists, durations, local file references, and memberships where available.
- Add an **existing library track** to a playlist without importing another copy.
- Import a new, authorized local file and add it to one or multiple managed playlists.
- Create and rename playlists **inside the correct managed folder**.
- Remove a track **from a managed playlist only**, leaving the underlying library entry, audio file, and membership in other playlists untouched.
- Safely test ordering if feasible, without making it a prerequisite.

**Hard safety boundary:** A centralized guard must block every write operation if the destination is not a verified descendant of `Nu DJ Vibes` or `Nu Vibes Vibes`. Do not rely on playlist name equality alone, since older playlists can have duplicate names elsewhere. For ambiguous folder identity or missing authorization, fail closed.

**Initial read-only adoption:** Build a reconciliation report that compares a sample of current Spotify and Music.app playlists, differentiates local-only tracks from missing Spotify tracks, identifies uncertain matches, and proposes changes with **zero** writes.

**Pre-change backup:** Before enabling Music.app writes, export/save copies of current relevant playlist metadata and the source mappings and obtain explicit user approval for any bulk removals.

---

## 8. Suggested implementation and persistence

Prefer a simple macOS-local Python CLI with SQLite; no web UI needed for MVP.

Suggested modules (adapt to the repo, don't generate scaffolding merely for its own sake):

```text
dj-library-sync/
  SPEC.md
  README.md
  .gitignore
  .env.example
  config.example.yaml
  pyproject.toml
  src/dj_sync/
    cli.py
    config.py
    db.py
    spotify_client.py
    tidal_client.py
    track_matcher.py
    planner.py
    local_files.py
    music_app.py
    reports.py
  tests/
  data/                # ignored in Git; local SQLite & private reports
```

SQLite should model at least:

- `tracks`: canonical internal ID, artist/title/version, identity metadata.
- `service_tracks`: track ↔ Spotify/TIDAL IDs, ISRCs, duration, confidence/evidence.
- `spotify_playlists`: Spotify IDs, group (`DJ` or `Vibes`), Music.app destination mappings, optional TIDAL playlist mappings.
- `desired_memberships`: latest fetched Spotify track ↔ playlist relationships.
- `local_assets`: local file references, hashes/fingerprints when computed, Music.app IDs, file-import verification.
- `sync_status` / `sync_events`: granular per-step status, timestamps, errors, retry counts.
- `match_reviews`: ambiguous/unavailable recording decisions.

Do not use a single `downloaded: true` flag to represent everything. Distinguish track acquisition from playlist membership.

**Configuration:** mapping of Spotify playlist IDs to `DJ` or `Vibes`, destination Music.app folder identifiers, incoming directory, sync schedule, and dry-run behavior. Store secrets only in secure local config/environment/OS keychain as appropriate; never commit them.

---

## 9. Operational controls and safety

- **Dry-run by default**, with an explicit `--apply` or equivalent to make changes.
- Initial operation must be read-only; don't send API playlist writes or Music.app mutations until user explicitly enables them.
- Support `sync --source spotify --limit 10 --dry-run` or equivalent MVP command.
- Clear `diff` output for proposed adds/removals/renames per playlist.
- Human review queue for uncertain matches and destructive playlist changes.
- Persistent idempotency keys or equivalent reconcile-against-current-state logic.
- Retry transient HTTP errors with exponential backoff; observe rate limits.
- Fail closed on ambiguous folder mapping, missing credentials, or Music.app Apple Events permissions.
- Structured logs with redaction of tokens, private headers, and secrets.
- Avoid overwriting DJ tags or changing Music.app's media organization settings without consent.
- No silent changes to `Sets` or legacy folders; no file deletion.
- Respect local macOS security and OS prompts for Music.app automation.
- Provide a clear exit/rollback plan for playlists (backups/diffs and reverse operations), although never claim rollback can restore unrelated application state automatically.

Potential later scheduling: user-invoked `sync now`, then a Mac `launchd` schedule after successful manual tests. Scheduling should not be configured without approval.

---

## 10. Build phases and acceptance criteria

### Phase 0 — Current-environment/API validation and specification

- Inspect existing repo and Tidaler documentation.
- Verify official Spotify/TIDAL auth and exact API permissions; document limitations.
- Confirm Music.app scripting features in read-only mode.
- Produce an implementation plan, `.env.example`, setup instructions, and risk list.

**Pass:** clear supported API approach, no hard-coded secrets, no user library changes.

### Phase 1 — Spotify/TIDAL proof of concept (FIRST IMPLEMENTATION)

1. Implement Spotify and TIDAL account OAuth if the official APIs allow the needed scopes.
2. Fetch one selected Spotify playlist and **only 10 tracks**.
3. Search TIDAL for equivalent recordings; output matched/unmatched/ambiguous report with evidence and confidence.
4. Persist mappings and a minimal SQLite ledger.
5. In read-only mode, plan the TIDAL playlist operations.
6. Only after explicit approval, optionally create/update a **test** TIDAL playlist.
7. Repeat to verify no duplicate entries.

**Pass:** user sees 10 real Spotify tracks, defensible candidate TIDAL matches, a repeatable dry run, and (when authorized) a duplicate-free test playlist. No Music.app changes.

### Phase 2 — Complete mapped Spotify ↔ TIDAL playlist sync

- Map Spotify playlist IDs to the two groups; do not assume API folder support.
- Build incremental processing for all selected playlists, pagination, updates, rename detection, member additions/removals, and audit logs.
- Adopt existing TIDAL playlists rather than blindly create duplicates.
- Validate same track appearing across multiple playlists.

**Pass:** repeatable idempotent sync, with a review queue for errors and deletions.

### Phase 3 — Index existing Music.app library (READ-ONLY)

- Index existing tracks and memberships, especially `Nu DJ Vibes` and `Nu Vibes Vibes`.
- Build Spotify ↔ local track matching and identify unmatched/local-only records.
- Verify folder identity and backup exports.
- Produce reconciliation reports with no changes.

**Pass:** no writes; useful report of adds/removals/ambiguous matches.

### Phase 4 — Local-file ingestion and Music.app updates

- Ingest **authorized** local audio files from a configurable directory.
- Match to pending playlist entries or report ambiguities.
- Reuse existing Music.app entries; never duplicate a track merely to add another playlist membership.
- Apply managed playlist additions with review and post-action verification.
- Add safe removals/renames only after explicit opt-in and full test coverage.

**Pass:** one Spotify addition eventually becomes one correct local track linked to one or more corresponding managed Music.app playlists, without touching legacy folders.

### Phase 5 — Reliability/automation

- Retry queue, background scheduling (opt-in), error digest, simple command-line reports.
- Optional exact ordering, advanced fingerprints, and folder-mapping UX.

---

## 11. Open implementation questions to surface, not guess

1. What is the user's current macOS and Music.app version, and is Apple Events automation enabled?
2. Are the Spotify `DJ`/`Vibes` playlist IDs available for mapping, and which local playlist names/IDs correspond to them?
3. Can the user's TIDAL developer app actually obtain permissions to create/update user playlists?
4. Which Tidaler output folder and audio formats does the user currently use? Does their workflow produce useful manifest/playlist metadata?
5. Does the user want strict order mirroring, or is correct playlist membership sufficient?
6. Are preexisting entries inside the managed Music.app folders eligible for removal after initial reconciliation? **Default no** until explicitly reviewed.
7. Should Music.app additions be triggered automatically after file ingestion, or only upon manual `--apply` during the early phases?

These questions do **not** block Phase 0 or read-only Phase 1 implementation.

---

## 12. Codex kickoff instruction

> Read `SPEC.md` before coding. Inspect the working tree and do not overwrite existing project files. Verify up-to-date Spotify and TIDAL API endpoints/scopes and review Tidaler's published interface. Start by implementing a minimal, tested Python CLI for a **read-only ten-track Spotify→TIDAL matching proof of concept** with SQLite persistence, safe OAuth handling, and a clear report. Do not manipulate macOS Music.app, existing TIDAL playlists, audio files, or protected folders at this stage. Document exact setup and the next step for testing a new TIDAL playlist after approval. Explicitly report any API capability that is not actually available for the current developer accounts. Then stop and show results.

