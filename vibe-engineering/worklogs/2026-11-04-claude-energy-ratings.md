# 2026-11-04 — claude — Energy ratings (1-5 stars per track, relative to its playlist)

## Goal

The user wants a 1-5 energy reading per track (chill → bridge → peak), relative to the other
songs in its playlist, carried in Music.app's star rating (already out of 5, read by rekordbox
and the CDJs). Option A: a track in several playlists is rated against one "home" playlist.
Auto first guess, then the user corrects. DJ and Vibes both.

## What happened

1. Checked carriers: DJ playlist tracks have empty Comments (1,064/1,088); ~2,800 older tracks
   carry bare 2-8 numbers (likely Mixed In Key energy). Star ratings: 39 DJ + 69 Vibes tracks
   user-rated (3-5★), 18 album-computed. Spotify (artist genres/audio features), TIDAL and
   Tidaler tags have no energy or genre data.
2. Built `energy.py` (`b6498eb`): ffmpeg per-window RMS for full / <150 Hz / >3 kHz and 21 ms
   >3 kHz windows → loud, low, high, sustain (time near peak), dynamics, busy (hi-hat hits/s).
   Per playlist: weighted z-scores, ranks spread over 1-5. Home = DJ before Vibes, then the
   smallest playlist; a star set on the page makes that playlist home. Stars write to
   Music.app `rating` with readback; stars the user set are never replaced by a guess (a
   rating that differs from what the tool last wrote counts as the user's).
3. Analysis: 2,159 of 2,160 tracks (1 WAV unreadable), ~15 min with 4 ffmpeg workers.
4. Sanity: averaged per playlist on one scale the order is right (Electro & Techno,
   Floor Fillers at the top; Ambient, Acoustic, Super Chill at the bottom). Against the
   108 existing user stars there is no correlation (Spearman 0.05): those stars look like
   "how much I like it", not energy. Asked the user.
5. Energy Ratings page published ((private page)):
   playlist rail, tracks sorted peak → chill, hollow-star guesses, tap to set, "keep the
   guesses for this playlist", Download ratings. Choices in db `ratings/<last hex>/items`
   (16 shards: a query returns ≤1000 docs). Functional pass: list of one shard (empty).

## Decisions

| Decision | Why |
|---|---|
| Stars, not Comments | User's choice; CDJs sort by rating |
| Home playlist per track | User chose option A |
| Guesses written only after the user's pass | Avoid churning ratings rekordbox may import |

## Next

- User: are the existing 108 stars energy or "like"? (Decides whether they are kept.)
- Test whether rekordbox picks up a changed rating for a track already in its collection.
- User rates on the page → `energy import` (or read db) → `energy apply --apply`.
