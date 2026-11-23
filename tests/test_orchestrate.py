"""The run orchestrator: stage ordering, soft vs hard outcomes, and the summary report."""

import json

import pytest

from dj_sync.errors import ApiError, SyncError
from dj_sync.orchestrate import STAGE_ORDER, run_stages, write_run_report


def test_stage_order_puts_music_index_before_downloads_and_apply_last():
    assert STAGE_ORDER.index("music_index") < STAGE_ORDER.index("download")
    assert STAGE_ORDER.index("local") < STAGE_ORDER.index("music_index")
    assert STAGE_ORDER[-1] == "music_apply"
    assert STAGE_ORDER[0] == "spotify_sync"


def test_soft_issues_continue_and_hard_errors_stop():
    calls = []

    def ok():
        calls.append("ok")
        return {"done": 1}

    def soft():
        calls.append("soft")
        return {"done": 1, "soft_issues": ["2 playlists deferred"]}

    def hard():
        calls.append("hard")
        raise ApiError("tidal", 403)

    def never():
        calls.append("never")
        return {}

    results = run_stages([("a", ok), ("b", soft), ("c", hard), ("d", never)])
    assert calls == ["ok", "soft", "hard"]
    assert [(r.name, r.status) for r in results] == [
        ("a", "ok"),
        ("b", "soft"),
        ("c", "hard"),
        ("d", "skipped"),
    ]
    assert "403" in (results[2].error or "")
    assert results[3].error == "not run: an earlier stage stopped the run"


def test_unexpected_exceptions_are_hard_but_never_leak_tracebacks_as_summary():
    def boom():
        raise ValueError("internal")

    [result] = run_stages([("x", boom)])
    assert result.status == "hard"
    assert result.error == "ValueError: internal"


def test_report_lists_every_stage_and_flags(tmp_path):
    def ok():
        return {"requested": 3}

    def soft():
        return {"soft_issues": ["1 track failed"]}

    def hard():
        raise SyncError("GUI is running")

    results = run_stages([("spotify_sync", ok), ("download", soft), ("music_apply", hard)])
    path = write_run_report(tmp_path, results, {"apply_download": True, "apply_music": False})
    text = path.read_text()
    assert "| spotify_sync | ok |" in text and "| download | soft |" in text
    assert "GUI is running" in text and "1 track failed" in text
    saved = json.loads(path.with_suffix(".json").read_text())
    assert saved["flags"] == {"apply_download": True, "apply_music": False}
    assert saved["exit_code"] == 2


@pytest.mark.parametrize(
    "statuses, code",
    [(["ok", "ok"], 0), (["ok", "soft"], 2), (["soft", "hard", "skipped"], 2)],
)
def test_exit_code(statuses, code, tmp_path):
    from dj_sync.orchestrate import StageResult, exit_code

    results = [StageResult(f"s{i}", s, {}, None, 0.0) for i, s in enumerate(statuses)]
    assert exit_code(results) == code
