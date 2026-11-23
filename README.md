# DJ Library Sync

Curate your music in Spotify only, and let this tool keep the rest in step:

- **TIDAL:** every Spotify playlist you map gets an unlisted mirror on TIDAL ("DJ / name" or "Vibes / name").
- **Your files:** tracks you don't already own are downloaded with [Tidaler](https://pypi.org/project/tidaler/) from your own TIDAL subscription, then converted to AIFF (max 24-bit / 48 kHz) with tags and cover art.
- **Music.app:** each mapped playlist is mirrored into one of two playlist folders, so rekordbox and your USB sticks pick it up from there.

It never deletes or rewrites audio you already have, never touches playlists outside its two Music.app folders, and only ever **adds** to playlists. Every command that changes something is a dry run until you pass an `--apply` flag.

It runs locally on a Mac. Nothing is hosted.

> **Setting this up with an AI coding agent?** Point it at [Setup guide for AI agents](#setup-guide-for-ai-agents) below. It's written for the agent.

---

## What you need

| Need | Why |
|---|---|
| A Mac with Music.app | The tool drives Music.app through AppleScript. |
| [Homebrew](https://brew.sh) with `python@3.13` and `ffmpeg` | Python 3.13 runs Tidaler; ffmpeg converts FLAC to AIFF. |
| Python 3.11 or newer | Runs this tool. Tested on 3.14. |
| Spotify Premium, plus your own Spotify developer app | Reads your playlists. Each person needs their **own** app (free; dev apps allow 5 users). |
| A TIDAL subscription, plus your own TIDAL developer app | Catalog matching, the TIDAL mirrors, and lossless downloads. |
| Tidaler 0.1.8 | Does the actual downloading, using your TIDAL login. |

Downloading uses your own TIDAL subscription through Tidaler. Make sure that fits TIDAL's terms where you live; that is on you, not on this tool.

### Music.app settings

In Music → Settings → Files, turn on **Keep Music Media folder organized** and **Copy files to Music Media folder when adding to library**. The tool relies on Music.app making its own copy of each import.

Create two playlist folders (File → New → Playlist Folder) named exactly:

- `Nu DJ Vibes` (receives your playlists mapped as `DJ`)
- `Nu Vibes Vibes` (receives your playlists mapped as `Vibes`)

These names are fixed in `src/dj_sync/config.py` (`DESTINATIONS`). Change them there if you want other names.

---

## Quick start (for people)

```sh
git clone <this repo> && cd DJ-Library-Sync
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env && chmod 600 .env          # then fill in your API keys
cp config.example.yaml config.yaml              # then map your playlists
sh tools/setup-tidaler.sh                       # installs the Tidaler CLI
.venv/bin/dj-sync doctor
```

Then follow the numbered steps in the agent guide below; they're the same steps a person would do. After the first full setup, the only command you need day to day is:

```sh
.venv/bin/dj-sync run --apply-tidal --apply-download --apply-music --archive-imported
```

Before you run it, quit the Tidaler app and open Music.app.

---

## Setup guide for AI agents

You are helping a person install DJ Library Sync on their own Mac. Their library is not the one described in `vibe-engineering/`. That folder documents the original author's library (paths, playlists, counts) and is useful for **how** things work, not for what this person has.

### Rules while you work

1. **Never read, print or paste secrets.** Don't `cat` `.env`, token files under `data/`, or `~/.config/tidaler/token.json`. Ask the person to type keys into `.env` themselves. `dj-sync doctor` reports what's missing without showing values.
2. **Dry run first, then a small live step, then the rest.** Every write command defaults to a preview. Use `--limit`, `--only`, `--playlist` and `--max-writes` for the first live runs, read the result back, then widen.
3. **Never delete, move or retag the person's existing audio, and never write to playlists outside the two managed folders.** The tool enforces this; don't work around it.
4. **Don't commit anything under `data/`, `config.yaml` or `.env`.** They're git-ignored on purpose: they hold the person's library, tokens and playlist IDs.
5. **The person decides which playlists map to `DJ` and which to `Vibes`.** Spotify's API doesn't expose playlist folders, so don't guess from names. Ask them.
6. **Let the person do every browser login themselves.** That covers Spotify consent, TIDAL consent and `tidaler login`. You start the command; they click through.

### Steps

Run commands from the repo root. Each step ends with how to check it worked.

**1. Install.**
```sh
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest -q
```
Check: all tests pass. They're offline and touch no real accounts.

**2. Spotify app.** Have the person create an app at <https://developer.spotify.com/dashboard>:
- Choose the Web API.
- Add the redirect URI `http://127.0.0.1:8765/callback`. It must be exactly this; `localhost` won't work.
- Paste the Client ID into `SPOTIFY_CLIENT_ID` in `.env`. No client secret is needed.

Then run `.venv/bin/dj-sync auth spotify` and let them approve in the browser within three minutes.
Check: the command says authentication succeeded.

**3. TIDAL app.** Have them create an app at <https://developer.tidal.com/>:
- Put the Client ID and Client Secret into `TIDAL_CLIENT_ID` and `TIDAL_CLIENT_SECRET`.
- Add the redirect URI `http://127.0.0.1:8766/callback`.
- Enable the scopes `user.read playlists.read playlists.write`.

Then run:
```sh
.venv/bin/dj-sync auth tidal            # catalog access (app credentials)
.venv/bin/dj-sync auth tidal --write    # the person approves playlist writes in the browser
```
Check: both succeed. If the portal refuses a scope or the loopback redirect, stop and tell the person. Don't try workarounds with private tokens.

**4. Map playlists.**
1. Run `.venv/bin/dj-sync playlists`. It writes `data/playlist-mapping-worksheet.yaml` listing their Spotify playlists.
2. Ask the person which ones are `DJ` and which are `Vibes`, and copy those IDs into `config.yaml` under `playlists:`.
3. Set `music_dirs` to their Music media folder. It's usually `~/Music/Music/Media.localized/Music`; check Music → Settings → Files for the real location.

Note that a Spotify developer app can only read playlists the person **owns or collaborates on**; others return 403.
Check: `.venv/bin/dj-sync sync --mapped` completes and prints match counts. It is read-only.

**5. TIDAL mirrors.**
```sh
.venv/bin/dj-sync tidal-sync              # preview
.venv/bin/dj-sync tidal-sync --apply      # creates unlisted "DJ / …" and "Vibes / …" playlists and adds the matched tracks
```
Check: a repeat `--apply` makes 0 writes.

**6. Tidaler.**
1. Run `sh tools/setup-tidaler.sh`. If Homebrew's Python 3.13 lives elsewhere, set `TIDALER_PYTHON`.
2. The person runs `.venv-tidaler/bin/tidaler login` themselves.
3. Then, once:
```sh
.venv-tidaler/bin/tidaler cfg format_track "Tracks/{artist_name} - {track_title}{track_explicit} [{track_id}]"
```
The tool identifies downloaded files by the TIDAL ID in that filename.

4. In `config.yaml`, set `tidaler_bin: .venv-tidaler/bin/tidaler` and `ffmpeg_bin` (`which ffmpeg`).
5. The Tidaler GUI app must be **closed** whenever this tool downloads.

Check: `.venv/bin/dj-sync doctor` shows Tidaler installed, `track_id_in_filenames: true`, a session present, and an ffmpeg path.

**7. What's already on disk.**
```sh
.venv/bin/dj-sync local scan && .venv/bin/dj-sync local match
.venv/bin/dj-sync music index        # first run triggers macOS's "allow Terminal to control Music" prompt
```
Check: `music index` reports one `Nu DJ Vibes` and one `Nu Vibes Vibes` folder. Once the person confirms those are the right folders, run `.venv/bin/dj-sync music index --confirm-folders`.

**8. Downloads, ramped.**
```sh
.venv/bin/dj-sync download                       # plan only
.venv/bin/dj-sync download --apply --limit 1     # one track: check the AIFF in the aiff_dir has tags and cover
.venv/bin/dj-sync download --apply --limit 10
.venv/bin/dj-sync download --apply               # up to download_limit (default 50) per run
```
Check: each run reports downloaded = converted, and no failures you can't explain. Tracks TIDAL has withdrawn are marked unavailable and not retried.

**9. Music.app placement, ramped.**
```sh
.venv/bin/dj-sync music report                                          # zero writes
.venv/bin/dj-sync music apply --apply --playlist <one Spotify ID> --max-writes 1
.venv/bin/dj-sync music apply --apply --archive-imported
```
Check: in Music.app the playlist exists inside the right folder with its tracks and covers, and a repeat `apply` makes 0 writes.

**10. Steady state.** From now on, after editing playlists in Spotify, run:
```sh
.venv/bin/dj-sync run --apply-tidal --apply-download --apply-music --archive-imported
```
Re-run it if more than 50 tracks were missing. A new Spotify playlist needs a `DJ`/`Vibes` line in `config.yaml` first.

### When something goes wrong

| Symptom | Fix |
|---|---|
| "Tidaler's GUI app is running" | Quit the Tidaler app, then re-run. |
| "macOS blocked Automation access to Music" | System Settings → Privacy & Security → Automation: allow the terminal app to control Music. |
| "Music.app is not running" | Open Music.app. |
| Tidaler asks to log in, or the run says to run `tidaler login` | The person runs `.venv-tidaler/bin/tidaler login`. |
| "Expected exactly one folder playlist named …" | Create (or de-duplicate) the two folders exactly as named above. |
| "Music.app reported … playlist(s) outside Nu DJ Vibes …" | Music.app returned an incomplete playlist list. Wait a minute and retry. If it persists, a linked playlist was moved out of its folder. |
| Spotify 403 on a playlist | The person doesn't own or collaborate on it. Copy it into a playlist they own. |
| Tracks stay in review / no TIDAL match | See "Review page" below. |
| A cover is missing in Music.app | `.venv/bin/dj-sync music artwork --apply` |

---

## Optional extras

**Review page (unmatched tracks).** Tracks the matcher can't place confidently go to review rather than being guessed.
1. `.venv/bin/dj-sync review export`
2. `.venv/bin/python tools/review-page/build.py data/reports/sync-<latest>.json`
3. Publish `data/review/tidal-match-review.html` as a Claude artifact. There it saves choices as you go and offers a "Download decisions" button. Opened as a plain local file it shows everything, but it can't save, so write the decisions file by hand instead:
   `{"decisions": [{"spotify_id": "…", "decision": "match", "tidal_id": "123"}]}`. The other decisions are `no_match`, `local` and `skip`.
4. `.venv/bin/dj-sync review import <decisions.json>`

**Energy ratings.** This gives each track a 1–5 star rating in Music.app (1 Chill … 5 Peak), relative to its playlist. Rekordbox and CDJs can sort by it.
1. `.venv/bin/dj-sync energy analyse` analyses the audio with ffmpeg; about 15 minutes for 2,000 tracks.
2. `energy export`, then `.venv/bin/python tools/energy-page/build.py`, then check and correct on `data/energy/energy-ratings.html`. Like the review page, it saves only when published as a Claude artifact. The hand-written format is `{"ratings": [{"music_id": "…", "stars": 4, "home": null}]}`.
3. `energy import <ratings.json>`, then `energy apply --apply`.

Stars the person set themselves are never replaced by a guess.

**Cover repair.** `.venv/bin/dj-sync music artwork --apply` makes covers show for tracks the tool imported.

## Commands

| Command | Writes to | What it does |
|---|---|---|
| `doctor` | nothing | Settings check; no network, no secrets printed |
| `auth spotify` / `auth tidal [--write]` | `data/` tokens | Browser logins |
| `playlists` | `data/` | Lists Spotify playlists and writes a mapping worksheet |
| `sync --mapped` | `data/` ledger | Reads mapped playlists and matches them on TIDAL (read-only) |
| `tidal-sync [--apply]` | TIDAL | Creates and fills the unlisted mirrors |
| `local scan` / `local match` | `data/` ledger | Finds which tracks are already on disk (read-only) |
| `download [--apply]` | Tidaler folder, AIFF folder | Downloads missing tracks, verifies by tags, converts to AIFF |
| `music index [--confirm-folders]` / `music report` | `data/` ledger | Reads Music.app (read-only) |
| `music apply [--apply]` | Music.app | Creates playlists in the two folders, adds and imports tracks |
| `music artwork [--apply]` | covers of the tool's imports | Repairs missing covers |
| `run [--apply-…]` | as flagged | Whole pipeline; each write surface has its own flag |
| `review export` / `review import` | `data/` ledger | Decisions for unmatched tracks |
| `energy analyse/export/import/apply` | Music.app star ratings (apply only) | Energy ratings |

Developer checks: `.venv/bin/pytest -q`, `.venv/bin/ruff check src tests`, `.venv/bin/ruff format --check src tests`, `.venv/bin/mypy src`.

## How it's built

- **[vibe-engineering/](vibe-engineering/README.md):** architecture, the safety invariants and how they're enforced, every design decision with its reasoning, and a log per work session.
- **[docs/](docs/):** the earlier research (API limits, Tidaler review) and the original proof-of-concept record.
- **[SPEC.md](SPEC.md):** the original specification.
