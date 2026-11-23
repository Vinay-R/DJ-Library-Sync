from dataclasses import replace

import httpx
import pytest

from dj_sync.errors import SyncError
from dj_sync.http import ReadClient
from dj_sync.spotify_client import SpotifyClient
from dj_sync.tidal_client import TidalClient, duration_ms

PLAYLIST = "a" * 22


def api(service, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    base = (
        "https://api.spotify.com/v1/" if service == "spotify" else "https://openapi.tidal.com/v2/"
    )
    return ReadClient(service, base, lambda force: "test-token", client, 0, lambda _: None)


def spotify_track(n):
    return {
        "type": "track",
        "id": f"s{n}",
        "name": f"Song {n}",
        "artists": [{"name": "Artist"}],
        "duration_ms": 200000,
        "external_ids": {"isrc": f"ISRC{n}"},
        "is_playable": True,
    }


def test_spotify_exact_ten_with_pagination_and_no_removed_inference():
    offsets = []

    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/items"):
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            offsets.append((offset, limit))
            count = min(4, limit)
            return httpx.Response(
                200,
                json={
                    "items": [{"item": spotify_track(i)} for i in range(offset, offset + count)],
                    "total": 30,
                    "next": "https://api.spotify.com/v1/ignored",
                },
            )
        return httpx.Response(200, json={"name": "Playlist", "snapshot_id": "v1"})

    result = SpotifyClient(api("spotify", handler)).sample(PLAYLIST)
    assert len(result.items) == 10
    assert not result.complete
    assert offsets == [(0, 10), (4, 6), (8, 2)]


def test_spotify_null_local_episode_and_legacy_key():
    rows = [
        {"item": None},
        {"item": {**spotify_track(1), "is_local": True}},
        {"item": {**spotify_track(2), "type": "episode"}},
        {"track": spotify_track(3)},
    ]

    def handler(request):
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"items": rows, "total": 4, "next": None})
        return httpx.Response(200, json={"name": "P", "snapshot_id": "1"})

    result = SpotifyClient(api("spotify", handler)).sample(PLAYLIST)
    assert result.complete
    assert all(i.track is None for i in result.items[:3])
    assert result.items[3].track.id == "s3"


def test_changed_snapshot_rejected():
    snapshots = iter(["1", "2"])

    def handler(request):
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"items": [], "total": 0, "next": None})
        return httpx.Response(200, json={"name": "P", "snapshot_id": next(snapshots)})

    with pytest.raises(SyncError, match="changed"):
        SpotifyClient(api("spotify", handler)).sample(PLAYLIST)


def test_playlist_discovery_paginates():
    def handler(request):
        assert request.method == "GET"
        if "offset" in request.url.params:
            return httpx.Response(200, json={"items": [{"id": "b"}], "next": None})
        return httpx.Response(
            200,
            json={
                "items": [{"id": "a"}],
                "next": "https://api.spotify.com/v1/me/playlists?offset=1",
            },
        )

    assert [p["id"] for p in SpotifyClient(api("spotify", handler)).playlists()] == ["a", "b"]


def tidal_document():
    track = {
        "type": "tracks",
        "id": "42",
        "attributes": {
            "title": "Night Drive",
            "version": "Extended Mix",
            "duration": "PT7M",
            "isrc": "USAAA2600001",
            "availability": ["STREAM"],
            "explicit": False,
        },
        "relationships": {"artists": {"data": [{"type": "artists", "id": "artist1"}]}},
    }
    artist = {"type": "artists", "id": "artist1", "attributes": {"name": "Artist"}}
    return track, artist


def test_tidal_official_isrc_and_query_search_jsonapi(source):
    requests = []
    track, artist = tidal_document()

    def handler(request):
        assert request.method == "GET"
        requests.append(request)
        assert request.url.params["countryCode"] == "US"
        if request.url.path == "/v2/tracks":
            assert request.url.params["filter[isrc]"] == source.isrc
            return httpx.Response(200, json={"data": [track], "included": [artist]})
        assert request.url.path == "/v2/searchResults"
        assert request.url.params["include"] == "tracks.artists,tracks.albums"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "searchResults",
                        "id": "opaque",
                        "relationships": {"tracks": {"data": [{"type": "tracks", "id": "42"}]}},
                    }
                ],
                "included": [track, artist],
            },
        )

    result = TidalClient(api("tidal", handler), "US").candidates(source)
    assert len(result) == 1
    assert result[0].artists == ("Artist",)
    assert result[0].duration_ms == 420000
    assert len(requests) == 2


def test_tidal_isrc_pages_are_not_discarded(source):
    track, artist = tidal_document()

    def handler(request):
        if request.url.path.endswith("searchResults"):
            return httpx.Response(
                200, json={"data": [{"id": "opaque", "relationships": {"tracks": {"data": []}}}]}
            )
        if "page[cursor]" in request.url.params:
            assert request.url.params["filter[isrc]"] == source.isrc
            return httpx.Response(200, json={"data": [{**track, "id": "43"}], "included": [artist]})
        return httpx.Response(
            200,
            json={
                "data": [track],
                "included": [artist],
                "links": {
                    "next": {"href": "https://openapi.tidal.com/v2/tracks?page%5Bcursor%5D=abc"}
                },
            },
        )

    assert len(TidalClient(api("tidal", handler), "US").candidates(source)) == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT1H2M3.5S", 3723500),
        ("PT7M", 420000),
        ("PT3S", 3000),
        (None, None),
        ("PT", None),
        (180, None),
        ("invalid", None),
    ],
)
def test_iso_duration(value, expected):
    assert duration_ms(value) == expected


@pytest.mark.parametrize(
    "next_url",
    [
        "?page%5Bcursor%5D=next",
        "/searchResults/opaque-id/relationships/tracks?page%5Bcursor%5D=next",
        "https://openapi.tidal.com/searchResults/opaque-id/relationships/tracks?page%5Bcursor%5D=next",
    ],
)
def test_tidal_search_cursor_preserves_query_and_includes(source, next_url):
    source = replace(source, isrc=None)
    track, artist = tidal_document()

    def handler(request):
        if request.url.path == "/v2/searchResults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "opaque-id",
                            "relationships": {"tracks": {"data": [], "links": {"next": next_url}}},
                        }
                    ]
                },
            )
        assert request.url.path == "/v2/searchResults/opaque-id/relationships/tracks"
        assert request.url.params["page[cursor]"] == "next"
        assert request.url.params["include"] == "tracks.artists,tracks.albums"
        assert request.url.params["countryCode"] == "US"
        return httpx.Response(
            200, json={"data": [{"type": "tracks", "id": "42"}], "included": [track, artist]}
        )

    assert TidalClient(api("tidal", handler), "US").candidates(source)[0].id == "42"


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/searchResults/opaque-id/relationships/tracks?page%5Bcursor%5D=next",
        "https://openapi.tidal.com/unrecognized-route?page%5Bcursor%5D=next",
    ],
)
def test_tidal_pagination_correction_does_not_relax_origin_guard(source, next_url):
    source = replace(source, isrc=None)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/v2/searchResults"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "opaque-id",
                        "relationships": {"tracks": {"data": [], "links": {"next": next_url}}},
                    }
                ]
            },
        )

    with pytest.raises(SyncError, match="Blocked"):
        TidalClient(api("tidal", handler), "US").candidates(source)
    assert len(calls) == 1


SEARCH_NEXT = "/searchResults/q/relationships/tracks?page[cursor]="


def endless_search_handler(calls, isrc_pages=0):
    track, artist = tidal_document()

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v2/tracks":
            cursor = int(request.url.params.get("page[cursor]", "0"))
            body = {"data": [{"type": "tracks", "id": "42"}], "included": [track, artist]}
            if cursor + 1 < isrc_pages:
                body["links"] = {"next": f"/v2/tracks?page[cursor]={cursor + 1}"}
            return httpx.Response(200, json=body)
        if request.url.path == "/v2/searchResults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "q",
                            "relationships": {
                                "tracks": {
                                    "data": [],
                                    "links": {"next": SEARCH_NEXT + "1"},
                                }
                            },
                        }
                    ]
                },
            )
        cursor = int(request.url.params["page[cursor]"])
        return httpx.Response(
            200,
            json={
                "data": [],
                "links": {"next": SEARCH_NEXT + str(cursor + 1)},
            },
        )

    return handler


def test_title_search_stops_after_three_pages_without_error(source):
    calls = []
    TidalClient(api("tidal", endless_search_handler(calls)), "US").candidates(
        replace(source, isrc=None)
    )
    assert calls.count("/v2/searchResults") == 1
    assert calls.count("/v2/searchResults/q/relationships/tracks") == 2


def test_isrc_lookup_pagination_is_not_capped(source):
    calls = []
    TidalClient(api("tidal", endless_search_handler(calls, isrc_pages=6)), "US").candidates(source)
    assert calls.count("/v2/tracks") == 6
