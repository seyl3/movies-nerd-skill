#!/usr/bin/env python3
"""Atomically generate or verify a root SHA256SUMS.txt manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from _common import library_roots
MANIFEST = "SHA256SUMS.txt"
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def checked_root(raw: str) -> Path:
    candidate = Path(raw).resolve(strict=True)
    for root in library_roots():
        if candidate == root.resolve(strict=False):
            return candidate
    raise ValueError("root must be exactly the configured Movies or Series root")


def included(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if relative.name == MANIFEST or relative.name == ".DS_Store" or relative.name.startswith("._"):
        return False
    if relative.parts and relative.parts[0] in {".incoming", ".movies-nerd-trash", ".git"}:
        return False
    return path.is_file() and not path.is_symlink()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def entries(root: Path) -> list[tuple[str, str]]:
    output = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        if not included(path, root):
            continue
        relative = str(path.relative_to(root))
        if CONTROL_RE.search(relative):
            raise ValueError(f"manifest cannot safely represent control characters: {relative!r}")
        output.append((digest(path), relative))
    return output


def render(items: list[tuple[str, str]]) -> bytes:
    return "".join(f"{checksum}  {name}\n" for checksum, name in items).encode("utf-8")


def parse_manifest(path: Path) -> list[tuple[str, str]]:
    parsed = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ValueError(f"invalid manifest line {number}")
        parsed.append((match.group(1), match.group(2)))
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true")
    action.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        root = checked_root(args.root)
        current = entries(root)
        manifest = root / MANIFEST
        if args.verify:
            if not manifest.is_file():
                raise ValueError(f"manifest not found: {manifest}")
            recorded = parse_manifest(manifest)
            missing = sorted(set(recorded) - set(current))
            unexpected = sorted(set(current) - set(recorded))
            output = {"verified": not missing and not unexpected, "entries": len(current), "missing_or_changed": missing, "unexpected_or_changed": unexpected}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if output["verified"] else 6
        payload = render(current)
        if not args.write:
            print(json.dumps({"mode": "dry-run", "root": str(root), "entries": len(current), "manifest_bytes": len(payload)}, indent=2))
            return 0
        fd, temporary = tempfile.mkstemp(prefix=f".{MANIFEST}.", suffix=".tmp", dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, manifest)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        print(json.dumps({"written": str(manifest), "entries": len(current), "bytes": len(payload)}, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
