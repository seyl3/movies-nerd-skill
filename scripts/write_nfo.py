#!/usr/bin/env python3
"""Render or atomically write Kodi/Jellyfin-compatible NFO XML."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET

from _common import library_roots
SCALARS = {
    "movie": ("title", "originaltitle", "sorttitle", "year", "plot", "premiered", "runtime", "rating", "studio", "country"),
    "tvshow": ("title", "originaltitle", "year", "plot", "premiered", "status", "runtime", "rating", "studio"),
    "episodedetails": ("showtitle", "title", "season", "episode", "plot", "aired", "runtime", "rating"),
}


def add_text(parent: ET.Element, tag: str, value: object) -> None:
    if value is None or value == "":
        return
    child = ET.SubElement(parent, tag)
    child.text = str(value)


def render(kind: str, data: dict) -> bytes:
    root = ET.Element(kind)
    for key in SCALARS[kind]:
        add_text(root, key, data.get(key))
    for genre in data.get("genres", []):
        add_text(root, "genre", genre)
    for director in data.get("directors", []):
        add_text(root, "director", director)
    unique_ids = data.get("uniqueids", {})
    if not isinstance(unique_ids, dict):
        raise ValueError("uniqueids must be an object")
    default_id = data.get("default_uniqueid")
    for name, value in sorted(unique_ids.items()):
        node = ET.SubElement(root, "uniqueid", {"type": str(name), "default": "true" if name == default_id else "false"})
        node.text = str(value)
    ET.indent(root, space="  ")
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    ET.fromstring(payload)
    return payload


def checked_output(raw: str) -> Path:
    output = Path(raw).resolve(strict=False)
    if output.suffix.lower() != ".nfo":
        raise ValueError("output must end in .nfo")
    if not any(output == root or root.resolve(strict=False) in output.parents for root in library_roots()):
        raise ValueError("output must be inside the configured Movies or Series root")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=tuple(SCALARS), required=True)
    parser.add_argument("--input", required=True, help="UTF-8 JSON metadata file")
    parser.add_argument("--output")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        source = Path(args.input)
        if source.stat().st_size > 1024 * 1024:
            raise ValueError("metadata JSON exceeds 1 MiB")
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("metadata JSON must be an object")
        payload = render(args.kind, data)
        if not args.write:
            sys.stdout.buffer.write(payload)
            return 0
        if not args.output:
            raise ValueError("--output is required with --write")
        output = checked_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, output)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        print(json.dumps({"written": str(output), "bytes": len(payload)}, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, ET.ParseError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
