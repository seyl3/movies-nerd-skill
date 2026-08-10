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

# Collection-informed defaults measured from the user's feature-length library.
# The 1080p sample contained 45 films: median 1.001 GiB/hour, 75th percentile
# 1.334 GiB/hour, and median complete-film size 2.045 GiB.  The ranking target
# is deliberately set near the 75th percentile so ordinary good encodes are not
# punished, while clearly bloated alternatives lose decisively.
EFFICIENCY_PROFILE = {
    2160: {"target_gib_per_hour": 2.75, "soft_max_gib_per_hour": 4.00},
    1080: {"target_gib_per_hour": 1.35, "soft_max_gib_per_hour": 1.80},
    720: {"target_gib_per_hour": 0.85, "soft_max_gib_per_hour": 1.25},
}


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


def parse_runtime_minutes(value: object) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        minutes = float(value)
    else:
        text = str(value).strip().lower()
        clock = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text)
        hours_minutes = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h(?:ours?)?)?\s*(?:(\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?)?", text)
        if clock:
            hours = float(clock.group(1) or 0)
            minutes = hours * 60 + float(clock.group(2)) + float(clock.group(3)) / 60
        elif hours_minutes and (hours_minutes.group(1) or hours_minutes.group(2)):
            minutes = float(hours_minutes.group(1) or 0) * 60 + float(hours_minutes.group(2) or 0)
        else:
            try:
                minutes = float(text)
            except ValueError:
                return None
    return minutes if 1 <= minutes <= 1440 else None


def efficiency(resolution: int, size_bytes: int, runtime_minutes: float | None) -> dict:
    profile = EFFICIENCY_PROFILE.get(resolution)
    if not profile or not runtime_minutes or size_bytes <= 0:
        return {
            "rating": "unknown",
            "gib_per_hour": None,
            "target_gib_per_hour": profile["target_gib_per_hour"] if profile else None,
            "soft_max_gib_per_hour": profile["soft_max_gib_per_hour"] if profile else None,
            "score_adjustment": 0.0,
        }

    gib_per_hour = (size_bytes / GIB) / (runtime_minutes / 60)
    target = profile["target_gib_per_hour"]
    soft_max = profile["soft_max_gib_per_hour"]
    compact_floor = target * 0.45
    if gib_per_hour < compact_floor:
        rating = "verify-compact"
        adjustment = -15.0
    elif gib_per_hour <= target:
        rating = "efficient"
        adjustment = 25.0
    elif gib_per_hour <= soft_max:
        rating = "balanced"
        adjustment = 25.0 * (soft_max - gib_per_hour) / (soft_max - target)
    else:
        rating = "bloated"
        adjustment = max(-120.0, -(gib_per_hour - soft_max) * 55.0)
    return {
        "rating": rating,
        "gib_per_hour": round(gib_per_hour, 2),
        "target_gib_per_hour": target,
        "soft_max_gib_per_hour": soft_max,
        "score_adjustment": round(adjustment, 2),
    }


def normalize(item: dict, max_bytes: int, runtime_minutes: float | None = None) -> dict:
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
    item_runtime = parse_runtime_minutes(first_value(item, ("runtime_minutes", "runtime", "duration_minutes")))
    runtime_minutes = item_runtime or parse_runtime_minutes(runtime_minutes)
    size_efficiency = efficiency(resolution, size_bytes, runtime_minutes)
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
    if size_efficiency["rating"] == "bloated":
        warnings.append(
            f"large for its runtime ({size_efficiency['gib_per_hour']:.2f} GiB/hour; "
            f"soft maximum {size_efficiency['soft_max_gib_per_hour']:.2f})"
        )
    elif size_efficiency["rating"] == "verify-compact":
        warnings.append("unusually compact for its runtime; verify source quality")
    if magnet and not magnet.startswith("magnet:?xt=urn:btih:"):
        warnings.append("invalid magnet format")

    tier = quality_tier(resolution, size_bytes, max_bytes) if size_bytes >= 0 else 0
    eligible = bool(title and size_bytes > 0 and tier >= 3 and not suspicious)
    health = min(40.0, math.log2(seeders + 1) * 5.0)
    score = tier * 100 + codec_score(title) * 10 + health + size_efficiency["score_adjustment"]
    if suspicious:
        score -= 500
    if size_bytes < 0 or size_bytes > max_bytes:
        score -= 1000

    if eligible and size_efficiency["rating"] == "bloated":
        reason = "eligible but large for its runtime; prefer a comparable efficient encode"
    elif eligible and size_efficiency["rating"] == "verify-compact":
        reason = "eligible but unusually compact; verify source quality"
    elif eligible and resolution >= 2160:
        reason = "preferred efficient 4K within the 15 GiB policy"
    elif eligible and resolution == 1080:
        reason = "preferred space-efficient 1080p fallback"
    else:
        reason = "not eligible under the default policy"

    return {
        "title": title,
        "source": source,
        "size_bytes": size_bytes if size_bytes >= 0 else None,
        "size": format_gib(size_bytes) if size_bytes >= 0 else None,
        "seeders": seeders,
        "resolution": f"{resolution}p" if resolution else None,
        "runtime_minutes": round(runtime_minutes, 2) if runtime_minutes else None,
        "size_efficiency": size_efficiency,
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
    parser.add_argument("--runtime-min", type=float, help="authoritative title runtime in minutes")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--table", action="store_true")
    args = parser.parse_args()
    if not (0 < args.max_gib <= 100):
        parser.error("--max-gib must be between 0 and 100")
    if not (1 <= args.top <= 100):
        parser.error("--top must be between 1 and 100")
    if args.runtime_min is not None and not (1 <= args.runtime_min <= 1440):
        parser.error("--runtime-min must be between 1 and 1440")

    payload = load_input(args.input)
    if isinstance(payload, dict):
        payload = first_value(payload, ("results", "items", "data"), [])
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("input must be a JSON array of result objects")
    max_bytes = int(args.max_gib * GIB)
    ranked = [normalize(item, max_bytes, args.runtime_min) for item in payload]
    ranked.sort(key=lambda item: (item["eligible"], item["score"], item["seeders"]), reverse=True)
    ranked = ranked[: args.top]
    if args.table:
        print("RANK\tELIGIBLE\tRES\tSIZE\tGIB/H\tEFFICIENCY\tSEEDS\tSOURCE\tTITLE\tWARNINGS")
        for index, item in enumerate(ranked, 1):
            size_efficiency = item["size_efficiency"]
            gib_per_hour = size_efficiency["gib_per_hour"]
            print(
                f"{index}\t{str(item['eligible']).lower()}\t{item['resolution'] or '-'}\t"
                f"{item['size'] or '-'}\t{gib_per_hour if gib_per_hour is not None else '-'}\t"
                f"{size_efficiency['rating']}\t{item['seeders']}\t{item['source']}\t{item['title']}\t"
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
