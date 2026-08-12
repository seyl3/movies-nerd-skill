#!/usr/bin/env python3
"""Run one post-download safety gate and select the main staged media."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path

from _common import staging_roots
from media_probe import ProbeError, probe_media, snapshot

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
    companions = []

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
            companions.append({"path": relative, "reason": "replace release companion with trusted library sidecar"})

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
    discardable = list(hazards) + companions + [
        {"path": item["path"], "reason": "non-main video or extra"}
        for item in extras
    ]
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
        "discardable_items": discardable,
        "cleanup_required": bool(discardable),
        "safe_to_extract_selected": bool(selected),
        "safe_to_continue": bool(selected) and not discardable,
    }


def containing_stage(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    for stage in staging_roots():
        fixed = stage.resolve(strict=False)
        if resolved != fixed and fixed in resolved.parents:
            return fixed
    raise ValueError("payload must be a job-specific path inside Movies Nerd staging")


def clean_destination(raw: Path, source: Path, stage: Path) -> Path:
    if not raw.is_absolute():
        raise ValueError("clean staging destination must be an absolute path")
    if raw.is_symlink() or raw.exists():
        raise ValueError("clean staging destination must not already exist")
    resolved = raw.resolve(strict=False)
    if resolved == stage or stage not in resolved.parents:
        raise ValueError("clean staging destination must stay inside the same Movies Nerd staging root")
    if resolved == source or source in resolved.parents or resolved in source.parents:
        raise ValueError("clean staging destination must be separate from the release payload")
    return resolved


def extract_selected(root: Path, output: Path, report: dict, series: bool = False) -> dict:
    """Move only verified selected media into a new clean staging directory."""
    if not report.get("selected"):
        raise ValueError("no verified main media is available to clean")
    stage = containing_stage(root)
    clean = clean_destination(output, root, stage)
    clean.mkdir(mode=0o700, parents=True, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    try:
        for item in report["selected"]:
            relative = Path(item["path"])
            source = (root / relative).resolve(strict=True)
            if source.is_symlink() or root not in source.parents or not source.is_file():
                raise ValueError("selected media changed before clean extraction")
            saved_snapshot = item.get("probe", {}).get("snapshot")
            if not saved_snapshot or snapshot(source) != saved_snapshot:
                raise ValueError("selected media changed before clean extraction")
            destination = clean / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise ValueError("clean staging destination collision")
            os.replace(source, destination)
            moved.append((source, destination))
        verified = scan_payload(clean, series)
        if not verified["safe_to_extract_selected"]:
            raise ValueError("cleaned media did not pass verification")
        return {
            "clean_payload": str(clean),
            "moved_media": [str(destination.relative_to(clean)) for _, destination in moved],
            "verification": verified,
        }
    except (OSError, ValueError):
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(destination, source)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    parser.add_argument("--series", action="store_true", help="treat all episode-like media as main content")
    parser.add_argument("--clean-dir", type=Path, help="new staging directory that will receive only verified media")
    parser.add_argument("--commit", action="store_true", help="move verified media into --clean-dir")
    args = parser.parse_args()
    if args.commit != bool(args.clean_dir):
        parser.error("use --clean-dir and --commit together")
    if args.payload.is_symlink():
        parser.error("payload root must not be a symlink")
    root = args.payload.resolve(strict=True)
    if not root.is_dir():
        parser.error("payload must be a directory")
    output = scan_payload(root, args.series)
    if args.commit:
        try:
            output["cleaning"] = extract_selected(root, args.clean_dir, output, args.series)
        except (OSError, ValueError) as exc:
            output["cleaning_error"] = str(exc)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 5
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if args.commit and output.get("cleaning", {}).get("verification", {}).get("safe_to_extract_selected"):
        return 0
    return 0 if output["safe_to_continue"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
