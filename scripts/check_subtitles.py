#!/usr/bin/env python3
"""Audit embedded and sidecar English/French subtitle coverage."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

LANG_MAP = {
    "en": "eng", "eng": "eng", "english": "eng",
    "fr": "fre", "fra": "fre", "fre": "fre", "french": "fre",
    "pt": "por", "por": "por", "pob": "por", "portuguese": "por",
}


def embedded_languages(media: Path) -> list[dict]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "s",
            "-show_entries", "stream=index:stream_tags=language,title,handler_name",
            "-of", "json", str(media),
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    output = []
    for stream in json.loads(result.stdout).get("streams", []):
        tags = stream.get("tags") or {}
        raw = str(tags.get("language") or "und").lower()
        output.append({
            "index": stream.get("index"),
            "language": LANG_MAP.get(raw, raw),
            "title": tags.get("title") or tags.get("handler_name"),
        })
    return output


def sidecar_language(path: Path, stem: str) -> str | None:
    name = path.name.lower()
    base = stem.lower()
    if not name.startswith(base):
        return None
    remainder = name[len(base):]
    tokens = [token for token in re.split(r"[._\- ]+", remainder) if token]
    for token in tokens:
        if token in LANG_MAP:
            return LANG_MAP[token]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("media", type=Path)
    args = parser.parse_args()
    media = args.media.resolve(strict=True)
    embedded = embedded_languages(media)
    sidecars = []
    for path in sorted(media.parent.iterdir()):
        if path.is_file() and not path.name.startswith("._") and path.suffix.lower() in {".srt", ".ass", ".ssa", ".vtt", ".sub"}:
            language = sidecar_language(path, media.stem)
            sidecars.append({"path": str(path), "language": language or "und"})
    languages = {item["language"] for item in embedded + sidecars}
    portuguese = [item["path"] for item in sidecars if item["language"] == "por"]
    output = {
        "media": str(media),
        "embedded": embedded,
        "sidecars": sidecars,
        "english": "eng" in languages,
        "french": "fre" in languages,
        "complete": {"english": "eng" in languages, "french": "fre" in languages},
        "missing": [name for code, name in (("eng", "English"), ("fre", "French")) if code not in languages],
        "portuguese_sidecars_to_remove": portuguese,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not output["missing"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
