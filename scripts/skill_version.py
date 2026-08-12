#!/usr/bin/env python3
"""Print the installed Movies Nerd semantic version."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


def version_path() -> Path:
    return Path(__file__).resolve().parents[1] / "VERSION"


def read_version(path: Path | None = None) -> str:
    version = (path or version_path()).read_text(encoding="utf-8").strip()
    if not SEMVER.fullmatch(version):
        raise ValueError("VERSION must contain one semantic version")
    return version


def label(version: str) -> str:
    return f"Movies Nerd v{version}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args()
    try:
        version = read_version()
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"skill": "movies-nerd", "version": version, "label": label(version)}, indent=2))
    else:
        print(label(version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
