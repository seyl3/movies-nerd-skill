#!/usr/bin/env python3
"""Find or remove macOS clutter and Portuguese sidecar subtitles without trash residue."""

from __future__ import annotations

import argparse
import json
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
        if not path.is_file() or ".movies-nerd" in path.parts or ".incoming" in path.parts:
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
        result = {"mode": "commit" if args.commit else "dry-run", "found": [str(path) for path in found], "removed": []}
        if args.commit and found:
            for path in found:
                if path.is_symlink() or not path.is_file() or root.resolve(strict=False) not in path.resolve(strict=True).parents:
                    raise ValueError("clutter target changed before cleanup")
                path.unlink()
                result["removed"].append(str(path))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
