# 2026-10-08 — claude — Search cap, identical-release rule, fallback search, review page

Written retroactively on 2026-10-15 from git history (`5c0441a` … `7fc2fd3`) and the session.

## Goal

The user asked why the sync was slow, what the review items were, which tracks had no TIDAL
match, and why some were "unavailable in Spotify"; then asked for a search cap, automatic
handling of identical releases, a fallback search, searching unavailable tracks, and a review
page.

## What happened

1. **Speed:** ~35% of tracks walked ~20 title-search pages (~300 results) for 85% of sync time.
   No plausible candidate ranked outside the top 40. Capped at 3 pages; 62 deep-search tracks
   re-matched with identical outcomes, 2.8 s per track instead of 13–15 s.
2. **Review analysis:** 224 of 303 review items were the same recording on several releases;
   79 had real differences (ISRC, explicit, version, title text).
3. **Identical-release rule** (user-approved): same ISRC, title, version, explicit flag, lengths
   within 3 s → pick Spotify's album, else earliest date, else lowest ID. 223 resolved
   (later 227).
4. **Fallback search** with bare title + lead artist when nothing is eligible; **search
   Spotify-greyed-out tracks** (Codex's code skipped them; 5 of 24 matched).
5. Artist 31 "Tune 11" lost to a karaoke version because "(with Artist 19 & Artist 14)"
   stayed in the title. Fixed: "(with …)" is stripped when it names credited artists.
6. **Review page:** a private artifact listing unresolved tracks with up to six candidates;
   decisions saved in its database; `review export`/`review import`. Template and build script
   kept in `tools/review-page/`.
7. TIDAL apply after re-matching added 287 tracks (41 POSTs); repeat 0.

## Decisions

| Decision | Why |
|---|---|
| 3-page title-search cap; ISRC uncapped | Evidence above |
| Identical-release rule | 224 review items were the same audio |
| `--retry-review` to re-check unresolved cached tracks | Matcher changes otherwise wait 24 h |
| Manual decisions table overrides search | User's review choices must stick |

## Verification

Tests 143 → 165. Review count 345 → 114 unique tracks. TIDAL mirrors 2,611 tracks.

## Commits

`5c0441a`, `30fbb4d`, `46321a0`, `2253904`, `7fc2fd3`.
