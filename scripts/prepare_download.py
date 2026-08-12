#!/usr/bin/env python3
"""Plan a Movies Nerd transfer and optionally hand it to qBittorrent stopped."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from _common import GIB, format_gib, library_roots, parse_size, staging_roots
from qbittorrent_api import magnet_hash

MAX_BYTES = 15 * GIB
HEADROOM = 10 * GIB


def safe_label(title: str, year: int) -> str:
    clean = re.sub(r"[/:\\\x00-\x1f\x7f]", " - ", title).strip(" .")
    clean = re.sub(r"\s+", " ", clean)
    if not clean or clean in {".", ".."}:
        raise ValueError("title does not produce a safe folder name")
    return f"{clean} ({year})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--magnet", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--kind", choices=("movie", "series"), default="movie")
    parser.add_argument("--expected-size", required=True, help="tracker-reported size, for example 8.2 GiB")
    parser.add_argument("--allow-oversize", action="store_true")
    parser.add_argument("--execute", action="store_true", help="add stopped to qBittorrent after explicit approval")
    args = parser.parse_args()
    try:
        torrent_hash = magnet_hash(args.magnet)
        expected = parse_size(args.expected_size)
        if expected > MAX_BYTES and not args.allow_oversize:
            raise ValueError(f"expected payload {format_gib(expected)} exceeds the 15 GiB limit")
        movies_root, series_root = library_roots()
        movie_stage, series_stage = staging_roots()
        stage = movie_stage if args.kind == "movie" else series_stage
        probe = stage
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        expected_root = movies_root if args.kind == "movie" else series_root
        free = shutil.disk_usage(probe).free
        needed = expected + HEADROOM
        if free < needed:
            raise ValueError(f"need {format_gib(needed)} free including headroom, have {format_gib(free)}")
        label = safe_label(args.title, args.year)
        plan = {
            "action": "add-stopped-to-qbittorrent" if args.execute else "dry-run",
            "hash": torrent_hash,
            "title": label,
            "expected_size": expected,
            "expected_size_gib": round(expected / GIB, 2),
            "free_gib": round(free / GIB, 2),
            "staging": str(stage),
            "library_root": str(expected_root),
            "library_root_exists": expected_root.is_dir(),
            "next": "inspect client metadata, deselect extras, then start",
        }
        if not args.execute:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        script = Path(__file__).with_name("qbittorrent_api.py")
        command = [sys.executable, str(script), "add-paused", "--magnet", args.magnet, "--kind", args.kind, "--rename", label, "--commit"]
        completed = subprocess.run(command, check=False, env=os.environ.copy())
        return completed.returncode
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
