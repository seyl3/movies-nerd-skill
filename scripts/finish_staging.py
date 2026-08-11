#!/usr/bin/env python3
"""Recoverably remove completed job files from Movies Nerd staging."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from _common import library_roots, staging_roots
from payload_safety import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS


def library_for(destination: Path) -> tuple[Path, Path]:
    resolved = destination.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("final destination must be a regular library directory")
    for root, stage in zip(library_roots(), staging_roots()):
        fixed = root.resolve(strict=False)
        if fixed in resolved.parents and stage.resolve(strict=False) not in resolved.parents:
            return fixed, stage.resolve(strict=False)
    raise ValueError("final destination must be inside the selected library and outside staging")


def verify_final_destination(destination: Path) -> dict:
    videos = []
    metadata = []
    artwork = []
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise ValueError("final destination contains a symlink")
        if not path.is_file():
            continue
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("final destination contains a special file")
        suffix = path.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            videos.append(path)
        elif suffix == ".nfo":
            metadata.append(path)
        elif suffix in IMAGE_EXTENSIONS:
            artwork.append(path)
    if not videos or not metadata or not artwork:
        raise ValueError("final destination is not fully organized with media, metadata, and artwork")
    return {
        "videos": len(videos),
        "metadata": len(metadata),
        "artwork": len(artwork),
    }


def checked_targets(raw_targets: list[Path], stage: Path) -> list[Path]:
    targets = []
    for raw in raw_targets:
        if raw.is_symlink() or not raw.exists():
            raise ValueError("staged cleanup target must exist and must not be a symlink")
        resolved = raw.resolve(strict=True)
        if resolved == stage or stage not in resolved.parents:
            raise ValueError("staged cleanup target must be a job-specific path inside staging")
        targets.append(resolved)
        sidecar = resolved.with_name("._" + resolved.name)
        if sidecar.exists():
            if sidecar.is_symlink() or not sidecar.is_file():
                raise ValueError("staged AppleDouble sidecar is not a regular file")
            targets.append(sidecar.resolve(strict=True))
    unique = sorted(set(targets), key=lambda path: (len(path.parts), str(path)))
    for index, target in enumerate(unique):
        if any(parent == target or parent in target.parents for parent in unique[:index]):
            raise ValueError("staged cleanup targets must not overlap")
    return unique


def quarantine_targets(targets: list[Path], stage: Path, library: Path) -> tuple[Path, list[dict]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = library / ".movies-nerd-trash" / "completed-staging" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    moves = []
    for source in targets:
        relative = source.relative_to(stage)
        destination = quarantine / relative
        if destination.exists() or destination.is_symlink():
            raise ValueError("staging quarantine collision")
        moves.append((source, destination))
    completed: list[tuple[Path, Path]] = []
    try:
        for source, destination in moves:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(source, destination)
            completed.append((source, destination))
    except OSError:
        for source, destination in reversed(completed):
            if destination.exists() and not source.exists():
                source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(destination, source)
        raise
    return quarantine, [
        {"from": str(source), "to": str(destination)}
        for source, destination in completed
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--staged", type=Path, action="append", required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        library, stage = library_for(args.destination)
        final = verify_final_destination(args.destination.resolve(strict=True))
        targets = checked_targets(args.staged, stage)
        output = {
            "mode": "commit" if args.commit else "dry-run",
            "destination": str(args.destination.resolve(strict=True)),
            "final_destination": final,
            "staged_targets": [str(path) for path in targets],
        }
        if args.commit:
            quarantine, moved = quarantine_targets(targets, stage, library)
            output.update({"moved": moved, "recoverable_from": str(quarantine)})
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
