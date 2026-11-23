import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .errors import SyncError
from .http import ReadClient
from .models import Track, normalize_isrc, simple_query


def duration_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", value
    )
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(x or 0) for x in match.groups())
    return round((hours * 3600 + minutes * 60 + seconds) * 1000)


def next_link(document: dict[str, Any]) -> str | None:
    link = (document.get("links") or {}).get("next")
    if isinstance(link, dict):
        link = link.get("href")
    return link if isinstance(link, str) and link else None


def parse_tracks(document: dict[str, Any], identifiers: list[dict[str, Any]]) -> list[Track]:
    included = {(r["type"], r["id"]): r for r in document.get("included", [])}
    tracks = []
    for ref in identifiers:
        if ref.get("type") != "tracks":
            continue
        raw = ref if "attributes" in ref else included.get(("tracks", ref["id"]), {})
        attributes = raw.get("attributes", {})
        if not attributes:
            raise SyncError("TIDAL omitted requested track metadata.")
        artists = []
        for artist in raw.get("relationships", {}).get("artists", {}).get("data", []):
            name = included.get(("artists", artist["id"]), {}).get("attributes", {}).get("name")
            if name:
                artists.append(name)
        availability = attributes.get("availability")
        available = None if availability is None else bool(set(availability) & {"STREAM", "DJ"})
        album_refs = raw.get("relationships", {}).get("albums", {}).get("data", [])
        album = (
            included.get(("albums", album_refs[0]["id"]), {}).get("attributes", {})
            if len(album_refs) == 1
            else {}
        )
        tracks.append(
            Track(
                "tidal",
                str(ref["id"]),
                attributes.get("title", ""),
                tuple(artists),
                duration_ms(attributes.get("duration")),
                normalize_isrc(attributes.get("isrc")),
                attributes.get("version") or "",
                available,
                attributes.get("explicit"),
                album.get("title", ""),
                album.get("releaseDate", ""),
            )
        )
    return tracks


# Title/artist search results are relevance-ranked. In the first full library sync,
# no plausible candidate (score >= 0.7) ever ranked outside the top 40 of up to ~300
# results, while deep pages cost ~85% of matching time. ISRC lookups stay uncapped.
SEARCH_PAGES = 3


class TidalClient:
    def __init__(self, api: ReadClient, country: str) -> None:
        self.api, self.country = api, country

    def _next_page(self, current: str, following: str, include: str) -> str:
        parsed = urlsplit(urljoin(urljoin(self.api.base, current), following))
        origin = urlsplit(self.api.base)
        # Live TIDAL relationship links can be root-relative without the /v2 prefix.
        # Correct only known catalog routes on the same official origin. ReadClient
        # still rejects all other origins and paths before attaching credentials.
        if (parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc) and (
            parsed.path == "/tracks"
            or re.fullmatch(r"/searchResults/[^/]+/relationships/tracks", parsed.path)
        ):
            parsed = parsed._replace(path=origin.path.rstrip("/") + parsed.path)
        query = dict(parse_qsl(urlsplit(current).query))
        query.update(parse_qsl(parsed.query))
        query.setdefault("countryCode", self.country)
        query.setdefault("include", include)
        return urlunsplit(parsed._replace(query=urlencode(query)))

    def _track_pages(
        self, path: str, params: dict[str, str], max_pages: int = 20, truncate: bool = False
    ) -> list[Track]:
        result: list[Track] = []
        path = str(httpx.URL(urljoin(self.api.base, path)).copy_merge_params(params))
        seen: set[str] = set()
        for _ in range(max_pages):
            page = self.api.get(path)
            refs = page.get("data")
            if not isinstance(refs, list):
                raise SyncError("TIDAL catalog response is malformed.")
            result.extend(parse_tracks(page, refs))
            following = next_link(page)
            if not following:
                return result
            if following in seen:
                raise SyncError("TIDAL repeated a pagination link.")
            seen.add(following)
            include = (
                "tracks.artists,tracks.albums"
                if "/relationships/tracks" in path
                else "artists,albums"
            )
            path = self._next_page(path, following, include)
        if truncate:
            return result
        raise SyncError("TIDAL candidate search exceeded 20 pages; review manually.")

    def fallback_candidates(self, source: Track) -> list[Track]:
        """Second search with only the bare title and lead artist."""
        query = simple_query(source.title, source.artists)
        full = " ".join([source.title, source.version, *source.artists]).strip()[:256]
        if not query or query == full:
            return []
        return self._search(query)

    def candidates(self, source: Track) -> list[Track]:
        tracks: list[Track] = []
        if source.isrc:
            tracks.extend(
                self._track_pages(
                    "tracks",
                    {
                        "filter[isrc]": source.isrc,
                        "countryCode": self.country,
                        "include": "artists,albums",
                    },
                )
            )
        # Search even if an ISRC hit exists: conflicting and duplicate editions need evidence.
        query = " ".join([source.title, source.version, *source.artists]).strip()[:256]
        if not query:
            return tracks
        tracks.extend(self._search(query))
        return list({t.id: t for t in tracks}.values())

    def _search(self, query: str) -> list[Track]:
        tracks: list[Track] = []
        page = self.api.get(
            "searchResults",
            {
                "filter[query]": query,
                "countryCode": self.country,
                "include": "tracks.artists,tracks.albums",
            },
        )
        roots = page.get("data")
        if not isinstance(roots, list) or len(roots) != 1:
            raise SyncError("TIDAL search did not return its documented result resource.")
        relationship = roots[0].get("relationships", {}).get("tracks")
        if not isinstance(relationship, dict) or not isinstance(relationship.get("data"), list):
            raise SyncError("TIDAL omitted the requested search track relationship.")
        tracks.extend(parse_tracks(page, relationship["data"]))
        following = next_link(relationship)
        if following:
            current = f"searchResults/{quote(str(roots[0]['id']), safe='')}/relationships/tracks"
            tracks.extend(
                self._track_pages(
                    self._next_page(current, following, "tracks.artists,tracks.albums"),
                    {},
                    max_pages=SEARCH_PAGES - 1,
                    truncate=True,
                )
            )
        return list({t.id: t for t in tracks}.values())
