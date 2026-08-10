#!/usr/bin/env python3
"""Find or recoverably quarantine macOS clutter and Portuguese sidecar subtitles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from _common import library_roots
SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
PORTUGUESE_RE = re.compile(r"(?:^|[._ -])(pt|por|pob|portuguese)(?:$|[._ -])", re.I)


def root_for(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    for root in library_roots():
        fixed = root.resolve(strict=False)
        if resolved == fixed or fixed in resolved.parents:
            return root
    raise ValueError("target must be inside the configured Movies or Series root")


def targets(base: Path) -> list[Path]:
    found = []
    for path in base.rglob("*"):
        if not path.is_file() or ".movies-nerd-trash" in path.parts or ".incoming" in path.parts:
            continue
        if path.name == ".DS_Store" or path.name.startswith("._"):
            found.append(path)
        elif path.suffix.lower() in SUBTITLE_SUFFIXES and PORTUGUESE_RE.search(path.stem):
            found.append(path)
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        base = args.target.resolve(strict=True)
        root = root_for(base)
        found = targets(base if base.is_dir() else base.parent)
        result = {"mode": "commit" if args.commit else "dry-run", "found": [str(path) for path in found], "moved": []}
        if args.commit and found:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            quarantine = root / ".movies-nerd-trash" / stamp
            for path in found:
                relative = path.relative_to(root)
                destination = quarantine / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError(f"quarantine collision: {destination}")
                os.replace(path, destination)
                result["moved"].append({"from": str(path), "to": str(destination)})
            result["recoverable_from"] = str(quarantine)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
