#!/usr/bin/env python3
"""Bounded, no-key Torrentio discovery through the Stremio add-on protocol."""

from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from qbittorrent_api import QbtError, magnet_hash, safe_magnet

BASE_URL = "https://torrentio.strem.fun"
ALLOWED_HOST = "torrentio.strem.fun"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_STREAMS = 60
IMDB_RE = re.compile(r"tt[0-9]{7,10}")
INFO_HASH_RE = re.compile(r"[0-9a-fA-F]{40}")
SIZE_RE = re.compile(
    r"(?:💾|\bsize\b)\s*[:=-]?\s*([0-9]+(?:[.,][0-9]+)?)\s*"
    r"(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)\b",
    re.IGNORECASE,
)
SEEDERS_RE = re.compile(r"(?:👤|\bseed(?:er)?s?\b)\s*[:=-]?\s*([0-9]+)", re.IGNORECASE)
SOURCE_RE = re.compile(r"⚙️?\s*([^\r\n]+)")
UNSAFE_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
CONFIG = "sort=qualitysize|qualityfilter=scr,cam|limit=3"


class TorrentioError(RuntimeError):
    pass


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TorrentioError("Torrentio redirected outside its fixed endpoint")


def _checked_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_HOST
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise TorrentioError("Torrentio URL is outside its fixed HTTPS endpoint")
    return url


def stream_url(
    imdb_id: str, *, series: bool = False,
    season: int | None = None, episode: int | None = None,
) -> str:
    identity = str(imdb_id or "").strip().lower()
    if not IMDB_RE.fullmatch(identity):
        raise TorrentioError("Torrentio requires an exact IMDb ID")
    media_type = "series" if series else "movie"
    if series:
        if (
            isinstance(season, bool) or isinstance(episode, bool)
            or not isinstance(season, int) or not isinstance(episode, int)
            or not 0 <= season <= 999 or not 1 <= episode <= 9999
        ):
            raise TorrentioError("Torrentio series lookup requires an exact season and episode")
        identity = f"{identity}:{season}:{episode}"
    config = quote(CONFIG, safe="=,|")
    return _checked_url(f"{BASE_URL}/{config}/stream/{media_type}/{identity}.json")


def _system_curl(url: str, timeout: float) -> bytes:
    curl = shutil.which("curl")
    if not curl:
        raise TorrentioError("Torrentio is temporarily unavailable")
    try:
        completed = subprocess.run(
            [
                curl, "--fail", "--silent", "--show-error", "--proto", "=https",
                "--max-time", str(timeout), "--max-filesize", str(MAX_RESPONSE_BYTES),
                "--header", "Accept: application/json",
                "--header", "User-Agent: Movies-Nerd/2",
                url,
            ],
            check=False, capture_output=True, timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TorrentioError("Torrentio is temporarily unavailable") from exc
    if completed.returncode != 0:
        raise TorrentioError("Torrentio is temporarily unavailable")
    return completed.stdout


def fetch_json(url: str, timeout: float) -> object:
    _checked_url(url)
    request = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Movies-Nerd/2",
    })
    try:
        with build_opener(RejectRedirects()).open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise TorrentioError("Torrentio changed its fixed endpoint")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise TorrentioError(f"Torrentio returned HTTP {exc.code}") from exc
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raw = _system_curl(url, timeout)
        else:
            raise TorrentioError("Torrentio is temporarily unavailable") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TorrentioError("Torrentio response exceeds 1 MiB")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TorrentioError("Torrentio returned invalid JSON") from exc


def _clean(value: object, limit: int) -> str:
    text = str(value or "")
    if UNSAFE_TEXT_RE.search(text):
        return ""
    return " ".join(text.split())[:limit].strip()


def _size_bytes(text: str) -> int:
    match = SIZE_RE.search(text)
    if not match:
        return 0
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2).upper()
    powers = {
        "B": 0, "KB": 1, "KIB": 1, "MB": 2, "MIB": 2,
        "GB": 3, "GIB": 3, "TB": 4, "TIB": 4,
    }
    return int(value * (1024 ** powers[unit]))


def normalize_streams(
    payload: object, *, title: str, year: int | None, max_bytes: int,
) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise TorrentioError("Torrentio returned an unexpected response")
    canonical = _clean(title, 250)
    if not canonical:
        raise TorrentioError("Torrentio normalization requires a title")
    results = []
    for stream in payload["streams"][:MAX_STREAMS]:
        if not isinstance(stream, dict):
            continue
        # Debrid streams contain playable URLs. Movies Nerd accepts only plain
        # BitTorrent identities and reconstructs its own allowlisted magnet.
        if stream.get("url") or stream.get("externalUrl"):
            continue
        info_hash = str(stream.get("infoHash") or "").strip().lower()
        if not INFO_HASH_RE.fullmatch(info_hash):
            continue
        raw_title = _clean(stream.get("title") or stream.get("name"), 500)
        size = _size_bytes(str(stream.get("title") or ""))
        if not raw_title or size <= 0 or size > max_bytes:
            continue
        seed_match = SEEDERS_RE.search(str(stream.get("title") or ""))
        source_match = SOURCE_RE.search(str(stream.get("title") or ""))
        source = _clean(source_match.group(1) if source_match else "Torrentio", 100)
        try:
            file_index = stream.get("fileIdx")
            if isinstance(file_index, bool):
                raise ValueError
            file_index = int(file_index) if file_index is not None else None
            if file_index is not None and not 0 <= file_index <= 100_000:
                raise ValueError
        except (TypeError, ValueError):
            continue
        display_title = f"{canonical} ({year}) {raw_title}" if year else f"{canonical} {raw_title}"
        try:
            magnet = safe_magnet(info_hash, display_title[:300])
        except QbtError:
            continue
        results.append({
            "title": display_title,
            "size": size,
            "seeders": int(seed_match.group(1)) if seed_match else 0,
            "leechers": 0,
            "source": source or "Torrentio",
            "provider": "Torrentio",
            "magnet": magnet,
            "info_hash": magnet_hash(magnet),
            "file_index": file_index,
        })
    return results


def search_torrentio(
    imdb_id: str, timeout: float, *, title: str, year: int | None,
    max_bytes: int, series: bool = False,
    season: int | None = None, episode: int | None = None,
) -> list[dict]:
    if not 0 < timeout <= 30:
        raise TorrentioError("Torrentio timeout is outside its safety bound")
    url = stream_url(imdb_id, series=series, season=season, episode=episode)
    return normalize_streams(fetch_json(url, timeout), title=title, year=year, max_bytes=max_bytes)
