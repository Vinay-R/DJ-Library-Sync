"""Build a scrubbed copy of this repository for public sharing.

Copies every tracked file into DEST, applies the replacements in a private scrub file
(kept under the git-ignored data/ folder, because it lists what it hides), then scans the
copy for anything that should not be there. Exits non-zero, writing nothing further, when a
forbidden string survives.

    .venv/bin/python tools/make_public.py --dest ../DJ-Library-Sync-public \\
        --scrub data/public-scrub.json [--ledger data/ledger.sqlite3]

Scrub file shape:
    {"replace": [["exact text", "replacement"], ...],   # applied longest first, whole words
     "regex": [["pattern", "replacement"], ...],        # applied after the plain ones
     "allow": ["common word that happens to be a track title", ...]}

With --ledger, every playlist name, track title and artist in the ledger (5+ characters,
not in "allow") is also treated as forbidden, which catches names the scrub file missed.
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [f for f in out.decode().split("\0") if f]


def word(text: str) -> re.Pattern[str]:
    """Whole-word match; a space in ``text`` also matches a line break (wrapped Markdown)."""
    body = r"\s+".join(re.escape(part) for part in text.split(" "))
    return re.compile(r"(?<![\w])" + body + r"(?![\w])")


def ledger_names(path: Path) -> set[str]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    names: set[str] = set()
    for (n,) in db.execute("SELECT name FROM spotify_playlists"):
        names.add(n or "")
    for (n,) in db.execute("SELECT name FROM music_playlists"):
        names.add(n or "")
    for title, artists in db.execute("SELECT title, artists_json FROM tracks"):
        names.add(title or "")
        names.update(json.loads(artists or "[]"))
    return {n for n in names if len(n) >= 5}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, required=True)
    ap.add_argument("--scrub", type=Path, required=True)
    ap.add_argument("--ledger", type=Path)
    args = ap.parse_args()
    scrub = json.loads(args.scrub.read_text())
    plain = sorted(scrub.get("replace", []), key=lambda p: -len(p[0]))
    regexes = [(re.compile(p), r) for p, r in scrub.get("regex", [])]
    allow = set(scrub.get("allow", []))

    dest = args.dest.resolve()
    if dest == Path.cwd().resolve() or Path.cwd().resolve().is_relative_to(dest):
        print("Refusing to write over this repository.", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    for child in dest.iterdir():  # keep the public repo's own .git
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()

    texts: dict[str, str] = {}
    for rel in tracked_files():
        public_rel = rel
        for old_name, new_name in plain:
            public_rel = word(old_name).sub(lambda _m, n=new_name: n, public_rel)
        for pat, new_name in regexes:
            public_rel = pat.sub(new_name, public_rel)
        src, out = Path(rel), dest / public_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(src, out)  # binary (test fixtures)
            continue
        for old, new in plain:
            text = word(old).sub(lambda _m, n=new: n, text)
        for pat, new in regexes:
            text = pat.sub(new, text)
        out.write_text(text, encoding="utf-8")
        shutil.copymode(src, out)
        texts[public_rel] = text

    forbidden = {old for old, _ in plain} | set(scrub.get("forbid", []))
    if args.ledger:
        forbidden |= ledger_names(args.ledger)
    forbidden -= allow
    replacements = {new for _, new in plain}
    leftovers: dict[str, list[str]] = {}
    for rel, text in texts.items():
        for name in forbidden:
            if name in replacements:
                continue
            if word(name).search(text):
                leftovers.setdefault(name, []).append(rel)
    if leftovers:
        print(f"{len(leftovers)} forbidden string(s) survived:", file=sys.stderr)
        for name, files in sorted(leftovers.items()):
            print(f"  {name!r}: {', '.join(sorted(files))[:160]}", file=sys.stderr)
        return 1
    print(f"Wrote {len(texts)} text files (+ binaries) to {dest}; no forbidden strings left.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
