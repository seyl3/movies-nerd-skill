#!/usr/bin/env python3
"""Validate small allowlisted .torrent files before qBittorrent sees them."""

from __future__ import annotations

import hashlib
import re
import shutil
import ssl
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from payload_safety import filename_reasons

MAX_TORRENT_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 32
MAX_ITEMS = 10_000
YTS_TORRENT_HOSTS = {"movies-api.accel.li", "yts.gg", "yts.mx"}
CONTROL_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SKIP_ONLY_REASONS = {
    "hidden payload path",
    "dangerous or archive extension, including inner extension",
    "unexpected extension",
}


class TorrentMetadataError(ValueError):
    pass


def checked_torrent_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.hostname not in YTS_TORRENT_HOSTS
        or parsed.username or parsed.password or parsed.fragment
    ):
        raise TorrentMetadataError("torrent URL is outside the fixed YTS HTTPS allowlist")
    return url


class AllowlistedRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_torrent_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_torrent(url: str, timeout: float = 5.0) -> bytes:
    checked_torrent_url(url)
    request = Request(url, headers={"Accept": "application/x-bittorrent", "User-Agent": "Movies-Nerd/2"})
    try:
        with build_opener(AllowlistedRedirects()).open(request, timeout=timeout) as response:
            checked_torrent_url(response.geturl())
            raw = response.read(MAX_TORRENT_BYTES + 1)
    except HTTPError as exc:
        raise TorrentMetadataError(f"torrent endpoint returned HTTP {exc.code}") from exc
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raw = fetch_with_system_curl(url, timeout)
        else:
            raise TorrentMetadataError("torrent endpoint is unavailable") from exc
    except TimeoutError as exc:
        raise TorrentMetadataError("torrent endpoint is unavailable") from exc
    if len(raw) > MAX_TORRENT_BYTES:
        raise TorrentMetadataError("torrent metadata exceeds 2 MiB")
    inspect_torrent(raw)
    return raw


def fetch_with_system_curl(url: str, timeout: float) -> bytes:
    """Use the OS trust store without weakening TLS or following redirects."""
    curl = shutil.which("curl")
    if not curl:
        raise TorrentMetadataError("Python TLS trust failed and system curl is unavailable")
    checked_torrent_url(url)
    with tempfile.TemporaryDirectory(prefix="movies-nerd-torrent-") as directory:
        destination = f"{directory}/candidate.torrent"
        command = [
            curl, "--fail", "--silent", "--show-error", "--proto", "=https",
            "--max-redirs", "0", "--max-time", str(timeout),
            "--max-filesize", str(MAX_TORRENT_BYTES), "--output", destination,
            "--write-out", "%{http_code}\n%{url_effective}",
            "--header", "Accept: application/x-bittorrent",
            "--header", "User-Agent: Movies-Nerd/2", url,
        ]
        try:
            completed = subprocess.run(
                command, check=False, capture_output=True, timeout=timeout + 1,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TorrentMetadataError("torrent endpoint is unavailable") from exc
        if completed.returncode != 0:
            raise TorrentMetadataError("torrent endpoint is unavailable")
        status, _, effective = completed.stdout.decode("utf-8", "replace").partition("\n")
        if status != "200" or checked_torrent_url(effective.strip()) != effective.strip():
            raise TorrentMetadataError("torrent endpoint returned an unsafe response")
        try:
            with open(destination, "rb") as handle:
                raw = handle.read(MAX_TORRENT_BYTES + 1)
        except OSError as exc:
            raise TorrentMetadataError("torrent endpoint produced no metadata") from exc
    return raw


class Parser:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.index = 0
        self.items = 0
        self.info_span: tuple[int, int] | None = None

    def parse(self, depth: int = 0):
        if depth > MAX_DEPTH:
            raise TorrentMetadataError("torrent metadata is nested too deeply")
        self.items += 1
        if self.items > MAX_ITEMS or self.index >= len(self.raw):
            raise TorrentMetadataError("torrent metadata is truncated or oversized")
        token = self.raw[self.index:self.index + 1]
        if token == b"i":
            return self.integer()
        if token == b"l":
            self.index += 1
            result = []
            while self.peek() != b"e":
                result.append(self.parse(depth + 1))
            self.index += 1
            return result
        if token == b"d":
            self.index += 1
            result = {}
            previous = None
            while self.peek() != b"e":
                key = self.bytestring()
                if previous is not None and key <= previous:
                    raise TorrentMetadataError("torrent dictionary keys are duplicate or unsorted")
                previous = key
                start = self.index
                value = self.parse(depth + 1)
                if depth == 0 and key == b"info":
                    self.info_span = (start, self.index)
                result[key] = value
            self.index += 1
            return result
        if token.isdigit():
            return self.bytestring()
        raise TorrentMetadataError("torrent metadata contains invalid bencoding")

    def peek(self) -> bytes:
        if self.index >= len(self.raw):
            raise TorrentMetadataError("torrent metadata is truncated")
        return self.raw[self.index:self.index + 1]

    def integer(self) -> int:
        end = self.raw.find(b"e", self.index + 1)
        if end < 0:
            raise TorrentMetadataError("torrent integer is truncated")
        text = self.raw[self.index + 1:end]
        if not re.fullmatch(rb"(?:0|-?[1-9][0-9]*)", text) or text == b"-0":
            raise TorrentMetadataError("torrent integer is not canonical")
        self.index = end + 1
        return int(text)

    def bytestring(self) -> bytes:
        colon = self.raw.find(b":", self.index)
        if colon < 0:
            raise TorrentMetadataError("torrent byte string is truncated")
        length_raw = self.raw[self.index:colon]
        if not re.fullmatch(rb"(?:0|[1-9][0-9]*)", length_raw):
            raise TorrentMetadataError("torrent byte string length is invalid")
        length = int(length_raw)
        start = colon + 1
        end = start + length
        if end > len(self.raw):
            raise TorrentMetadataError("torrent byte string exceeds input")
        value = self.raw[start:end]
        self.index = end
        return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise TorrentMetadataError(f"torrent {label} is invalid")
    return value


def inspect_torrent(raw: bytes, expected_hash: str | None = None) -> dict:
    if not raw or len(raw) > MAX_TORRENT_BYTES:
        raise TorrentMetadataError("torrent metadata is empty or oversized")
    parser = Parser(raw)
    value = parser.parse()
    if parser.index != len(raw) or not isinstance(value, dict) or parser.info_span is None:
        raise TorrentMetadataError("torrent metadata has an invalid top-level structure")
    info = value.get(b"info")
    if not isinstance(info, dict):
        raise TorrentMetadataError("torrent info dictionary is missing")
    start, end = parser.info_span
    info_hash = hashlib.sha1(raw[start:end]).hexdigest()
    if expected_hash and info_hash != expected_hash.strip().lower():
        raise TorrentMetadataError("torrent info hash does not match the selected release")
    name = info.get(b"name.utf-8") or info.get(b"name")
    if not isinstance(name, bytes) or not name or len(name) > 1024 or CONTROL_RE.search(name):
        raise TorrentMetadataError("torrent name is invalid")
    root_name = name.decode("utf-8", "replace")
    hard_root_reasons = [reason for reason in filename_reasons(root_name) if reason not in SKIP_ONLY_REASONS]
    if hard_root_reasons:
        raise TorrentMetadataError("torrent root path is unsafe")
    files = info.get(b"files")
    if files is None:
        total = _positive_int(info.get(b"length"), "length")
        file_count = 1
    else:
        if not isinstance(files, list) or not files or len(files) > 5000:
            raise TorrentMetadataError("torrent file list is invalid")
        total = 0
        for item in files:
            if not isinstance(item, dict):
                raise TorrentMetadataError("torrent file record is invalid")
            total += _positive_int(item.get(b"length"), "file length")
            path = item.get(b"path.utf-8") or item.get(b"path")
            if not isinstance(path, list) or not path or any(
                not isinstance(part, bytes) or not part or len(part) > 1024 or CONTROL_RE.search(part)
                for part in path
            ):
                raise TorrentMetadataError("torrent file path is invalid")
            decoded = [part.decode("utf-8", "replace") for part in path]
            full_path = "/".join([root_name, *decoded])
            hard_reasons = [reason for reason in filename_reasons(full_path) if reason not in SKIP_ONLY_REASONS]
            if hard_reasons:
                raise TorrentMetadataError("torrent file path is unsafe")
        file_count = len(files)
    return {
        "info_hash": info_hash,
        "name": root_name[:300],
        "total_size": total,
        "file_count": file_count,
    }
