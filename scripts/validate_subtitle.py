#!/usr/bin/env python3
"""Validate an untrusted SRT download without reproducing its text."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

MAX_BYTES = 5 * 1024 * 1024
TIMESTAMP_RE = re.compile(
    r"(?m)^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})(?:\s+[^\r\n]*)?$"
)


def milliseconds(parts: tuple[str, str, str, str]) -> int:
    hours, minutes, seconds, millis = map(int, parts)
    if minutes > 59 or seconds > 59 or millis > 999 or hours > 99:
        raise ValueError("invalid SRT timestamp")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def validate_bytes(data: bytes, expected_language: str, media_duration: float | None = None, forced: bool = False) -> dict:
    reasons = []
    if not data or len(data) > MAX_BYTES:
        reasons.append("file is empty or exceeds 5 MiB")
    signatures = (b"MZ", b"PK\x03\x04", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")
    if any(data.startswith(signature) for signature in signatures):
        reasons.append("archive or executable signature")
    if b"\x00" in data:
        reasons.append("binary NUL byte")
    encoding = "utf-8"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
            encoding = "cp1252"
        except UnicodeDecodeError:
            text = ""
            reasons.append("unsupported text encoding")
    prefix = text.lstrip()[:500].lower()
    if prefix.startswith(("<!doctype html", "<html", "<?xml")) or "cloudflare" in prefix or "just a moment" in prefix:
        reasons.append("HTML or challenge page, not an SRT")
    if any(len(line) > 20_000 for line in text.splitlines()):
        reasons.append("unreasonably long text line")
    cues = []
    try:
        for match in TIMESTAMP_RE.finditer(text):
            start = milliseconds(match.groups()[:4])
            end = milliseconds(match.groups()[4:])
            if end <= start:
                raise ValueError("subtitle cue ends before it starts")
            cues.append((start, end))
    except ValueError as exc:
        reasons.append(str(exc))
    if len(cues) < 5:
        reasons.append("fewer than five valid SRT cues")
    first_ms = min((start for start, _ in cues), default=None)
    last_ms = max((end for _, end in cues), default=None)
    sync_plausible = True
    coverage_ratio = None
    if media_duration and last_ms is not None:
        coverage_ratio = last_ms / (media_duration * 1000)
        if first_ms is not None and first_ms > media_duration * 1000:
            sync_plausible = False
        if last_ms > (media_duration + 600) * 1000:
            sync_plausible = False
        if not sync_plausible:
            reasons.append("subtitle timing is implausible for the media duration")
    full_coverage_candidate = bool(cues) and not forced and coverage_ratio is not None and coverage_ratio >= 0.5
    return {
        "valid": not reasons,
        "expected_language": expected_language,
        "encoding": encoding,
        "bytes": len(data),
        "cue_count": len(cues),
        "first_cue_seconds": round(first_ms / 1000, 3) if first_ms is not None else None,
        "last_cue_seconds": round(last_ms / 1000, 3) if last_ms is not None else None,
        "media_duration_seconds": round(media_duration, 3) if media_duration else None,
        "coverage_ratio": round(coverage_ratio, 3) if coverage_ratio is not None else None,
        "forced_or_partial": forced or not full_coverage_candidate,
        "counts_as_full_coverage": full_coverage_candidate and not reasons,
        "reasons": reasons,
    }


def media_duration(path: Path) -> float:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return float(completed.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subtitle", type=Path)
    parser.add_argument("--expected-language", choices=("en", "fr"), required=True)
    parser.add_argument("--media", type=Path)
    parser.add_argument("--forced", action="store_true")
    args = parser.parse_args()
    try:
        if args.subtitle.is_symlink():
            raise ValueError("subtitle must not be a symlink")
        subtitle = args.subtitle.resolve(strict=True)
        if not subtitle.is_file() or subtitle.suffix.lower() != ".srt":
            raise ValueError("subtitle must be a regular, non-symlink .srt file")
        data = subtitle.read_bytes()
        duration = media_duration(args.media.resolve(strict=True)) if args.media else None
        report = validate_bytes(data, args.expected_language, duration, args.forced)
        report["path"] = str(subtitle)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["valid"] else 4
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
