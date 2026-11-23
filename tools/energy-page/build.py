"""Build the Energy Ratings page from the latest energy export.

Run from the project root after `dj-sync energy export`:

    .venv/bin/python tools/energy-page/build.py

The output embeds private library data, so it is written under the git-ignored data/energy/.
"""

import json
from pathlib import Path

TEMPLATE = Path(__file__).parent / "template.html"
DATA = Path("data/energy/energy-data.json")
OUTPUT = Path("data/energy/energy-ratings.html")


def main() -> None:
    data = json.loads(DATA.read_text())
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    OUTPUT.write_text(TEMPLATE.read_text().replace("__ENERGY_DATA__", payload))
    tracks = sum(len(p["tracks"]) for p in data["playlists"])
    print(f"{len(data['playlists'])} playlists, {tracks} playlist entries -> {OUTPUT}")


if __name__ == "__main__":
    main()
