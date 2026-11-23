"""Energy ratings: a 1-5 star guess per track, relative to its playlist, that the user corrects.

Spotify's audio features are closed to new apps, so the guess comes from the audio itself:
ffmpeg measures loudness, low-end weight, brightness, how much of the track sits near full
power, and how busy the hi-hat range is. Within each playlist those measures become
z-scores, a weighted sum ranks the tracks, and the ranking is cut into five equal groups.

Stars live in Music.app's own rating (which rekordbox and the CDJs read). The user's stars
always win: a rating the user set (in Music.app or on the energy page) is never replaced by
a guess. A track in several playlists is rated against one "home" playlist: DJ before
Vibes, then the smallest; choosing stars for a track on the page makes that playlist home.
"""

import json
import math
import re
import statistics
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .config import DESTINATIONS
from .db import Ledger
from .errors import SyncError
from .music_app import MusicApp

WINDOW = 4800  # samples per RMS window (~0.1 s at 48 kHz)
ONSET_WINDOW = 1024  # ~21 ms, for hi-hat-range hits
# Weights of each z-scored measure in the energy score. Loudness barely separates modern
# masters, so rhythm density and time-at-full-power carry most of the weight.
WEIGHTS = {"busy": 1.0, "sustain": 1.0, "high": 0.6, "low": 0.6, "loud": 0.4, "dynamics": -0.6}


def _rms_series(path: str, ffmpeg: str) -> dict[str, list[float]]:
    """Per-window RMS levels (dB) for the full signal, <150 Hz, >3 kHz and >3 kHz fine."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        branches = {
            "full": f"asetnsamples={WINDOW}",
            "low": f"lowpass=f=150,asetnsamples={WINDOW}",
            "high": f"highpass=f=3000,asetnsamples={WINDOW}",
            "fine": f"highpass=f=3000,asetnsamples={ONSET_WINDOW}",
        }
        parts = [
            f"[0:a]aformat=channel_layouts=mono,asplit={len(branches)}"
            + "".join(f"[i{k}]" for k in branches)
        ]
        maps: list[str] = []
        for k, chain in branches.items():
            parts.append(
                f"[i{k}]{chain},astats=metadata=1:reset=1,ametadata=print:"
                f"key=lavfi.astats.Overall.RMS_level:file={d / (k + '.txt')}[o{k}]"
            )
            maps += ["-map", f"[o{k}]", "-f", "null", "-"]
        done = subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-i", path, "-filter_complex", ";".join(parts)]
            + maps,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if done.returncode != 0:
            raise SyncError(f"ffmpeg could not analyse '{Path(path).name}'.")
        out: dict[str, list[float]] = {}
        for k in branches:
            text = (d / f"{k}.txt").read_text()
            out[k] = [
                -120.0 if v == "-inf" else float(v)
                for v in re.findall(r"RMS_level=(-inf|-?[\d.]+)", text)
            ]
        return out


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return -120.0
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


def measures(series: dict[str, list[float]]) -> dict[str, float]:
    """Energy measures from RMS series. Silence (< -60 dB) is ignored."""
    full = [v for v in series["full"] if v > -60]
    if len(full) < 10:
        raise SyncError("Too little audio to analyse.")
    peak = _quantile(full, 0.9)
    fine = series["fine"]
    # A "hit" is a window at least 4 dB above the previous one in the >3 kHz band.
    hits = sum(1 for a, b in zip(fine, fine[1:], strict=False) if b - a >= 4 and b > -50)
    seconds = len(series["full"]) * WINDOW / 48000
    return {
        "loud": round(peak, 2),
        "low": round(_quantile([v for v in series["low"] if v > -60], 0.9), 2),
        "high": round(_quantile([v for v in series["high"] if v > -70], 0.9), 2),
        "sustain": round(sum(1 for v in full if v > peak - 6) / len(series["full"]), 3),
        "dynamics": round(peak - _quantile(full, 0.1), 2),
        "busy": round(hits / max(seconds, 1.0), 3),
    }


def analyse_file(path: str, ffmpeg: str) -> dict[str, float]:
    return measures(_rms_series(path, ffmpeg))


def guesses(scores: dict[str, float]) -> dict[str, int]:
    """Cut one playlist's scores into five equal groups: 5 = highest energy."""
    ordered = sorted(scores, key=lambda k: scores[k])
    n = len(ordered)
    if n == 1:
        return {ordered[0]: 3}
    # Spread the ranking evenly over 1-5 so small playlists still use the whole range.
    return {k: 1 + round(4 * i / (n - 1)) for i, k in enumerate(ordered)}


def playlist_scores(features: dict[str, dict[str, float]]) -> dict[str, float]:
    """Weighted z-scores within one playlist."""
    out = {k: 0.0 for k in features}
    if len(features) < 2:
        return out
    for name, weight in WEIGHTS.items():
        values = [f[name] for f in features.values()]
        mean = statistics.fmean(values)
        sd = statistics.pstdev(values) or 1.0
        for k, f in features.items():
            out[k] += weight * (f[name] - mean) / sd
    return out


# ---------- ledger ----------


def _memberships(ledger: Ledger) -> list[tuple[str, str, str, str]]:
    """(music track id, playlist id, playlist name, group) for every managed playlist entry."""
    marks = ",".join("?" * len(DESTINATIONS))
    return [
        (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
        for r in ledger.connection.execute(
            f"""SELECT DISTINCT m.track_id, s.music_persistent_id, s.name, s.group_name
            FROM music_memberships m JOIN spotify_playlists s
            ON s.music_persistent_id=m.playlist_id WHERE s.group_name IN ({marks})""",
            tuple(DESTINATIONS),
        )
    ]


def analyse(
    ledger: Ledger,
    ffmpeg: str,
    *,
    limit: int | None = None,
    progress: Any = None,
    workers: int = 4,
) -> dict[str, Any]:
    """Measure every managed track not yet measured (or whose file moved). Resumable."""
    c = ledger.connection
    tracks = {t for t, _, _, _ in _memberships(ledger)}
    done = {
        str(r[0]): str(r[1]) for r in c.execute("SELECT music_id, location FROM energy_features")
    }
    todo = []
    for music_id in sorted(tracks):
        row = c.execute(
            "SELECT location FROM music_tracks WHERE persistent_id=?", (music_id,)
        ).fetchone()
        if row and row[0] and done.get(music_id) != row[0]:
            todo.append((music_id, str(row[0])))
    failed: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "tracks": len(tracks),
        "to_analyse": len(todo),
        "analysed": 0,
        "failed": failed,
    }
    batch = todo[:limit] if limit else todo
    # ffmpeg does the work; a few at once keeps the cores busy. Writes stay on this thread.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyse_file, loc, ffmpeg): (mid, loc) for mid, loc in batch}
        for i, future in enumerate(as_completed(futures)):
            music_id, location = futures[future]
            try:
                feats = future.result()
            except (SyncError, OSError, subprocess.TimeoutExpired) as exc:
                failed.append({"file": Path(location).name, "error": str(exc)})
                continue
            with c:
                c.execute(
                    "INSERT OR REPLACE INTO energy_features VALUES(?,?,?,?)",
                    (music_id, location, json.dumps(feats), time.time()),
                )
            result["analysed"] += 1
            if progress:
                progress(i + 1, len(batch))
    return result


def plan(ledger: Ledger, live_ratings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per playlist: each track's guess there; per track: home, guess, current and final stars.

    ``live_ratings`` maps Music.app track id -> {"stars": 0-5, "kind": "user"|"computed"}.
    """
    c = ledger.connection
    feats = {
        str(r[0]): json.loads(r[1])
        for r in c.execute("SELECT music_id, features_json FROM energy_features")
    }
    members = _memberships(ledger)
    playlists: dict[str, dict[str, Any]] = {}
    for music_id, pid, name, group in members:
        p = playlists.setdefault(pid, {"id": pid, "name": name, "group": group, "tracks": []})
        p["tracks"].append(music_id)
    per_playlist: dict[str, dict[str, int]] = {}
    scores: dict[str, dict[str, float]] = {}
    for pid, p in playlists.items():
        measured = {t: feats[t] for t in p["tracks"] if t in feats}
        scores[pid] = playlist_scores(measured)
        per_playlist[pid] = guesses(scores[pid]) if measured else {}
    choices = {
        str(r[0]): {"stars": int(r[1]), "home": r[2]}
        for r in c.execute("SELECT music_id, stars, home_playlist FROM energy_choices")
    }
    written = {str(r[0]): int(r[1]) for r in c.execute("SELECT music_id, stars FROM energy_writes")}
    order = {g: i for i, g in enumerate(DESTINATIONS)}
    tracks: dict[str, dict[str, Any]] = {}
    for music_id in {m for m, _, _, _ in members}:
        homes = sorted(
            (p for p in playlists.values() if music_id in p["tracks"]),
            key=lambda p: (order.get(p["group"], 9), len(p["tracks"]), p["name"]),
        )
        choice = choices.get(music_id)
        home = choice["home"] if choice and choice.get("home") in playlists else homes[0]["id"]
        live = live_ratings.get(music_id, {"stars": 0, "kind": "none"})
        current = int(live.get("stars") or 0)
        # Stars the user set themselves: a non-computed rating the tool did not write.
        users_own = (
            current > 0 and live.get("kind") != "computed" and written.get(music_id) != current
        )
        guess = per_playlist.get(home, {}).get(music_id)
        if choice:
            final, source = choice["stars"], "page"
        elif users_own:
            final, source = current, "yours"
        elif guess:
            final, source = guess, "guess"
        else:
            final, source = None, "not analysed"
        tracks[music_id] = {
            "home": home,
            "guess": guess,
            "current": current,
            "current_is_yours": users_own,
            "final": final,
            "source": source,
        }
    return {
        "playlists": playlists,
        "per_playlist": per_playlist,
        "scores": scores,
        "tracks": tracks,
    }


def import_choices(ledger: Ledger, data: dict[str, Any]) -> dict[str, int]:
    """Store stars chosen on the energy page: {"ratings": [{music_id, stars, home}]}."""
    rows = data.get("ratings")
    if not isinstance(rows, list):
        raise SyncError("Expected a JSON object with a 'ratings' list.")
    now = time.time()
    stored = cleared = 0
    with ledger.connection as c:
        for r in rows:
            music_id = str(r.get("music_id") or "")
            stars = r.get("stars")
            if not music_id:
                raise SyncError("A rating has no music_id.")
            if stars in (None, 0):
                c.execute("DELETE FROM energy_choices WHERE music_id=?", (music_id,))
                cleared += 1
                continue
            if not isinstance(stars, int) or not 1 <= stars <= 5:
                raise SyncError(f"Stars for {music_id} must be 1-5, got {stars!r}.")
            c.execute(
                "INSERT OR REPLACE INTO energy_choices VALUES(?,?,?,?)",
                (music_id, stars, r.get("home"), now),
            )
            stored += 1
    return {"stored": stored, "cleared": cleared}


def apply_ratings(
    ledger: Ledger, app: MusicApp, *, apply: bool = False, limit: int | None = None
) -> dict[str, Any]:
    """Write final stars to Music.app where they differ. The user's own stars are kept."""
    tracks = sorted({m for m, _, _, _ in _memberships(ledger)})
    live = app.ratings(tracks)
    p = plan(ledger, live)
    changes = [
        (music_id, t["final"])
        for music_id, t in sorted(p["tracks"].items())
        if t["final"] and t["final"] != t["current"]
    ]
    result: dict[str, Any] = {
        "mode": "applied" if apply else "dry_run",
        "tracks": len(p["tracks"]),
        "by_source": {},
        "to_change": len(changes),
        "written": 0,
        "deferred_by_limit": max(0, len(changes) - limit) if limit else 0,
    }
    for t in p["tracks"].values():
        result["by_source"][t["source"]] = result["by_source"].get(t["source"], 0) + 1
    if not apply:
        return result
    batch = changes[:limit] if limit else changes
    for start in range(0, len(batch), 100):
        chunk = batch[start : start + 100]
        app.set_ratings({m: s for m, s in chunk})
        back = app.ratings([m for m, _ in chunk])
        wrong = [m for m, s in chunk if int(back.get(m, {}).get("stars") or 0) != s]
        if wrong:
            raise SyncError(
                f"Music.app did not keep the stars for {len(wrong)} track(s); stopping."
            )
        now = time.time()
        with ledger.connection as c:
            c.executemany(
                "INSERT OR REPLACE INTO energy_writes VALUES(?,?,?)",
                [(m, s, now) for m, s in chunk],
            )
        result["written"] += len(chunk)
    return result


def page_data(ledger: Ledger, live: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Data for the energy page: playlists with their tracks sorted by energy."""
    p = plan(ledger, live)
    c = ledger.connection
    info: dict[str, dict[str, Any]] = {}
    for music_id in p["tracks"]:
        row = c.execute(
            "SELECT name, artist, duration_ms FROM music_tracks WHERE persistent_id=?",
            (music_id,),
        ).fetchone()
        spotify = c.execute(
            """SELECT st.service_id FROM local_assets la JOIN service_tracks st
            ON st.track_id=la.track_id AND st.service='spotify'
            WHERE la.music_persistent_id=? LIMIT 1""",
            (music_id,),
        ).fetchone()
        info[music_id] = {
            "title": row[0] if row else "",
            "artist": row[1] if row else "",
            "duration_ms": row[2] if row else None,
            "spotify": f"https://open.spotify.com/track/{spotify[0]}" if spotify else None,
        }
    playlists = []
    for pid, pl in sorted(
        p["playlists"].items(), key=lambda kv: (kv[1]["group"] != "DJ", kv[1]["name"].lower())
    ):
        scores = p["scores"].get(pid, {})
        rows = []
        for music_id in pl["tracks"]:
            t = p["tracks"][music_id]
            score = scores.get(music_id)
            rows.append(
                {
                    "id": music_id,
                    **info[music_id],
                    "guess": p["per_playlist"][pid].get(music_id),
                    "score": None if score is None or math.isnan(score) else round(score, 3),
                    "home": t["home"],
                    "current": t["current"],
                    "current_is_yours": t["current_is_yours"],
                }
            )
        rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
        playlists.append({"id": pid, "name": pl["name"], "group": pl["group"], "tracks": rows})
    return {"generated_at": time.time(), "playlists": playlists}
