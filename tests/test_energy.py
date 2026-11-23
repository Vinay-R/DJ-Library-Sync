"""Energy star ratings: measures, per-playlist guesses, the user's stars always win."""

import json

import pytest

from dj_sync import energy
from dj_sync.db import Ledger
from dj_sync.errors import SyncError
from dj_sync.music_app import RATINGS_SCRIPT, SET_RATINGS_SCRIPT, MusicApp


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.sqlite3")
    yield db
    db.close()


class FakeRatings:
    def __init__(self, ratings=None):
        self.ratings = ratings or {}  # id -> {"stars", "kind"}
        self.writes = []
        self.ignore = set()

    def __call__(self, script, args):
        if script is RATINGS_SCRIPT:
            want = json.loads(args[0])
            return json.dumps({t: self.ratings.get(t, {"stars": 0, "kind": "user"}) for t in want})
        assert script is SET_RATINGS_SCRIPT
        stars = json.loads(args[0])
        self.writes.append(stars)
        for t, s in stars.items():
            if t not in self.ignore:
                self.ratings[t] = {"stars": s, "kind": "user"}
        return "ok"


def feats(busy):
    return {"loud": -7, "low": -8, "high": -20, "sustain": busy / 10, "dynamics": 8, "busy": busy}


def seed(ledger, playlists):
    """playlists: {name: (group, [(music_id, busy), ...])}"""
    c = ledger.connection
    for i, (name, (group, tracks)) in enumerate(playlists.items()):
        pid = f"P{i}"
        c.execute(
            "INSERT INTO spotify_playlists(id, name, group_name, music_persistent_id) "
            "VALUES(?,?,?,?)",
            (f"sp{i}", name, group, pid),
        )
        for pos, (music_id, busy) in enumerate(tracks):
            c.execute("INSERT INTO music_memberships VALUES(?,?,?)", (pid, music_id, pos))
            c.execute(
                "INSERT OR IGNORE INTO music_tracks VALUES(?,?,?,?,?,1)",
                (music_id, f"Song {music_id}", "Artist", 200000, f"/lib/{music_id}.aiff"),
            )
            c.execute(
                "INSERT OR IGNORE INTO energy_features VALUES(?,?,?,1)",
                (music_id, f"/lib/{music_id}.aiff", json.dumps(feats(busy))),
            )
    c.commit()


def test_measures_reward_time_at_full_power_and_busy_hats():
    calm = {
        "full": [-20.0] * 50 + [-10.0] * 10,
        "low": [-12.0] * 60,
        "high": [-30.0] * 60,
        "fine": [-40.0] * 300,
    }
    hot = {
        "full": [-8.0] * 60,
        "low": [-9.0] * 60,
        "high": [-18.0] * 60,
        "fine": [-40.0, -30.0] * 150,
    }
    a, b = energy.measures(calm), energy.measures(hot)
    assert b["sustain"] > a["sustain"] and b["busy"] > a["busy"] and b["dynamics"] < a["dynamics"]
    with pytest.raises(SyncError):
        energy.measures({"full": [-90.0] * 5, "low": [], "high": [], "fine": []})


def test_guesses_split_a_playlist_into_five_equal_groups():
    scores = {f"t{i}": float(i) for i in range(10)}
    g = energy.guesses(scores)
    assert [g[f"t{i}"] for i in range(10)] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    assert energy.guesses({"only": 0.0}) == {"only": 3}


def test_guess_is_relative_to_the_home_playlist(ledger):
    # T3 is the calmest track in "Hard" but the busiest in "Chill": its home decides.
    seed(
        ledger,
        {
            "Hard": ("DJ", [("T1", 9), ("T2", 8), ("T3", 4), ("T4", 7), ("T5", 6)]),
            "Chill": ("Vibes", [("T3", 4), ("T6", 1), ("T7", 2), ("T8", 1.5), ("T9", 3)]),
        },
    )
    p = energy.plan(ledger, {})
    assert p["per_playlist"]["P0"]["T3"] == 1 and p["per_playlist"]["P1"]["T3"] == 5
    assert p["tracks"]["T3"]["home"] == "P0"  # DJ before Vibes
    assert p["tracks"]["T3"]["final"] == 1 and p["tracks"]["T3"]["source"] == "guess"
    energy.import_choices(ledger, {"ratings": [{"music_id": "T3", "stars": 4, "home": "P1"}]})
    p = energy.plan(ledger, {})
    assert p["tracks"]["T3"]["home"] == "P1" and p["tracks"]["T3"]["final"] == 4


def test_the_users_own_stars_are_kept_and_computed_ones_are_not(ledger):
    seed(ledger, {"Hard": ("DJ", [("T1", 9), ("T2", 1), ("T3", 5)])})
    live = {"T1": {"stars": 2, "kind": "user"}, "T2": {"stars": 5, "kind": "computed"}}
    p = energy.plan(ledger, live)
    assert p["tracks"]["T1"]["final"] == 2 and p["tracks"]["T1"]["source"] == "yours"
    assert p["tracks"]["T2"]["source"] == "guess" and p["tracks"]["T2"]["final"] == 1


def test_apply_writes_guesses_reads_back_and_never_replaces_the_users_stars(ledger):
    seed(ledger, {"Hard": ("DJ", [("T1", 9), ("T2", 1), ("T3", 5)])})
    music = FakeRatings({"T1": {"stars": 3, "kind": "user"}})
    dry = energy.apply_ratings(ledger, MusicApp(music))
    assert dry["to_change"] == 2 and music.writes == []
    done = energy.apply_ratings(ledger, MusicApp(music), apply=True)
    assert done["written"] == 2 and music.writes == [{"T2": 1, "T3": 3}]
    assert music.ratings["T1"]["stars"] == 3  # the user's own
    again = energy.apply_ratings(ledger, MusicApp(music), apply=True)
    assert again["to_change"] == 0
    # The user later changes a guessed track in Music.app: it becomes theirs.
    music.ratings["T2"] = {"stars": 4, "kind": "user"}
    p = energy.plan(ledger, music.ratings)
    assert p["tracks"]["T2"]["source"] == "yours" and p["tracks"]["T2"]["final"] == 4
    # A page choice overrides everything, including the user's earlier stars.
    energy.import_choices(ledger, {"ratings": [{"music_id": "T1", "stars": 5, "home": None}]})
    energy.apply_ratings(ledger, MusicApp(music), apply=True)
    assert music.ratings["T1"]["stars"] == 5


def test_apply_stops_when_music_does_not_keep_the_stars(ledger):
    seed(ledger, {"Hard": ("DJ", [("T1", 9), ("T2", 1)])})
    music = FakeRatings()
    music.ignore = {"T1"}
    with pytest.raises(SyncError, match="did not keep"):
        energy.apply_ratings(ledger, MusicApp(music), apply=True)
    assert ledger.connection.execute("SELECT count(*) FROM energy_writes").fetchone()[0] == 0


def test_import_validates_and_clears(ledger):
    with pytest.raises(SyncError, match="1-5"):
        energy.import_choices(ledger, {"ratings": [{"music_id": "T1", "stars": 7}]})
    energy.import_choices(ledger, {"ratings": [{"music_id": "T1", "stars": 3}]})
    out = energy.import_choices(ledger, {"ratings": [{"music_id": "T1", "stars": 0}]})
    assert out == {"stored": 0, "cleared": 1}
    assert ledger.connection.execute("SELECT count(*) FROM energy_choices").fetchone()[0] == 0


def test_page_data_sorts_each_playlist_by_energy(ledger):
    seed(ledger, {"Hard": ("DJ", [("T1", 1), ("T2", 9), ("T3", 5)])})
    data = energy.page_data(ledger, {})
    rows = data["playlists"][0]["tracks"]
    assert [r["id"] for r in rows] == ["T2", "T3", "T1"]
    assert [r["guess"] for r in rows] == [5, 3, 1]
