#!/usr/bin/env python3
"""Verified stream-copy remux into the preferred MKV container."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from _common import staging_roots
from media_probe import ffprobe_data, load_report, probe_media
LANG_NAMES = {
    "eng": "English", "fre": "French", "ita": "Italian", "spa": "Spanish",
    "ger": "German", "jpn": "Japanese", "kor": "Korean", "ara": "Arabic",
    "por": "Portuguese", "mul": "Multiple Languages",
}
LANG_NORMALIZE = {
    "en": "eng", "eng": "eng", "fr": "fre", "fra": "fre", "fre": "fre",
    "it": "ita", "ita": "ita", "es": "spa", "spa": "spa", "de": "ger",
    "deu": "ger", "ger": "ger", "ja": "jpn", "jpn": "jpn", "ko": "kor",
    "kor": "kor", "ar": "ara", "ara": "ara", "pt": "por", "por": "por",
    "mul": "mul",
}


def in_staging(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    for root in staging_roots():
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            pass
    return False


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, capture_output=True)


def probe(path: Path) -> dict:
    return ffprobe_data(probe_media(path))


def stream_signature(info: dict) -> list[tuple]:
    signatures = []
    for stream in info.get("streams", []):
        kind = stream.get("codec_type")
        if kind not in {"video", "audio", "subtitle", "attachment"}:
            continue
        signatures.append((
            kind,
            stream.get("codec_name"),
            stream.get("width"),
            stream.get("height"),
            stream.get("sample_rate"),
            stream.get("channels"),
        ))
    return signatures


def duration(info: dict) -> float | None:
    raw = (info.get("format") or {}).get("duration")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def overrides(values: list[str]) -> dict[int, str]:
    parsed = {}
    for value in values:
        try:
            raw_index, raw_lang = value.split("=", 1)
            index = int(raw_index)
        except ValueError as exc:
            raise ValueError(f"invalid INDEX=LANG override: {value}") from exc
        lang = LANG_NORMALIZE.get(raw_lang.lower())
        if not lang:
            raise ValueError(f"unsupported language override: {raw_lang}")
        parsed[index] = lang
    return parsed


def language(stream: dict, mapping: dict[int, str]) -> str:
    index = int(stream["index"])
    if index in mapping:
        return mapping[index]
    raw = str((stream.get("tags") or {}).get("language") or "und").lower()
    return LANG_NORMALIZE.get(raw, "und")


def remux(
    source: Path, target: Path, info: dict,
    audio_map: dict[int, str] | None = None,
    subtitle_map: dict[int, str] | None = None,
    allow_unknown: bool = False,
) -> dict:
    """Stream-copy one already-probed staged file and verify the result."""
    source = source.resolve(strict=True)
    target = target.resolve(strict=False)
    audio_map = dict(audio_map or {})
    subtitle_map = dict(subtitle_map or {})
    if not source.is_file() or source.is_symlink():
        raise ValueError("input is not a regular file")
    if not in_staging(source) or not in_staging(target):
        raise ValueError("input and output must remain inside a Movies Nerd staging root")
    if target.suffix.lower() != ".mkv":
        raise ValueError("output must use the .mkv extension")
    if target.exists() or target.is_symlink():
        raise ValueError("output already exists; refusing to overwrite")
    streams = info.get("streams", [])
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    unknown_audio = [stream["index"] for stream in audio if language(stream, audio_map) == "und"]
    unknown_subs = [stream["index"] for stream in subtitles if language(stream, subtitle_map) == "und"]
    if (unknown_audio or unknown_subs) and not allow_unknown:
        raise ValueError(
            "supply explicit language overrides for untagged streams: "
            f"audio={unknown_audio}, subtitles={unknown_subs}"
        )

    default_audio = 0
    for i, stream in enumerate(audio):
        if (stream.get("disposition") or {}).get("default"):
            default_audio = i
            break

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.stem + ".remuxing.mkv")
    if temp.exists():
        raise ValueError(f"temporary output already exists: {temp}")
    command = [
        "ffmpeg", "-y", "-v", "error", "-i", str(source),
        "-map", "0:v", "-map", "0:a", "-map", "0:s?", "-map", "0:t?",
        "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
    ]
    for i, stream in enumerate(audio):
        lang = language(stream, audio_map)
        old = str((stream.get("tags") or {}).get("title") or "")
        label = LANG_NAMES.get(lang, lang)
        if "commentary" in old.lower():
            label += " Commentary"
        command += [f"-metadata:s:a:{i}", f"language={lang}"]
        command += [f"-metadata:s:a:{i}", f"title={label}"]
        command += [f"-disposition:a:{i}", "default" if i == default_audio else "0"]
    for i, stream in enumerate(subtitles):
        lang = language(stream, subtitle_map)
        old = str((stream.get("tags") or {}).get("title") or "")
        disposition = stream.get("disposition") or {}
        sdh = "sdh" in old.lower() or "hearing" in old.lower() or disposition.get("hearing_impaired")
        label = LANG_NAMES.get(lang, lang) + (" (SDH)" if sdh else "")
        flags = []
        if disposition.get("forced"):
            flags.append("forced")
        if sdh:
            flags.append("hearing_impaired")
        command += [f"-metadata:s:s:{i}", f"language={lang}"]
        command += [f"-metadata:s:s:{i}", f"title={label}"]
        command += [f"-disposition:s:{i}", "+".join(flags) if flags else "0"]
    command.append(str(temp))

    try:
        run(command)
        after = probe(temp)
        if stream_signature(info) != stream_signature(after):
            raise RuntimeError("material stream layout or codec mismatch")
        before_duration = duration(info)
        after_duration = duration(after)
        if before_duration is None or after_duration is None:
            raise RuntimeError("material duration unavailable for verification")
        tolerance = max(1.0, before_duration * 0.001)
        if abs(before_duration - after_duration) > tolerance:
            raise RuntimeError("material duration mismatch")
        if len(info.get("chapters", [])) != len(after.get("chapters", [])):
            raise RuntimeError("chapter count mismatch")
        os.replace(temp, target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return {
        "input": str(source), "output": str(target), "container": "matroska",
        "reencoded": False, "streams_verified": True,
        "duration_verified": True, "chapters_verified": True,
        "source_preserved": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--probe-json", type=Path, help="saved full probe or Gate 2 JSON")
    parser.add_argument("--audio-language", action="append", default=[], metavar="INDEX=LANG")
    parser.add_argument("--subtitle-language", action="append", default=[], metavar="INDEX=LANG")
    args = parser.parse_args()
    source = args.input.resolve(strict=True)
    target = args.output.resolve(strict=False)
    if not source.is_file():
        parser.error("input is not a regular file")
    if not in_staging(source) or not in_staging(target):
        parser.error("input and output must remain inside a Movies Nerd staging root")
    if target.suffix.lower() != ".mkv":
        parser.error("output must use the .mkv extension")
    if target.exists():
        parser.error("output already exists; refusing to overwrite")

    audio_map = overrides(args.audio_language)
    subtitle_map = overrides(args.subtitle_language)
    info = ffprobe_data(load_report(args.probe_json, source)) if args.probe_json else probe(source)
    print(json.dumps(remux(source, target, info, audio_map, subtitle_map), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
