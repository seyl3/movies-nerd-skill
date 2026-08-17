from __future__ import annotations

import hashlib
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common
import search_releases


def bencode(value) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            bencode(key) + bencode(value[key]) for key in sorted(value)
        ) + b"e"
    raise TypeError(type(value))

def torrent_fixture(path: list[bytes] | None = None) -> tuple[bytes, str]:
    if path is None:
        info = {
            b"length": 2_000_000_000,
            b"name": b"Example.mkv",
            b"piece length": 262144,
            b"pieces": b"x" * 20,
        }
    else:
        info = {
            b"files": [{b"length": 2_000_000_000, b"path": path}],
            b"name": b"Example",
            b"piece length": 262144,
            b"pieces": b"x" * 20,
        }
    raw_info = bencode(info)
    raw = bencode({b"announce": b"udp://tracker.example/announce", b"info": info})
    return raw, hashlib.sha1(raw_info).hexdigest()

def roots(base: Path) -> dict[str, str]:
    return {
        _common.MOVIES_ROOT_ENV: str(base / "Films"),
        _common.SERIES_ROOT_ENV: str(base / "Series"),
    }

def candidate(info_hash: str, source: str = "source", score: float = 100) -> dict:
    return {
        "title": "Example (2024) 1080p x265",
        "source": source,
        "provider": source,
        "size_bytes": 2_000_000_000,
        "size": "1.86 GiB",
        "resolution": "1080p",
        "seeders": 0,
        "leechers": 0,
        "score": score,
        "warnings": [],
        "magnet": search_releases.minimal_magnet(info_hash, "Example"),
        "info_hash": info_hash,
    }
