import re
from dataclasses import dataclass, field
from urllib.parse import quote

# Statuses that need no further human review. "local": the user already has the file in
# Music.app (unverified until Music.app indexing). "skipped": the user chose not to sync it.
SETTLED_STATUSES = frozenset({"matched", "local", "skipped"})

ISRC_PATTERN = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")


def normalize_isrc(value: object) -> str | None:
    """Canonical 12-character ISRC, or None. TIDAL rejects lowercase or hyphenated forms."""
    if not isinstance(value, str):
        return None
    candidate = re.sub(r"[\s-]", "", value).upper()
    return candidate if ISRC_PATTERN.fullmatch(candidate) else None


def simple_query(title: str, artists: list[str] | tuple[str, ...]) -> str:
    """Fallback search text: the bare title plus the lead artist only."""
    text = title.replace("’", "'").replace("‘", "'")
    text = re.sub(r"\s*[\[(][^\])]*[\])]", "", text)
    text = re.split(r"\s+[-–—]\s+", text)[0]
    lead = artists[0] if artists else ""
    return " ".join(f"{text} {lead}".split())[:256]


@dataclass(frozen=True)
class Track:
    service: str
    id: str
    title: str
    artists: tuple[str, ...]
    duration_ms: int | None
    isrc: str | None = None
    version: str = ""
    available: bool | None = None
    explicit: bool | None = None
    album_title: str = ""
    album_release_date: str = ""

    @property
    def url(self) -> str:
        if self.service == "spotify":
            return f"https://open.spotify.com/track/{quote(self.id, safe='')}"
        return f"https://tidal.com/browse/track/{quote(self.id, safe='')}"


@dataclass(frozen=True)
class PlaylistItem:
    position: int
    track: Track | None
    issue: str | None = None


@dataclass(frozen=True)
class Snapshot:
    id: str
    name: str
    snapshot_id: str
    items: tuple[PlaylistItem, ...]
    complete: bool


@dataclass(frozen=True)
class Candidate:
    track: Track
    score: float
    reasons: tuple[str, ...]
    eligible: bool


@dataclass(frozen=True)
class Match:
    status: str
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)
    selected_id: str | None = None
    reason: str = ""
