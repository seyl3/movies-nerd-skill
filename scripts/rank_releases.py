#!/usr/bin/env python3
"""Rank normalized tracker results without fetching or downloading anything."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

from _common import GIB, first_value, format_gib, parse_size

BAD_PATTERN = re.compile(
    r"(?:^|[ ._\-])(cam|hdcam|telesync|telecine|ts|tc|dvdscr|screener|sample)(?:$|[ ._\-])",
    re.I,
)


def infer_resolution(title: str) -> int:
    text = title.lower()
    if re.search(r"\b(4320p|8k)\b", text):
        return 4320
    if re.search(r"\b(2160p|4k|uhd)\b", text):
        return 2160
    if re.search(r"\b1080[pi]?\b", text):
        return 1080
    if re.search(r"\b720[pi]?\b", text):
        return 720
    if re.search(r"\b576[pi]?\b", text):
        return 576
    if re.search(r"\b480[pi]?\b", text):
        return 480
    return 0


def codec_score(title: str) -> int:
    text = title.lower()
    if re.search(r"\b(av1|x265|h[ .]?265|hevc)\b", text):
        return 3
    if re.search(r"\b(x264|h[ .]?264|avc)\b", text):
        return 2
    return 1


def quality_tier(resolution: int, size_bytes: int, max_bytes: int) -> int:
    if size_bytes > max_bytes:
        return 0
    if resolution >= 2160:
        return 4
    if resolution == 1080:
        return 3
    if resolution == 720:
        return 2
    return 1 if resolution else 0


def normalize(item: dict, max_bytes: int) -> dict:
    title = str(first_value(item, ("title", "name", "Name"), "")).strip()
    size_raw = first_value(item, ("size", "Size", "bytes", "length"))
    try:
        size_bytes = parse_size(size_raw)
        size_error = None
    except (ValueError, TypeError):
        size_bytes = -1
        size_error = "missing or invalid size"
    seeders_raw = first_value(item, ("seeders", "Seeders", "seeds", "Seeds"), 0)
    try:
        seeders = max(0, int(str(seeders_raw).replace(",", "")))
    except ValueError:
        seeders = 0
    source = str(first_value(item, ("source", "Source", "tracker"), "unknown"))
    magnet = str(first_value(item, ("magnet", "Magnet", "magnetUri"), ""))
    resolution = infer_resolution(title)
    suspicious = bool(BAD_PATTERN.search(title))
    warnings = []
    if not title:
        warnings.append("missing title")
    if size_error:
        warnings.append(size_error)
    elif size_bytes > max_bytes:
        warnings.append(f"exceeds {format_gib(max_bytes)} limit")
    if resolution == 0:
        warnings.append("resolution not recognized")
    if suspicious:
        warnings.append("low-quality or sample marker")
    if seeders < 10:
        warnings.append("fewer than 10 seeders")
    if magnet and not magnet.startswith("magnet:?xt=urn:btih:"):
        warnings.append("invalid magnet format")

    tier = quality_tier(resolution, size_bytes, max_bytes) if size_bytes >= 0 else 0
    eligible = bool(title and size_bytes > 0 and tier >= 3 and not suspicious)
    health = min(40.0, math.log2(seeders + 1) * 5.0)
    score = tier * 100 + codec_score(title) * 10 + health
    if suspicious:
        score -= 500
    if size_bytes < 0 or size_bytes > max_bytes:
        score -= 1000

    if eligible and resolution >= 2160:
        reason = "preferred 4K within the 15 GiB policy"
    elif eligible and resolution == 1080:
        reason = "preferred 1080p fallback"
    else:
        reason = "not eligible under the default policy"

    return {
        "title": title,
        "source": source,
        "size_bytes": size_bytes if size_bytes >= 0 else None,
        "size": format_gib(size_bytes) if size_bytes >= 0 else None,
        "seeders": seeders,
        "resolution": f"{resolution}p" if resolution else None,
        "codec_preference": codec_score(title),
        "eligible": eligible,
        "score": round(score, 2),
        "reason": reason,
        "warnings": warnings,
        "magnet": magnet or None,
    }


def load_input(path: str | None) -> object:
    if path:
        raw = Path(path).read_bytes()
    else:
        raw = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("input exceeds 5 MiB limit")
    return json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="JSON file; stdin when omitted")
    parser.add_argument("--max-gib", type=float, default=15.0)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--table", action="store_true")
    args = parser.parse_args()
    if not (0 < args.max_gib <= 100):
        parser.error("--max-gib must be between 0 and 100")
    if not (1 <= args.top <= 100):
        parser.error("--top must be between 1 and 100")

    payload = load_input(args.input)
    if isinstance(payload, dict):
        payload = first_value(payload, ("results", "items", "data"), [])
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("input must be a JSON array of result objects")
    max_bytes = int(args.max_gib * GIB)
    ranked = [normalize(item, max_bytes) for item in payload]
    ranked.sort(key=lambda item: (item["eligible"], item["score"], item["seeders"]), reverse=True)
    ranked = ranked[: args.top]
    if args.table:
        print("RANK\tELIGIBLE\tRES\tSIZE\tSEEDS\tSOURCE\tTITLE\tWARNINGS")
        for index, item in enumerate(ranked, 1):
            print(
                f"{index}\t{str(item['eligible']).lower()}\t{item['resolution'] or '-'}\t"
                f"{item['size'] or '-'}\t{item['seeders']}\t{item['source']}\t{item['title']}\t"
                f"{'; '.join(item['warnings'])}"
            )
    else:
        json.dump(ranked, sys.stdout, ensure_ascii=False, indent=2)
        print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
