"""Build the TIDAL Match Review page from the latest review export.

Run from the project root after `dj-sync review export`:

    .venv/bin/python tools/review-page/build.py data/reports/sync-<id>.json

The output embeds private track data, so it is written under the git-ignored data/review/.
"""

import json
import sys
from pathlib import Path

TEMPLATE = Path(__file__).parent / "template.html"
OUTPUT = Path("data/review/tidal-match-review.html")


def main(summary_path: str) -> None:
    data = json.loads(Path("data/review/review-data.json").read_text())
    summary = json.loads(Path(summary_path).read_text())
    matched: set[str] = set()
    for entry in summary["playlists"]:
        if not entry.get("report"):
            continue
        report = json.loads(Path(entry["report"]).with_suffix(".json").read_text())
        matched |= {
            r["source"]["id"]
            for r in report["rows"]
            if r["source"] and r["match"]["status"] == "matched"
        }
    data["matched_total"] = len(matched)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    OUTPUT.write_text(TEMPLATE.read_text().replace("__REVIEW_DATA__", payload))
    print(
        f"needs review {len(data['items'])}, auto-resolved {len(data['auto_resolved'])}, "
        f"matched {len(matched)} -> {OUTPUT}"
    )


if __name__ == "__main__":
    main(sys.argv[1])
