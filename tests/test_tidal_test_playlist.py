import json
import time

import httpx
import pytest

from dj_sync.auth import TIDAL_WRITE_SCOPES
from dj_sync.cli import main
from dj_sync.db import Ledger
from dj_sync.errors import ApiError, SyncError
from dj_sync.tidal_test_playlist import TestPlaylistAPI, apply_test_plan, load_plan


class Token:
    service = "tidal"
    mode = "user_write"
    token = {"scope": TIDAL_WRITE_SCOPES}

    def __call__(self, force=False):
        return "fake-access-token"


class Remote:
    def __init__(self):
        self.playlists = {
            "existing": {
                "name": "Historic playlist",
                "description": "untouched",
                "accessType": "UNLISTED",
            }
        }
        self.items = {"existing": ["old-track"]}
        self.calls = []
        self.lose_create = False
        self.lose_add = False
        self.bad_readback = False
        self.forbid_create = False
        self.owner = "me-id"
        self.posts_by_key = {}

    def resource(self, id):
        return {
            "type": "playlists",
            "id": id,
            "attributes": {**self.playlists[id], "numberOfItems": len(self.items[id])},
            "relationships": {"owners": {"data": [{"type": "users", "id": self.owner}]}},
        }

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        assert request.url.host == "openapi.tidal.com"
        assert request.method in {"GET", "POST"}
        if request.method == "GET":
            if path == "/v2/users/me":
                return httpx.Response(200, json={"data": {"type": "users", "id": "me-id"}})
            if path == "/v2/playlists":
                assert request.url.params["filter[owners.id]"] == "me"
                return httpx.Response(
                    200, json={"data": [self.resource(k) for k in self.playlists]}
                )
            id = path.split("/")[3]
            if path.endswith("/relationships/items"):
                items = self.items[id]
                if self.bad_readback and items:
                    items = items[:-1]
                return httpx.Response(
                    200, json={"data": [{"type": "tracks", "id": t} for t in items]}
                )
            return httpx.Response(200, json={"data": self.resource(id)})
        assert "existing" not in path, "Must never mutate a preexisting playlist"
        assert request.headers["Content-Type"] == "application/vnd.api+json"
        assert request.headers["Idempotency-Key"]
        key = request.headers["Idempotency-Key"]
        payload = json.loads(request.content)
        if key in self.posts_by_key:
            return self.posts_by_key[key]
        if path == "/v2/playlists":
            if self.forbid_create:
                return httpx.Response(403)
            assert payload["data"]["attributes"]["accessType"] == "UNLISTED"
            id = "new-" + str(len(self.playlists))
            self.playlists[id] = payload["data"]["attributes"]
            self.items[id] = []
            response = httpx.Response(201, json={"data": self.resource(id)})
            self.posts_by_key[key] = response
            if self.lose_create:
                self.lose_create = False
                raise httpx.ReadTimeout("response lost")
            return response
        assert path.endswith("/relationships/items")
        assert payload["meta"]["onDuplicates"] == "SKIP"
        id = path.split("/")[3]
        ids = [r["id"] for r in payload["data"]]
        assert len(ids) == len(set(ids))
        self.items[id].extend(t for t in ids if t not in self.items[id])
        response = httpx.Response(200, json={"data": []})
        self.posts_by_key[key] = response
        if self.lose_add:
            self.lose_add = False
            raise httpx.ReadTimeout("response lost")
        return response

    def api(self, apply=True, token=None):
        return TestPlaylistAPI(
            token or Token(),
            httpx.Client(transport=httpx.MockTransport(self.handle)),
            0,
            "US",
            apply=apply,
        )


@pytest.fixture
def plan():
    return {
        "source_id": "s" * 22,
        "snapshot_id": "snapshot",
        "country": "US",
        "track_ids": ["1", "2", "3"],
        "hash": "test-plan-hash",
        "report_run_id": "run-id",
        "name": "DJ Library Sync TEST — Example",
        "access_type": "UNLISTED",
        "skipped": [],
    }


def intent_file(tmp_path):
    return next((tmp_path / "test-playlists").glob("*.json"))


def test_apply_twice_creates_only_one_test_and_no_duplicate_entries(tmp_path, plan):
    remote = Remote()
    first = apply_test_plan(remote.api(), plan, tmp_path)
    second = apply_test_plan(remote.api(), plan, tmp_path)
    assert first["post_requests"] == 2
    assert first["added_count"] == 3
    assert second["post_requests"] == second["added_count"] == 0
    assert second["after_count"] == 3
    assert len(remote.playlists) == 2
    assert remote.items["existing"] == ["old-track"]
    state = json.loads(intent_file(tmp_path).read_text())
    assert state["status"] == "verified"
    assert state["events"][0]["status"] == "verified_by_readback"


@pytest.mark.parametrize("lost", ["lose_create", "lose_add"])
def test_committed_write_with_lost_response_recovers_without_duplicates(tmp_path, plan, lost):
    remote = Remote()
    setattr(remote, lost, True)
    with pytest.raises(SyncError, match="uncertain"):
        apply_test_plan(remote.api(), plan, tmp_path)
    result = apply_test_plan(remote.api(), plan, tmp_path)
    assert result["after_count"] == 3
    assert len(remote.playlists) == 2
    assert remote.items["new-1"] == plan["track_ids"]
    assert sum(r.method == "POST" and r.url.path == "/v2/playlists" for r in remote.calls) == 1


def test_readback_must_confirm_membership(tmp_path, plan):
    remote = Remote()
    remote.bad_readback = True
    with pytest.raises(SyncError, match="readback"):
        apply_test_plan(remote.api(), plan, tmp_path)
    assert json.loads(intent_file(tmp_path).read_text())["status"] == "add_pending"
    remote.bad_readback = False
    assert apply_test_plan(remote.api(), plan, tmp_path)["post_requests"] == 0


@pytest.mark.parametrize(
    "tamper", ["owner", "description", "name", "accessType", "extra", "duplicate"]
)
def test_existing_target_changes_fail_closed(tmp_path, plan, tamper):
    remote = Remote()
    apply_test_plan(remote.api(), plan, tmp_path)
    if tamper == "owner":
        remote.owner = "other-user"
    elif tamper == "extra":
        remote.items["new-1"].append("unrelated")
    elif tamper == "duplicate":
        remote.items["new-1"].append("1")
    else:
        remote.playlists["new-1"][tamper] = "changed"
    remote.calls = []
    with pytest.raises(SyncError):
        apply_test_plan(remote.api(), plan, tmp_path)
    assert all(r.method == "GET" for r in remote.calls)


def test_expired_uncertain_creation_never_recreates(tmp_path, plan):
    remote = Remote()
    remote.forbid_create = True
    with pytest.raises(ApiError):
        apply_test_plan(remote.api(), plan, tmp_path)
    path = intent_file(tmp_path)
    state = json.loads(path.read_text())
    state["create_started"] = time.time() - 3601
    path.write_text(json.dumps(state))
    remote.forbid_create = False
    remote.calls = []
    with pytest.raises(SyncError, match="replay window"):
        apply_test_plan(remote.api(), plan, tmp_path)
    assert all(r.method == "GET" for r in remote.calls)


def test_changed_plan_or_account_cannot_expand_test_scope(tmp_path, plan):
    remote = Remote()
    apply_test_plan(remote.api(), plan, tmp_path)
    with pytest.raises(SyncError, match="different plan"):
        apply_test_plan(remote.api(), {**plan, "hash": "other"}, tmp_path)


def test_write_transport_requires_apply_and_cannot_add_to_arbitrary_playlist(plan):
    remote = Remote()
    with pytest.raises(SyncError, match="explicit apply"):
        remote.api(apply=False).create({"name": "x", "description": "x", "create_key": "key"})
    with pytest.raises(SyncError, match="verified test"):
        remote.api().add("existing", ["1"], "key")
    assert not remote.calls


def test_writer_rejects_catalog_or_read_only_tokens():
    token = Token()
    token.mode = "client_credentials"
    with pytest.raises(SyncError, match="user-write"):
        Remote().api(token=token)
    token.mode = "user_write"
    token.token = {"scope": "user.read playlists.read"}
    with pytest.raises(SyncError, match="write scopes"):
        Remote().api(token=token).user_id()


def test_cli_dry_run_needs_no_write_token_and_creates_no_remote_intent(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "config.yaml"
    config.write_text("{}")
    ledger = Ledger(tmp_path / "data" / "ledger.sqlite3")
    run_id = ledger.begin("s" * 22)
    report = {
        "country": "US",
        "playlist": {"id": "s" * 22, "snapshot_id": "v1", "name": "Example"},
        "rows": [
            {
                "source": {"title": "Track"},
                "match": {
                    "status": "matched",
                    "selected_id": "1",
                    "candidates": [{"track": {"id": "1"}, "eligible": True, "score": 1.0}],
                },
            }
        ],
    }
    ledger.finish(run_id, "completed", report)

    def forbidden(*args, **kwargs):
        pytest.fail("Dry run must not perform a network request")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    assert main(["--config", str(config), "test-playlist", "--run-id", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["writes_performed"] == 0
    assert not (tmp_path / "data" / "test-playlists").exists()
    ledger.connection.execute("UPDATE runs SET started=0")
    ledger.connection.commit()
    with pytest.raises(SyncError, match="older"):
        load_plan(ledger, run_id, "US")
    ledger.close()


def test_membership_pagination_stays_on_same_origin_and_resource():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/v2/playlists/test-id/relationships/items"
        if "page[cursor]" in request.url.params:
            return httpx.Response(200, json={"data": [{"type": "tracks", "id": "2"}]})
        return httpx.Response(
            200,
            json={
                "data": [{"type": "tracks", "id": "1"}],
                "links": {"next": "/playlists/test-id/relationships/items?page%5Bcursor%5D=next"},
            },
        )

    api = TestPlaylistAPI(Token(), httpx.Client(transport=httpx.MockTransport(handler)), 0, "US")
    assert api.members("test-id") == ["1", "2"]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/v2/playlists/test-id/relationships/items?page%5Bcursor%5D=next",
        "/playlists/other-id/relationships/items?page%5Bcursor%5D=next",
    ],
)
def test_membership_pagination_rejects_external_or_other_playlist(link):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [], "links": {"next": link}})

    api = TestPlaylistAPI(Token(), httpx.Client(transport=httpx.MockTransport(handler)), 0, "US")
    with pytest.raises(SyncError):
        api.members("test-id")
    assert len(calls) == 1


def test_verified_test_membership_persists_idempotently(tmp_path, plan):
    remote = Remote()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    run_id = ledger.begin(plan["source_id"])
    plan["report_run_id"] = run_id
    first = apply_test_plan(remote.api(), plan, tmp_path)
    ledger.record_test_verification(first)
    second = apply_test_plan(remote.api(), plan, tmp_path)
    ledger.record_test_verification(second)
    assert (
        ledger.connection.execute("SELECT count(*) FROM tidal_test_memberships").fetchone()[0] == 3
    )
    assert ledger.connection.execute("SELECT count(*) FROM tidal_test_runs").fetchone()[0] == 2
    assert ledger.connection.execute("SELECT count(*) FROM local_assets").fetchone()[0] == 0
    ledger.close()
