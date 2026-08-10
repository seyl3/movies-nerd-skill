#!/usr/bin/env python3
"""Inspect a staged payload and identify main media while skipping extras by default."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".webm", ".ts", ".m2ts"}
ALLOWED_COMPANIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".jpg", ".jpeg", ".png", ".nfo", ".txt"}
DANGEROUS_EXTENSIONS = {
    ".app", ".bat", ".cmd", ".com", ".dmg", ".exe", ".hta", ".iso", ".jar",
    ".js", ".lnk", ".msi", ".pkg", ".ps1", ".scr", ".sh", ".url", ".website",
}
EXTRA_PATTERN = re.compile(
    r"(?:^|[ ._\-/])(sample|trailer|teaser|featurette|extra|bonus|interview|"
    r"behind[ ._-]*the[ ._-]*scenes|deleted[ ._-]*scene|making[ ._-]*of)(?:$|[ ._\-/])",
    re.I,
)


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (ValueError, OSError):
        return False


def probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "format=duration:stream=width,height,codec_name",
            "-of", "json", str(path),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        return {"valid_media": False, "probe_error": result.stderr.strip()[:500]}
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0
    return {
        "valid_media": bool(stream.get("width") and stream.get("height")),
        "duration_seconds": round(duration, 3),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "video_codec": stream.get("codec_name"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    parser.add_argument("--series", action="store_true", help="treat all episode-like media as main content")
    args = parser.parse_args()
    root = args.payload.resolve(strict=True)
    if not root.is_dir():
        parser.error("payload must be a directory")

    entries = []
    hazards = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            resolved = path.resolve(strict=False)
            if not within(resolved, root):
                hazards.append({"path": str(path), "reason": "symlink escapes payload"})
            continue
        if not path.is_file():
            continue
        if not within(path, root):
            hazards.append({"path": str(path), "reason": "path escapes payload"})
            continue
        suffix = path.suffix.lower()
        relative = str(path.relative_to(root))
        if suffix in DANGEROUS_EXTENSIONS:
            hazards.append({"path": relative, "reason": "executable or installer-like file"})
            continue
        if suffix in VIDEO_EXTENSIONS:
            item = {"path": relative, "bytes": path.stat().st_size, "looks_like_extra": bool(EXTRA_PATTERN.search(relative))}
            item.update(probe(path))
            entries.append(item)
        elif suffix not in ALLOWED_COMPANIONS and not path.name.startswith("._") and path.name != ".DS_Store":
            hazards.append({"path": relative, "reason": "unexpected file type"})

    valid = [item for item in entries if item.get("valid_media")]
    if args.series:
        selected = [item for item in valid if not item["looks_like_extra"] and item.get("duration_seconds", 0) >= 10 * 60]
    else:
        selected = []
        candidates = [item for item in valid if not item["looks_like_extra"]]
        if candidates:
            selected = [max(candidates, key=lambda item: (item.get("duration_seconds", 0), item["bytes"]))]
    selected_paths = {item["path"] for item in selected}
    extras = [item for item in valid if item["path"] not in selected_paths]
    output = {
        "payload": str(root),
        "mode": "series" if args.series else "movie",
        "selected": selected,
        "extras_skipped_by_default": extras,
        "hazards": hazards,
        "safe_to_continue": bool(selected) and not hazards,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["safe_to_continue"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
