"""Conservative metadata evidence scoring, never a claim of calibrated probability."""

import re
import unicodedata
from difflib import SequenceMatcher

from .models import Candidate, Match, Track

VERSION = re.compile(
    r"\b(extended|original|radio|edit|remix|mix|dub|vip|live|acoustic|instrumental|"
    r"remaster(?:ed)?|rework|bootleg|club|clean|sped|slowed|version)\b",
    re.I,
)
FEATURE = re.compile(r"\b(?:feat\.?|ft\.?|featuring|prod\.|prod by|produced by)\s+.*", re.I)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Underscores separate words (filenames, download tools), like spaces and punctuation.
    return " ".join(re.findall(r"[^\W_]+", text))


def identity(track: Track) -> tuple[str, str]:
    title = track.title
    versions: list[str] = []
    if track.version:
        versions.append(normalize(track.version))

    guests = [normalize(a) for a in track.artists[1:] if normalize(a)]

    def bracket(match: re.Match[str]) -> str:
        content = match[1]
        if VERSION.search(content):
            versions.append(normalize(content))
            return " "
        # Spotify credits guests as "(with A & B)"; drop it only when it names credited artists.
        if re.match(r"\s*with\s", content, re.I) and any(g in normalize(content) for g in guests):
            return " "
        return " " if FEATURE.search(content) else match[0]

    title = re.sub(r"[\[(]([^\])]+)[\])]", bracket, title)
    parts = re.split(r"\s+[-–—]\s+", title)
    if len(parts) > 1 and VERSION.search(parts[-1]):
        versions.append(normalize(parts.pop()))
        title = " ".join(parts)
    title = FEATURE.sub("", title)
    # Preserve inline version words even if they do not use brackets or a separator.
    inline = VERSION.search(title)
    if inline:
        versions.append(normalize(title))
    return normalize(title), " | ".join(sorted(set(versions)))


def score(source: Track, target: Track) -> Candidate:
    st, sv = identity(source)
    tt, tv = identity(target)
    title = SequenceMatcher(None, st, tt).ratio() if st and tt else 0.0
    # A shared guest artist cannot validate a different lead artist's recording.
    source_artist = normalize(source.artists[0]) if source.artists else ""
    target_artist = normalize(target.artists[0]) if target.artists else ""
    artist = (
        SequenceMatcher(None, source_artist, target_artist).ratio()
        if source_artist and target_artist
        else 0.0
    )
    same_isrc = bool(source.isrc and target.isrc and source.isrc.upper() == target.isrc.upper())
    reasons: list[str] = [f"title_similarity={title:.3f}", f"artist_similarity={artist:.3f}"]
    eligible = True
    if same_isrc:
        reasons.append("ISRC agrees")
    elif source.isrc and target.isrc:
        reasons.append("ISRC differs: manual review required")
        eligible = False
    if sv != tv:
        reasons.append(f"version conflict: {sv or 'unspecified'} vs {tv or 'unspecified'}")
        eligible = False
    if source.duration_ms is None or target.duration_ms is None:
        reasons.append("missing duration")
        duration_score, eligible = 0.0, False
    else:
        delta = abs(source.duration_ms - target.duration_ms)
        tolerance = max(3000, source.duration_ms * 0.01)
        duration_score = max(0.0, 1 - delta / 15000)
        reasons.append(f"duration_delta_ms={delta}")
        if delta > tolerance:
            reasons.append("duration conflict")
            eligible = False
    if title < 0.94 or artist < 0.94:
        reasons.append("title or artist evidence insufficient")
        eligible = False
    if (
        source.explicit is not None
        and target.explicit is not None
        and (source.explicit != target.explicit)
    ):
        reasons.append("explicit/clean edition conflict")
        eligible = False
    if target.available is False:
        reasons.append("unavailable in selected TIDAL market")
        eligible = False
    elif target.available is None:
        reasons.append("TIDAL availability not supplied; catalog identity only")
    confidence = round(
        0.45 * title + 0.30 * artist + 0.15 * duration_score + (0.10 if same_isrc else 0), 4
    )
    return Candidate(target, confidence, tuple(reasons), eligible)


def identical_recording_choice(source: Track, tied: list[Candidate]) -> Candidate | None:
    """Pick one release when every tied candidate is demonstrably the same recording.

    Requires the source ISRC on every candidate, one shared title/version identity,
    one explicit flag, and lengths within 3 seconds of each other. Then prefers the
    release matching Spotify's album (title and date, then title), then the earliest
    full release date, then the lowest catalog ID. Never used to settle a conflict.
    """
    if not source.isrc or len(tied) < 2:
        return None
    isrc = source.isrc.upper()
    if any(not c.track.isrc or c.track.isrc.upper() != isrc for c in tied):
        return None
    if len({identity(c.track) for c in tied}) != 1:
        return None
    if len({c.track.explicit for c in tied}) != 1:
        return None
    lengths = [c.track.duration_ms for c in tied]
    if any(n is None for n in lengths) or max(lengths) - min(lengths) > 3000:  # type: ignore[type-var, operator]
        return None
    album = normalize(source.album_title)

    def preference(c: Candidate) -> tuple[int, str, int, str]:
        same_album = bool(album) and normalize(c.track.album_title) == album
        same_date = same_album and c.track.album_release_date == source.album_release_date
        date = c.track.album_release_date
        full_date = date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") else "9999-99-99"
        numeric = int(c.track.id) if c.track.id.isdigit() else 2**63
        return (0 if same_date else 1 if same_album else 2, full_date, numeric, c.track.id)

    return min(tied, key=preference)


def match_track(source: Track, targets: list[Track]) -> Match:
    # Spotify playback availability says nothing about the TIDAL catalog; match anyway.
    ranked = tuple(
        sorted(
            (score(source, t) for t in {t.id: t for t in targets}.values()),
            key=lambda c: (-c.score, c.track.id),
        )
    )
    if not ranked:
        return Match("unmatched", reason="No catalog candidates returned")
    eligible = [c for c in ranked if c.eligible and c.score >= 0.89]
    if eligible:
        best = eligible[0]
        contenders = [
            c
            for c in ranked
            if c.track.id != best.track.id and c.eligible and best.score - c.score < 0.04
        ]
        if not contenders:
            return Match(
                "matched", ranked, best.track.id, "Unique high-confidence compatible candidate"
            )
        # Release metadata breaks a recording-level tie only when ISRC and all
        # compatibility gates already agree. Never use an album to rescue a remix conflict.
        same_release = [
            c
            for c in [best, *contenders]
            if source.isrc
            and c.track.isrc
            and source.isrc.upper() == c.track.isrc.upper()
            and source.album_title
            and source.album_release_date
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", source.album_release_date)
            and normalize(source.album_title) == normalize(c.track.album_title)
            and source.album_release_date == c.track.album_release_date
        ]
        if len(same_release) == 1:
            return Match(
                "matched",
                ranked,
                same_release[0].track.id,
                "Recording evidence agrees; unique album title and full release date match",
            )
        chosen = identical_recording_choice(source, [best, *contenders])
        if chosen:
            return Match(
                "matched",
                ranked,
                chosen.track.id,
                "Same recording on several releases (ISRC, title, version, explicit flag and "
                f"length agree); chose {chosen.track.album_title or 'unnamed release'} "
                f"{chosen.track.album_release_date}".rstrip(),
            )
        return Match(
            "ambiguous", ranked, reason="Multiple compatible candidates with similar scores"
        )
    plausible = any(c.score >= 0.65 for c in ranked)
    return Match(
        "ambiguous" if plausible else "unmatched",
        ranked,
        reason="Candidates require manual review" if plausible else "No credible candidate",
    )
