#!/usr/bin/env python3
"""Create and safely reuse one bounded full ffprobe report per media file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

MAX_OUTPUT_BYTES = 5 * 1024 * 1024
SCHEMA = "movies-nerd-media-probe-v1"
SHOW_ENTRIES = (
    "format=format_name,duration,size,bit_rate:"
    "stream=index,codec_type,codec_name,width,height,sample_rate,channels,channel_layout:"
    "stream_tags=language,title,handler_name:"
    "stream_disposition=default,forced,hearing_impaired:"
    "chapter=id,start_time,end_time:chapter_tags=title"
)


class ProbeError(ValueError):
    pass


def snapshot(path: Path) -> dict:
    if path.is_symlink():
        raise ProbeError("media must not be a symlink")
    details = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode):
        raise ProbeError("media must be a regular file")
    return {
        "device": details.st_dev,
        "inode": details.st_ino,
        "bytes": details.st_size,
        "mtime_ns": details.st_mtime_ns,
    }


def bounded_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)[:1_000]


def clean_probe(data: object) -> dict:
    if not isinstance(data, dict):
        raise ProbeError("ffprobe returned an invalid object")
    streams = []
    for stream in data.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        streams.append({
            "index": stream.get("index"),
            "codec_type": bounded_text(stream.get("codec_type")),
            "codec_name": bounded_text(stream.get("codec_name")),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "sample_rate": bounded_text(stream.get("sample_rate")),
            "channels": stream.get("channels"),
            "channel_layout": bounded_text(stream.get("channel_layout")),
            "tags": {
                key: bounded_text(tags.get(key))
                for key in ("language", "title", "handler_name")
                if bounded_text(tags.get(key)) is not None
            },
            "disposition": {
                key: 1 if disposition.get(key) else 0
                for key in ("default", "forced", "hearing_impaired")
            },
        })
    chapters = []
    for chapter in data.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        tags = chapter.get("tags") if isinstance(chapter.get("tags"), dict) else {}
        chapters.append({
            "id": chapter.get("id"),
            "start_time": bounded_text(chapter.get("start_time")),
            "end_time": bounded_text(chapter.get("end_time")),
            "title": bounded_text(tags.get("title")),
        })
    raw_format = data.get("format") if isinstance(data.get("format"), dict) else {}
    return {
        "streams": streams,
        "chapters": chapters,
        "format": {
            "format_name": bounded_text(raw_format.get("format_name")),
            "duration": bounded_text(raw_format.get("duration")),
            "size": bounded_text(raw_format.get("size")),
            "bit_rate": bounded_text(raw_format.get("bit_rate")),
        },
    }


def summary(info: dict) -> dict:
    videos = [stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"]
    primary = videos[0] if videos else {}
    try:
        duration = float((info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    valid = bool(
        primary.get("width") and primary.get("height")
        and primary.get("codec_name") and duration > 0
    )
    return {
        "valid_media": valid,
        "duration_seconds": round(duration, 3),
        "width": primary.get("width"),
        "height": primary.get("height"),
        "video_codec": primary.get("codec_name"),
        "stream_count": len(info.get("streams", [])),
        "chapter_count": len(info.get("chapters", [])),
    }


def probe_media(path: Path) -> dict:
    media = path.resolve(strict=True)
    before = snapshot(media)
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", SHOW_ENTRIES,
                "-of", "json", str(media),
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(str(exc)) from exc
    if completed.returncode != 0:
        raise ProbeError(completed.stderr.strip()[:500] or "ffprobe failed")
    if len(completed.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ProbeError("ffprobe output exceeds 5 MiB")
    try:
        info = clean_probe(json.loads(completed.stdout))
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe returned invalid JSON") from exc
    after = snapshot(media)
    if before != after:
        raise ProbeError("media changed during probe")
    report = {
        "schema": SCHEMA,
        "media": str(media),
        "snapshot": before,
        "ffprobe": info,
        "summary": summary(info),
    }
    if not report["summary"]["valid_media"]:
        raise ProbeError("ffprobe did not find a valid video stream and positive duration")
    return report


def read_probe_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ProbeError("probe JSON must be a regular non-symlink file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(MAX_OUTPUT_BYTES + 1)
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ProbeError("probe JSON exceeds 5 MiB")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProbeError("probe JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ProbeError("probe JSON must contain an object")
    return value


def extract_report(value: dict, media: Path | None = None) -> dict:
    candidates = []
    if value.get("schema") == SCHEMA:
        candidates.append(value)
    for key in ("probe", "media_probe"):
        item = value.get(key)
        if isinstance(item, dict) and item.get("schema") == SCHEMA:
            candidates.append(item)
    cache = value.get("cache")
    if isinstance(cache, dict):
        item = cache.get("media_probe")
        if isinstance(item, dict) and item.get("schema") == SCHEMA:
            candidates.append(item)
    for selected in value.get("selected") or []:
        if isinstance(selected, dict):
            item = selected.get("probe")
            if isinstance(item, dict) and item.get("schema") == SCHEMA:
                candidates.append(item)
    if media is None and len(candidates) == 1:
        return candidates[0]
    expected = media.resolve(strict=True) if media is not None else None
    for candidate in candidates:
        try:
            candidate_path = Path(str(candidate.get("media") or "")).resolve(strict=True)
        except (OSError, ValueError):
            continue
        if expected is None or candidate_path == expected:
            return candidate
    raise ProbeError("no matching media probe was found")


def load_report(path: Path, media: Path | None = None) -> dict:
    report = extract_report(read_probe_json(path), media)
    if media is not None and report.get("snapshot") != snapshot(media.resolve(strict=True)):
        raise ProbeError("saved media probe is stale")
    return report


def ffprobe_data(report: dict) -> dict:
    value = report.get("ffprobe")
    if report.get("schema") != SCHEMA or not isinstance(value, dict):
        raise ProbeError("invalid media probe schema")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(probe_media(args.media), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ProbeError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
