from collections import Counter
from pathlib import Path
from typing import Any

from .models import SETTLED_STATUSES
from .storage import atomic_text, save_json


def escape(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    for char in ("\\", "|", "[", "]", "<", ">", "*", "_", "`"):
        text = text.replace(char, "\\" + char)
    return text


def write_report(directory: Path, report: dict[str, Any]) -> Path:
    stem = directory / report["run_id"]
    save_json(stem.with_suffix(".json"), report)
    lines = [
        "# Spotify → TIDAL read-only matching report",
        "",
        f"Playlist: {escape(report['playlist']['name'])}",
        "",
        f"Country: {report['country']} · Dry run · External writes: **0**",
        "",
        (
            "Scope: complete playlist snapshot"
            if report["playlist"]["complete"]
            else "Scope: first playlist items"
        )
        + ", including unavailable/local/non-track entries. "
        "These are real API observations only when run with authenticated services.",
        "",
        f"Counts: {escape(report['counts'])}",
        "",
        "| Position | Spotify track | Status | Selected TIDAL ID |",
        "| --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        source = row.get("source")
        title = f"{', '.join(source['artists'])} — {source['title']}" if source else row["issue"]
        lines.append(
            f"| {row['position'] + 1} | {escape(title)} | "
            f"{row['match']['status']} | {row['match'].get('selected_id') or '—'} |"
        )
    lines.extend(
        [
            "",
            "## Source changes",
            "",
            f"Coverage: {escape(report['diff']['scope'])}.",
            "",
            f"Newly observed IDs: {escape(report['diff']['added_ids'])}",
            "",
            f"Removed IDs: {escape(report['diff']['removed_ids'])}. "
            f"Removal comparison valid: {report['diff']['removals_evaluated']}.",
            "",
            f"Previous name: {escape(report['diff']['renamed_from'])}. "
            f"Order changed: {report['diff']['order_changed']}.",
            "",
            f"Group assignment needed: {report['mapping_review_required']}.",
            "",
            "## Candidate evidence",
            "",
        ]
    )
    full = report["diff"]["scope"] == "complete"
    if full:
        lines.extend(
            [
                "Complete snapshots list evidence only for items needing review. "
                "The companion JSON report retains evidence for every item.",
                "",
            ]
        )
    for row in report["rows"]:
        if full and row["match"]["status"] in SETTLED_STATUSES:
            continue
        lines.extend([f"### Item {row['position'] + 1}", "", escape(row["match"]["reason"]), ""])
        candidates = row["match"].get("candidates", [])
        for candidate in candidates[:5]:
            track = candidate["track"]
            lines.append(
                f"- [{escape(track['title'])}]({candidate['url']}): "
                f"score {candidate['score']:.3f}; " + escape("; ".join(candidate["reasons"]))
            )
        if len(candidates) > 5:
            lines.append(
                f"\nShowing the top 5 of {len(candidates)} candidates. "
                "The companion JSON report retains all candidate evidence."
            )
        lines.append("")
    lines.extend(
        [
            "## Proposed TIDAL changes",
            "",
            escape(report["plan"]["note"]),
            "",
            f"Unique proposed IDs: {escape(report['plan']['proposed_track_ids'])}",
            "",
            "No Music.app inspection or mutation; no audio files read or changed.",
            "",
        ]
    )
    atomic_text(stem.with_suffix(".md"), "\n".join(lines))
    return stem.with_suffix(".md")


def write_summary(directory: Path, summary: dict[str, Any]) -> Path:
    stem = directory / f"sync-{summary['id']}"
    save_json(stem.with_suffix(".json"), summary)
    totals = summary["totals"]
    lines = [
        "# Mapped playlist sync summary (read-only)",
        "",
        f"Country: {summary['country']} · Dry run · External writes: **0**",
        "",
        f"Playlists: {totals['playlists']} · Unique Spotify tracks: "
        f"{totals['unique_spotify_tracks']} · In more than one playlist: "
        f"{totals['tracks_in_multiple_playlists']} · Unique proposed TIDAL IDs: "
        f"{totals['proposed_tidal_ids']}",
        "",
        f"Row status counts: {escape(totals['row_counts'])}",
        "",
        "| Playlist | Group | Status | Items | Matched | Needs review | New | Removed | Report |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in summary["playlists"]:
        counts = entry.get("counts", {})
        review = sum(v for k, v in counts.items() if k not in SETTLED_STATUSES)
        name = entry.get("name") or entry["id"]
        if entry.get("renamed_from"):
            name = f"{name} (was {entry['renamed_from']})"
        lines.append(
            f"| {escape(name)} | {entry['group']} | {entry['status']} | "
            f"{entry.get('items', '—')} | {counts.get('matched', '—')} | "
            f"{review if counts else '—'} | {entry.get('added_since_last', '—')} | "
            f"{entry.get('removed_since_last', '—')} | "
            f"{escape(Path(entry['report']).name) if entry.get('report') else '—'} |"
        )
    problems = [e for e in summary["playlists"] if e.get("error")]
    if problems:
        lines.extend(["", "## Skipped playlists", ""])
        lines.extend(f"- {escape(e['id'])}: {escape(e['error'])}" for e in problems)
    deferred = [e for e in summary["playlists"] if e["status"] == "deferred"]
    if deferred:
        lines.extend(
            [
                "",
                f"{len(deferred)} playlist(s) deferred after a TIDAL catalog authorization "
                "or rate-limit stop. Rerun later; completed matches are reused.",
            ]
        )
    lines.extend(
        [
            "",
            "Removals since the previous complete snapshot are review-only. No TIDAL, "
            "Spotify or Music.app playlist was changed.",
            "",
        ]
    )
    atomic_text(stem.with_suffix(".md"), "\n".join(lines))
    return stem.with_suffix(".md")


def _comment_safe(value: Any) -> str:
    return " ".join(str(value).split())


def mapping_worksheet(review: list[dict[str, Any]]) -> str:
    """A commented YAML worksheet. It never guesses a group from a playlist name."""

    def line(entry: dict[str, Any], value: str) -> str:
        items = entry.get("items")
        size = f" ({items} items)" if isinstance(items, int) else ""
        return f"#  {entry['id']}: {value}   # {_comment_safe(entry['name'])}{size}"

    mapped = [e for e in review if e.get("group")]
    readable = [e for e in review if not e.get("group") and e.get("readable")]
    unreadable = [e for e in review if not e.get("group") and not e.get("readable")]
    lines = [
        "# DJ Library Sync - playlist mapping worksheet",
        "#",
        "# Spotify's API does not expose desktop folders, so each playlist is assigned by hand.",
        '# Set the value to DJ for playlists in your Spotify "DJ" folder and Vibes for',
        '# playlists in your "Vibes" folder. Copy those lines, without the leading "#",',
        "# under `playlists:` in config.yaml. Leave every other playlist out.",
        '# A blank "" value is rejected by config validation, so nothing is mapped by accident.',
        "#",
        f"# Already mapped in config.yaml ({len(mapped)}):",
        *(line(e, str(e["group"])) for e in mapped),
        "#",
        f"# Readable and unmapped: owned by you or collaborative ({len(readable)}):",
        *(line(e, '""') for e in readable),
        "#",
        f"# Not readable by this Spotify development app; owned by others ({len(unreadable)}).",
        "# Mapping one of these will be reported as unreadable during sync:",
        *(line(e, '""') for e in unreadable),
        "",
    ]
    return "\n".join(lines)


def write_local_report(directory: Path, summary: dict[str, Any]) -> Path:
    """Which mapped Spotify tracks already exist as local files."""
    stem = directory / f"local-match-{int(summary['generated_at'])}"
    save_json(stem.with_suffix(".json"), summary)
    t = summary["totals"]
    lines = [
        "# Local library check (read-only)",
        "",
        f"Files indexed: {summary['files_indexed']}. "
        f"Spotify tracks checked: {t['spotify_tracks']}. "
        "No audio file was opened for writing.",
        "",
        "| Result | Tracks |",
        "| --- | --- |",
        f"| Already on disk | {t['found']} |",
        f"| Already on disk and also matched on TIDAL | {t['found_and_tidal_matched']} |",
        f"| Possibly on disk (file unreadable, length unknown) | {t.get('possibly_found', 0)} |",
        f"| Same song on disk, different version or length | {t['other_version']} |",
        f"| Not found on disk | {t['missing']} |",
        f"| On disk more than once | {t['found_with_several_copies']} |",
        "",
    ]
    for status, heading in (
        ("found", "Already on disk"),
        ("possibly_found", "Possibly on disk: same title and artist, file unreadable"),
        ("other_version", "Same song, different version or length on disk"),
    ):
        rows = sorted(
            (x for x in summary["tracks"] if x["local_status"] == status),
            key=lambda x: (x["playlists"][0], x["spotify"]["artists"][0].casefold()),
        )
        lines.extend(
            [
                f"## {heading} ({len(rows)})",
                "",
                "| Spotify | Local file | TIDAL | Playlists |",
                "| --- | --- | --- | --- |",
            ]
        )
        for x in rows:
            s, f = x["spotify"], x["local"]
            lines.append(
                f"| {escape(', '.join(s['artists']))} — {escape(s['title'])} | "
                f"{escape(Path(f['path']).name)} | {x['tidal_status']} | "
                f"{escape('; '.join(x['playlists']))} |"
            )
        lines.append("")
    atomic_text(stem.with_suffix(".md"), "\n".join(lines))
    return stem.with_suffix(".md")


def write_music_report(directory: Path, report: dict[str, Any]) -> Path:
    """Reconciliation of mapped Spotify playlists against Music.app. Zero writes."""
    stem = directory / f"music-report-{int(report['generated_at'])}"
    save_json(stem.with_suffix(".json"), report)
    t = report["totals"]
    labels = {
        "in_playlist": "Already in the Music.app playlist",
        "add_existing": "In the library, to add to the playlist",
        "import_pending": "Converted AIFF waiting for import",
        "import_from_disk": "File on disk but not in the Music.app library",
        "download_pending": "Matched on TIDAL, not downloaded yet",
        "local": "Marked by you as already in Music.app, not found by name",
        "skipped": "Skipped by you",
        "unresolved": "Needs review",
    }
    lines = [
        "# Music.app reconciliation (read-only)",
        "",
        "No Music.app playlist, track or file was changed.",
        "",
        "| Outcome | Tracks |",
        "| --- | --- |",
        *(f"| {labels.get(k, k)} | {v} |" for k, v in sorted(t.items())),
        "",
        "| Spotify playlist | Music.app destination | How | In playlist | To add | To import | "
        "To download | Local-only |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in report["playlists"]:
        count = Counter(x["action"] for x in e["tracks"])
        d = e["destination"]
        lines.append(
            f"| {escape(e['group'])} / {escape(e['name'])} | {escape(d['name'])} | {d['how']} | "
            f"{count.get('in_playlist', 0)} | {count.get('add_existing', 0)} | "
            f"{count.get('import_pending', 0)} | {count.get('download_pending', 0)} | "
            f"{len(e['local_only'])} |"
        )
    extras = [e for e in report["playlists"] if e["local_only"]]
    if extras:
        lines.extend(["", "## Tracks only in Music.app (never removed automatically)", ""])
        for e in extras:
            lines.append(
                f"- {escape(e['name'])}: "
                + escape("; ".join(e["local_only_names"][:20]))
                + (" …" if len(e["local_only"]) > 20 else "")
            )
    lines.append("")
    atomic_text(stem.with_suffix(".md"), "\n".join(lines))
    return stem.with_suffix(".md")


def write_download_report(
    directory: Path, plan: dict[str, Any], verified: dict[str, Any], converted: dict[str, Any]
) -> Path:
    """One Tidaler run: what was requested, what really arrived, what was converted."""
    stem = directory / f"download-{plan['run_id']}"
    save_json(
        stem.with_suffix(".json"), {"plan": plan, "verified": verified, "converted": converted}
    )
    lines = [
        "# Download run (Tidaler)",
        "",
        f"Requested: {verified['requested']} · Verified by tags: {verified['downloaded']} · "
        f"Failed: {verified['failed']} · Wrong length: {verified['corrupt']} · "
        f"Sibling release (review): {verified['sibling_release']} · "
        f"Unexpected files: {verified['unexpected']} · Timed out: {verified['timed_out']}",
        "",
        f"Converted to AIFF: {converted['converted']} · Conversion failed: {converted['failed']}",
        "",
        f"Excluded before requesting: {escape(plan['excluded'])}. "
        f"Deferred by the per-run cap: {plan['deferred_by_limit']}.",
        "",
    ]
    for key, heading in (
        ("failed", "Not downloaded"),
        ("corrupt", "Wrong length (left in place; review by hand)"),
        ("sibling_release", "Same recording, different TIDAL release (review)"),
        ("unexpected", "Files not requested by this run"),
        ("unreadable", "Unreadable files"),
    ):
        rows = verified["details"].get(key, [])
        if rows:
            lines.extend([f"## {heading} ({len(rows)})", ""])
            lines.extend(f"- {escape(r)}" for r in rows)
            lines.append("")
    if converted["failures"]:
        lines.extend(["## Conversion failures", ""])
        lines.extend(f"- {escape(f['flac'])}: {escape(f['reason'])}" for f in converted["failures"])
        lines.append("")
    atomic_text(stem.with_suffix(".md"), "\n".join(lines))
    return stem.with_suffix(".md")
