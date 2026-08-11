#!/usr/bin/env python3
"""Run one post-download safety gate and select the main staged media."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path

from media_probe import ProbeError, probe_media

from payload_safety import (
    ALLOWED_COMPANIONS,
    MAX_PAYLOAD_FILES,
    VIDEO_EXTENSIONS,
    content_reasons,
    directory_reasons,
    filename_reasons,
)

EXTRA_PATTERN = re.compile(
    r"(?:^|[ ._\-/])(sample|trailer|teaser|featurette|extra|bonus|interview|"
    r"behind[ ._-]*the[ ._-]*scenes|deleted[ ._-]*scene|making[ ._-]*of)(?:$|[ ._\-/])",
    re.I,
)


def payload_paths(root: Path):
    """Walk without following directory symlinks or loading the full tree."""
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    path = Path(entry.path)
                    try:
                        is_directory = entry.is_dir(follow_symlinks=False)
                    except OSError as exc:
                        yield path, exc
                        continue
                    yield path, None
                    if is_directory:
                        pending.append(path)
        except OSError as exc:
            yield current, exc


def scan_payload(root: Path, series: bool = False) -> dict:
    entries = []
    hazards = []
    checked_files = 0
    checked_entries = 0
    validated_companions = 0

    for path, walk_error in payload_paths(root):
        relative = str(path.relative_to(root))
        checked_entries += 1
        if checked_entries > MAX_PAYLOAD_FILES:
            hazards.append({
                "path": "<payload>",
                "reason": f"payload contains more than {MAX_PAYLOAD_FILES} filesystem entries",
            })
            break
        if walk_error:
            hazards.append({"path": relative, "reason": f"cannot traverse payload safely: {walk_error}"})
            continue
        if path.is_symlink():
            hazards.append({"path": relative, "reason": "symlinks are not permitted in payloads"})
            continue
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            hazards.append({"path": relative, "reason": f"cannot inspect filesystem entry: {exc}"})
            continue
        if stat.S_ISDIR(mode):
            reasons = directory_reasons(relative)
            if reasons:
                hazards.append({"path": relative, "reason": "; ".join(reasons)})
            continue
        if not stat.S_ISREG(mode):
            hazards.append({"path": relative, "reason": "special filesystem entry is not permitted"})
            continue

        checked_files += 1
        suffix = path.suffix.lower()
        reasons = filename_reasons(relative)
        if not reasons:
            try:
                reasons.extend(content_reasons(path, suffix))
            except OSError as exc:
                reasons.append(f"cannot inspect file content: {exc}")
        if reasons:
            hazards.append({"path": relative, "reason": "; ".join(reasons)})
            continue

        if suffix in VIDEO_EXTENSIONS:
            try:
                before_probe = path.stat(follow_symlinks=False)
            except OSError as exc:
                hazards.append({"path": relative, "reason": f"cannot inspect media before probe: {exc}"})
                continue
            item = {
                "path": relative,
                "bytes": before_probe.st_size,
                "looks_like_extra": bool(EXTRA_PATTERN.search(relative)),
            }
            try:
                full_probe = probe_media(path)
                item["probe"] = full_probe
                item.update(full_probe["summary"])
            except (OSError, ProbeError) as exc:
                item.update({"valid_media": False, "probe_error": str(exc)[:500]})
            if not item.get("valid_media"):
                hazards.append({
                    "path": relative,
                    "reason": "invalid media container or disguised non-media file",
                    "detail": item.get("probe_error"),
                })
                continue
            entries.append(item)
        elif suffix in ALLOWED_COMPANIONS:
            validated_companions += 1

    valid = [item for item in entries if item.get("valid_media")]
    if series:
        selected = [
            item for item in valid
            if not item["looks_like_extra"] and item.get("duration_seconds", 0) >= 10 * 60
        ]
    else:
        candidates = [item for item in valid if not item["looks_like_extra"]]
        selected = [max(candidates, key=lambda item: (item.get("duration_seconds", 0), item["bytes"]))] if candidates else []
    selected_paths = {item["path"] for item in selected}
    extras = [item for item in valid if item["path"] not in selected_paths]
    return {
        "payload": str(root),
        "mode": "series" if series else "movie",
        "security_gate": {
            "entries_checked": checked_entries,
            "files_checked": checked_files,
            "companions_validated": validated_companions,
            "checks": ["filename", "path", "content-signature", "media-probe"],
        },
        "selected": selected,
        "extras_skipped_by_default": extras,
        "hazards": hazards,
        "safe_to_continue": bool(selected) and not hazards,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    parser.add_argument("--series", action="store_true", help="treat all episode-like media as main content")
    args = parser.parse_args()
    if args.payload.is_symlink():
        parser.error("payload root must not be a symlink")
    root = args.payload.resolve(strict=True)
    if not root.is_dir():
        parser.error("payload must be a directory")
    output = scan_payload(root, args.series)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["safe_to_continue"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
