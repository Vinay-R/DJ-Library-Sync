import json

import pytest

from dj_sync.cli import main, parser
from dj_sync.config import Config, playlist_id
from dj_sync.errors import SyncError
from dj_sync.storage import process_lock


def test_environment_overrides_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "environment")
    (tmp_path / ".env").write_text("SPOTIFY_CLIENT_ID=file\n")
    path = tmp_path / "config.yaml"
    path.write_text("playlists:\n  aaaaaaaaaaaaaaaaaaaaaa: DJ\n")
    config = Config.load(path)
    assert config.playlists == {"a" * 22: "DJ"}
    import os

    assert os.environ["SPOTIFY_CLIENT_ID"] == "environment"


@pytest.mark.parametrize(
    "content",
    [
        "dry_run: false",
        "playlists: []",
        "country_code: usa",
        "playlists:\n  aaaaaaaaaaaaaaaaaaaaaa: Sets",
        "request_interval: -1",
        "tidal_auth: cookies",
    ],
)
def test_unsafe_or_invalid_configuration_rejected(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    with pytest.raises(SyncError):
        Config.load(path)


def test_no_apply_command():
    with pytest.raises(SystemExit):
        parser().parse_args(["sync", "--playlist", "a" * 22, "--apply"])


def test_limit_cannot_exceed_ten():
    with pytest.raises(SystemExit):
        parser().parse_args(["sync", "--playlist", "a" * 22, "--limit", "11"])


def test_doctor_no_secrets_or_network(tmp_path, monkeypatch, capsys):
    for name in ["SPOTIFY_CLIENT_ID", "TIDAL_CLIENT_ID", "TIDAL_CLIENT_SECRET"]:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("playlists: {}\n")
    assert main(["--config", str(path), "doctor"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["live_api_access"] == "not_checked"
    assert result["missing_credentials"] == [
        "SPOTIFY_CLIENT_ID",
        "TIDAL_CLIENT_ID",
        "TIDAL_CLIENT_SECRET",
    ]


def test_playlist_url_parsing():
    assert playlist_id("https://open.spotify.com/playlist/" + "a" * 22 + "?si=x") == "a" * 22
    assert playlist_id("spotify:playlist:" + "a" * 22) == "a" * 22
    with pytest.raises(SyncError):
        playlist_id("../../x")


def test_process_lock_prevents_concurrent_token_rotation(tmp_path):
    with process_lock(tmp_path):
        with pytest.raises(SyncError):
            with process_lock(tmp_path):
                pytest.fail("Lock must not be acquired twice")
