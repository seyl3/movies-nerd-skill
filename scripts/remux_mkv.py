#!/usr/bin/env python3
"""Verified stream-copy remux into the preferred MKV container."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

STAGING_ROOTS = (
    Path("/Volumes/ssd/Films/.incoming/Movies Nerd"),
    Path("/Volumes/ssd/Se\u0301ries/.incoming/Movies Nerd"),
)
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
    for root in STAGING_ROOTS:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            pass
    return False


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, capture_output=True)


def probe(path: Path) -> dict:
    result = run(["ffprobe", "-v", "error", "-show_streams", "-show_chapters", "-of", "json", str(path)])
    return json.loads(result.stdout)


def streamhash(path: Path) -> list[str]:
    result = run([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-map", "0:a",
        "-map", "0:s?", "-map", "0:t?", "-c", "copy", "-f", "streamhash",
        "-hash", "sha256", "-",
    ])
    return result.stdout.strip().splitlines()


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
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
    info = probe(source)
    streams = info.get("streams", [])
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    unknown_audio = [stream["index"] for stream in audio if language(stream, audio_map) == "und"]
    unknown_subs = [stream["index"] for stream in subtitles if language(stream, subtitle_map) == "und"]
    if unknown_audio or unknown_subs:
        raise SystemExit(
            "error: supply explicit language overrides for untagged streams: "
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
        raise SystemExit(f"error: temporary output already exists: {temp}")
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

    before_hash = streamhash(source)
    try:
        run(command)
        after_hash = streamhash(temp)
        after = probe(temp)
        before_types = [s.get("codec_type") for s in streams if s.get("codec_type") in {"video", "audio", "subtitle", "attachment"}]
        after_types = [s.get("codec_type") for s in after.get("streams", []) if s.get("codec_type") in {"video", "audio", "subtitle", "attachment"}]
        if before_hash != after_hash:
            raise RuntimeError("material packet hash mismatch")
        if before_types != after_types:
            raise RuntimeError("material stream layout mismatch")
        if len(info.get("chapters", [])) != len(after.get("chapters", [])):
            raise RuntimeError("chapter count mismatch")
        os.replace(temp, target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    print(json.dumps({
        "input": str(source), "output": str(target), "container": "matroska",
        "reencoded": False, "packet_hashes_verified": True,
        "chapters_verified": True, "source_preserved": True,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
