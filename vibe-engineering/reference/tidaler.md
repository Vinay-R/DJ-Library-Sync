For: anyone touching downloads. What this project relies on in Tidaler 0.1.8, verified 2026-10-13 against the source and live runs.

# Tidaler (third-party TIDAL downloader)

Repository: https://github.com/maya-doshi/tidaler (PyPI `tidaler`, requires Python >=3.12,<3.14).
This project never implements downloading; it runs the user's Tidaler through its CLI.

## Installs on this Mac

- GUI app: `/Applications/tidaler.app` (0.1.8, bundle id `com.exislow.TidalDlNg`).
- CLI: `.venv-tidaler/bin/tidaler` (0.1.8, Python 3.13), created by `tools/setup-tidaler.sh`.
- Both share `~/.config/tidaler/` (`settings.json`, `token.json`); `XDG_CONFIG_HOME` moves it.

## Interface used

- `tidaler dl -l FILE` — one URL per line. Track URLs: `https://tidal.com/browse/track/<id>`.
- `tidaler cfg KEY VALUE` — the public way to change a setting (used once, see below).
- `tidaler login` — run once by hand if the session is ever lost.

## Behaviour that matters

- **Exit code is 0 even when every URL fails.** Per-URL failures are only printed
  ("Media not found (ID: …). Maybe it is not available anymore.").
- **Logged out → blocking PKCE prompt** on stdin ("Paste 'Ooops' page URL here…"). The tool
  runs it with stdin closed. "You are logged in." is a success line, not a prompt.
- **`skip_existing`** compares one exact destination path inside Tidaler's own folder; it does
  not know about the Music.app library.
- **Tags written:** FLAC `ISRC`, `URL` (the TIDAL share URL containing the track ID), title,
  artist, album, cover; MP4 `isrc` and `©url`.
- **Concurrency 3, 3–5 s random delay** between downloads (Tidaler's own settings).
- Downloads can be 16-, 24- or (rarely) 32-bit FLAC at HI_RES_LOSSLESS.

## User's settings (as of 2026-10-15)

- `download_base_path`: `~/Music/Tidal-DL/FLAC ` — note the trailing space; read verbatim.
- `format_track`: `Tracks/{artist_name} - {track_title}{track_explicit} [{track_id}]` — the
  `[{track_id}]` suffix was added on 2026-10-13 with the user's approval; nothing else changed.
- `quality_audio` HI_RES_LOSSLESS, `extract_flac` true, ffmpeg `/opt/homebrew/bin/ffmpeg`.

## Rules for this project

- Close the GUI app before a download run (shared token; the tool refuses if it's running,
  detected by its bundle path).
- Never read `token.json` beyond checking it exists; never print it.
- Files the user put in `Tracks/` themselves (e.g. `My Own Track.flac`) are recorded
  as `preexisting` and never moved.
