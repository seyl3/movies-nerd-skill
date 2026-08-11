#!/usr/bin/env python3
"""Normalize MKV track headers in seconds, with clone-backed rollback."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from _common import staging_roots
from media_probe import ffprobe_data, load_report, probe_media
from remux_mkv import LANG_NAMES, duration, language, overrides, stream_signature


def in_staging(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return any(
        _relative_to(resolved, root.resolve(strict=False))
        for root in staging_roots()
    )


def _relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def expected_title(stream: dict, lang: str, subtitle: bool) -> str:
    old = str((stream.get("tags") or {}).get("title") or "")
    disposition = stream.get("disposition") or {}
    if subtitle:
        sdh = (
            "sdh" in old.lower()
            or "hearing" in old.lower()
            or bool(disposition.get("hearing_impaired"))
        )
        return LANG_NAMES.get(lang, lang) + (" (SDH)" if sdh else "")
    return LANG_NAMES.get(lang, lang) + (" Commentary" if "commentary" in old.lower() else "")


def change_plan(
    info: dict, audio_map: dict[int, str], subtitle_map: dict[int, str],
) -> list[dict]:
    streams = info.get("streams", [])
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    unknown_audio = [stream["index"] for stream in audio if language(stream, audio_map) == "und"]
    unknown_subs = [stream["index"] for stream in subtitles if language(stream, subtitle_map) == "und"]
    if unknown_audio or unknown_subs:
        raise ValueError(
            "supply explicit language overrides for untagged streams: "
            f"audio={unknown_audio}, subtitles={unknown_subs}"
        )
    default_audio = 0
    for index, stream in enumerate(audio):
        if (stream.get("disposition") or {}).get("default"):
            default_audio = index
            break

    changes = []
    for index, stream in enumerate(audio, 1):
        lang = language(stream, audio_map)
        tags = stream.get("tags") or {}
        properties = {}
        if str(tags.get("language") or "und").lower() != lang:
            properties["language"] = lang
        title = expected_title(stream, lang, False)
        if str(tags.get("title") or "") != title:
            properties["name"] = title
        wanted_default = 1 if index - 1 == default_audio else 0
        if bool((stream.get("disposition") or {}).get("default")) != bool(wanted_default):
            properties["flag-default"] = wanted_default
        if properties:
            changes.append({"selector": f"track:a{index}", "properties": properties})

    for index, stream in enumerate(subtitles, 1):
        lang = language(stream, subtitle_map)
        tags = stream.get("tags") or {}
        properties = {}
        if str(tags.get("language") or "und").lower() != lang:
            properties["language"] = lang
        title = expected_title(stream, lang, True)
        if str(tags.get("title") or "") != title:
            properties["name"] = title
        if (stream.get("disposition") or {}).get("default"):
            properties["flag-default"] = 0
        if properties:
            changes.append({"selector": f"track:s{index}", "properties": properties})
    return changes


def command_for(tool: str, source: Path, changes: list[dict]) -> list[str]:
    command = [tool, "--abort-on-warnings", str(source)]
    for change in changes:
        command.extend(["--edit", change["selector"]])
        for name, value in change["properties"].items():
            command.extend(["--set", f"{name}={value}"])
    return command


def clone_with_rollback(source: Path, target: Path) -> bool:
    if target.exists():
        raise ValueError(f"rollback clone already exists: {target}")
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        clonefile = libc.clonefile
        clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clonefile.restype = ctypes.c_int
        if clonefile(os.fsencode(source), os.fsencode(target), 0) == 0:
            return True
        target.unlink(missing_ok=True)
        return False
    if sys.platform.startswith("linux"):
        try:
            import fcntl

            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                fcntl.ioctl(target_fd, 0x40049409, source_fd)
                return True
            finally:
                os.close(target_fd)
                os.close(source_fd)
        except OSError:
            target.unlink(missing_ok=True)
            return False
    return False


def verify_material_match(before: dict, after: dict) -> None:
    if stream_signature(before) != stream_signature(after):
        raise RuntimeError("material stream layout or codec mismatch")
    before_duration = duration(before)
    after_duration = duration(after)
    if before_duration is None or after_duration is None:
        raise RuntimeError("material duration unavailable for verification")
    tolerance = max(1.0, before_duration * 0.001)
    if abs(before_duration - after_duration) > tolerance:
        raise RuntimeError("material duration mismatch")
    if len(before.get("chapters", [])) != len(after.get("chapters", [])):
        raise RuntimeError("chapter count mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path)
    parser.add_argument("--probe-json", type=Path, required=True)
    parser.add_argument("--audio-language", action="append", default=[], metavar="INDEX=LANG")
    parser.add_argument("--subtitle-language", action="append", default=[], metavar="INDEX=LANG")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    restored = False
    try:
        if args.media.is_symlink():
            raise ValueError("media must not be a symlink")
        source = args.media.resolve(strict=True)
        if not source.is_file() or source.suffix.lower() != ".mkv":
            raise ValueError("media must be a regular non-symlink MKV")
        if not in_staging(source):
            raise ValueError("media must remain inside a Movies Nerd staging root")
        before_report = load_report(args.probe_json, source)
        before = ffprobe_data(before_report)
        if "matroska" not in str((before.get("format") or {}).get("format_name") or "").lower():
            raise ValueError("saved probe does not describe a Matroska container")
        audio_map = overrides(args.audio_language)
        subtitle_map = overrides(args.subtitle_language)
        changes = change_plan(before, audio_map, subtitle_map)
        tool = shutil.which("mkvpropedit")
        plan = {
            "media": str(source),
            "changes": changes,
            "change_count": len(changes),
            "mkvpropedit_available": bool(tool),
            "action": "none" if not changes else ("edit-headers" if tool else "fallback-to-remux"),
            "committed": False,
        }
        if not changes or not args.commit:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        if not tool:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 3

        backup = source.with_name(f".{source.name}.mkvpropedit-backup")
        if not clone_with_rollback(source, backup):
            plan["action"] = "fallback-to-remux"
            plan["reason"] = "filesystem copy-on-write clone unavailable"
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 3
        try:
            completed = subprocess.run(
                command_for(tool, source, changes),
                check=False,
                text=True,
                capture_output=True,
                timeout=120,
            )
            if completed.returncode != 0:
                raise RuntimeError((completed.stderr or completed.stdout).strip()[:500] or "mkvpropedit failed")
            after_report = probe_media(source)
            after = ffprobe_data(after_report)
            verify_material_match(before, after)
            remaining = change_plan(after, audio_map, subtitle_map)
            if remaining:
                raise RuntimeError("requested header normalization did not verify")
            backup.unlink()
            plan.update({"committed": True, "verified": True, "probe": after_report})
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        except Exception:
            os.replace(backup, source)
            restored = True
            raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc), "source_restored": restored}, indent=2), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
