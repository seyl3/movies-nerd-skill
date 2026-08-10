#!/usr/bin/env python3
"""Dependency-free filename and content checks for untrusted media payloads."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import os
import re
import stat

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".webm", ".ts", ".m2ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
TEXT_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".idx", ".nfo", ".txt"}
ALLOWED_COMPANIONS = IMAGE_EXTENSIONS | TEXT_EXTENSIONS | {".sub"}
SAFE_EXTENSIONS = VIDEO_EXTENSIONS | ALLOWED_COMPANIONS
DANGEROUS_EXTENSIONS = {
    ".app", ".bat", ".bin", ".bz2", ".cmd", ".com", ".dll", ".dmg", ".exe",
    ".gz", ".hta", ".img", ".iso", ".jar", ".js", ".lnk", ".msi", ".pkg",
    ".ps1", ".py", ".rar", ".reg", ".scr", ".sh", ".tar", ".url", ".vbs",
    ".website", ".xz", ".zip", ".7z",
}
MAX_PAYLOAD_FILES = 5_000
MAX_PATH_BYTES = 1_024
MAX_COMPONENT_BYTES = 255
MAX_TEXT_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
BIDI_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
WINDOWS_RESERVED_RE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.I)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def filename_reasons(name: str) -> list[str]:
    """Return reasons a torrent payload filename is unsafe."""
    reasons: list[str] = []
    normalized = name.replace("\\", "/")
    raw_parts = normalized.split("/")
    posix = PurePosixPath(normalized)
    if not name or CONTROL_RE.search(name):
        reasons.append("empty name or control character")
    if BIDI_RE.search(name):
        reasons.append("bidirectional filename spoofing character")
    if len(name.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        reasons.append("path exceeds safety limit")
    if posix.is_absolute() or not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        reasons.append("absolute, empty, or traversing path component")
    for part in raw_parts:
        if len(part.encode("utf-8", "surrogatepass")) > MAX_COMPONENT_BYTES:
            reasons.append("path component exceeds safety limit")
        if part.endswith((" ", ".")):
            reasons.append("path component has a deceptive trailing character")
        if part.startswith("."):
            reasons.append("hidden payload path")
        if ":" in part or WINDOWS_RESERVED_RE.fullmatch(part):
            reasons.append("platform-sensitive path component")
        suffixes = [suffix.lower() for suffix in PurePosixPath(part).suffixes]
        if any(suffix in DANGEROUS_EXTENSIONS for suffix in suffixes):
            reasons.append("dangerous or archive extension, including inner extension")
    suffix = posix.suffix.lower()
    if suffix not in SAFE_EXTENSIONS:
        reasons.append("unexpected extension")
    return _unique(reasons)


def directory_reasons(name: str) -> list[str]:
    return [reason for reason in filename_reasons(name) if reason != "unexpected extension"]


def _dangerous_signature(head: bytes, tail: bytes) -> str | None:
    signatures = (
        ((b"MZ",), "Windows executable"),
        ((b"\x7fELF",), "ELF executable"),
        ((b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"), "Mach-O executable"),
        ((b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"), "Mach-O universal binary or Java class"),
        ((b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",), "OLE document or installer"),
        ((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"), "ZIP or application bundle"),
        ((b"Rar!\x1a\x07",), "RAR archive"),
        ((b"7z\xbc\xaf\x27\x1c",), "7z archive"),
        ((b"\x1f\x8b",), "gzip archive"),
        ((b"BZh",), "bzip2 archive"),
        ((b"\xfd7zXZ\x00",), "xz archive"),
        ((b"\x4c\x00\x00\x00\x01\x14\x02\x00",), "Windows shortcut"),
        ((b"\x00\x05\x16\x07",), "AppleDouble metadata"),
    )
    for prefixes, label in signatures:
        if any(head.startswith(prefix) for prefix in prefixes):
            return label
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar archive"
    if any(len(head) >= offset + 5 and head[offset:offset + 5] == b"CD001" for offset in (0x8001, 0x8801, 0x9001)):
        return "ISO disk image"
    if tail.startswith(b"koly"):
        return "Apple disk image"
    prefix = head[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if prefix.startswith(b"#!"):
        return "script shebang"
    if prefix.startswith((b"<!doctype html", b"<html", b"<?xml-stylesheet")) or b"<script" in prefix:
        return "active HTML or script content"
    return None


def content_reasons(path: Path, suffix: str) -> list[str]:
    """Inspect a regular file without executing, extracting, or hashing it."""
    reasons: list[str] = []
    listed = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(listed.st_mode):
        return ["not a regular file"]
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (listed.st_dev, listed.st_ino) != (before.st_dev, before.st_ino):
            return ["file changed before safety inspection"]
        if before.st_size <= 0:
            reasons.append("empty file")
        head = handle.read(64 * 1024)
        if before.st_size > 512:
            handle.seek(-512, 2)
        else:
            handle.seek(0)
        tail = handle.read(512)
        after = os.fstat(handle.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        return ["file changed during safety inspection"]

    signature = _dangerous_signature(head, tail)
    if signature:
        reasons.append(f"dangerous content signature: {signature}")
    if suffix in IMAGE_EXTENSIONS:
        valid_image = (
            (suffix in {".jpg", ".jpeg"} and head.startswith(b"\xff\xd8\xff"))
            or (suffix == ".png" and head.startswith(b"\x89PNG\r\n\x1a\n"))
            or (suffix == ".webp" and head.startswith(b"RIFF") and head[8:12] == b"WEBP")
        )
        if not valid_image:
            reasons.append("image signature does not match extension")
        if before.st_size > MAX_IMAGE_BYTES:
            reasons.append("image exceeds 64 MiB safety limit")
    elif suffix in TEXT_EXTENSIONS:
        if before.st_size > MAX_TEXT_BYTES:
            reasons.append("text companion exceeds 10 MiB safety limit")
        if b"\x00" in head:
            reasons.append("text companion contains binary NUL bytes")
    return _unique(reasons)
