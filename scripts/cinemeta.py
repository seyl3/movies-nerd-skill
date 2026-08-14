#!/usr/bin/env python3
"""Fetch exact no-key metadata and artwork from Stremio's official Cinemeta addon."""

from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

META_ORIGIN = "https://v3-cinemeta.strem.io"
IMAGE_ORIGIN = "https://images.metahub.space"
META_HOST = "v3-cinemeta.strem.io"
IMAGE_HOSTS = {"images.metahub.space", "live.metahub.space"}
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024
USER_AGENT = "MoviesNerdSkill metadata-preparer"
IMDB_RE = re.compile(r"tt\d{5,12}")


class CinemetaError(RuntimeError):
    pass


def normalized_title(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def release_year(value: object) -> int | None:
    match = re.search(r"(?:18|19|20|21)\d{2}", str(value or ""))
    return int(match.group()) if match else None


def checked_url(url: str, *, image: bool = False) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = IMAGE_HOSTS if image else {META_HOST}
    try:
        port = parsed.port
    except ValueError as exc:
        raise CinemetaError("metadata service URL is invalid") from exc
    if (
        parsed.scheme != "https" or host not in allowed or port not in {None, 443}
        or parsed.username is not None or parsed.password is not None or parsed.fragment
    ):
        raise CinemetaError("metadata service URL is outside the fixed HTTPS allowlist")
    if image:
        if not re.fullmatch(
            r"/(?:poster|background|logo)/original/tt\d{5,12}/img", parsed.path,
        ):
            raise CinemetaError("artwork URL has an unsupported path")
    elif not (
        re.fullmatch(r"/meta/(?:movie|series)/tt\d{5,12}\.json", parsed.path)
        or parsed.path.startswith("/catalog/movie/top/search=")
        or parsed.path.startswith("/catalog/series/top/search=")
    ):
        raise CinemetaError("metadata URL has an unsupported path")
    return url


class FixedRedirects(HTTPRedirectHandler):
    def __init__(self, *, image: bool):
        super().__init__()
        self.image = image

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl, image=self.image)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def system_curl(url: str, maximum: int, accept: str) -> bytes:
    """Use the operating-system trust store without following redirects."""
    curl = shutil.which("curl")
    if not curl:
        raise CinemetaError("the metadata service is unavailable")
    try:
        completed = subprocess.run(
            [
                curl, "--fail", "--silent", "--show-error", "--proto", "=https",
                "--max-redirs", "0", "--max-time", "20", "--max-filesize", str(maximum),
                "--header", f"Accept: {accept}", "--header", f"User-Agent: {USER_AGENT}",
                url,
            ],
            check=False, capture_output=True, timeout=21,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CinemetaError("the metadata service is unavailable") from exc
    if completed.returncode != 0 or len(completed.stdout) > maximum:
        raise CinemetaError("the metadata service request failed")
    return completed.stdout


def fetch_bytes(url: str, maximum: int, accept: str, *, image: bool = False) -> bytes:
    checked_url(url, image=image)
    request = Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
    try:
        with build_opener(FixedRedirects(image=image)).open(request, timeout=20) as response:
            checked_url(response.geturl(), image=image)
            data = response.read(maximum + 1)
    except HTTPError as exc:
        raise CinemetaError(f"metadata service returned HTTP {exc.code}") from exc
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            data = system_curl(url, maximum, accept)
        else:
            raise CinemetaError("the metadata service is unavailable") from exc
    if not data or len(data) > maximum:
        raise CinemetaError("metadata service response is empty or too large")
    return data


def fetch_json(url: str) -> dict:
    raw = fetch_bytes(url, MAX_JSON_BYTES, "application/json")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CinemetaError("metadata service returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise CinemetaError("metadata service returned an unexpected response")
    return value


def resolve_identity(kind: str, title: str, year: int) -> dict:
    if kind not in {"movie", "series"}:
        raise CinemetaError("metadata kind must be movie or series")
    encoded = quote(" ".join(title.split()), safe="")
    result = fetch_json(f"{META_ORIGIN}/catalog/{kind}/top/search={encoded}.json")
    metas = [item for item in result.get("metas", []) if isinstance(item, dict)]
    by_year = [item for item in metas if release_year(item.get("releaseInfo")) == year]
    exact = []
    requested = normalized_title(title)
    for item in by_year:
        aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
        if any(normalized_title(name) == requested for name in [item.get("name"), *aliases]):
            exact.append(item)
    matches = exact or by_year
    if len(matches) != 1:
        raise CinemetaError("the title and year did not resolve to one authoritative IMDb ID")
    identifier = str(matches[0].get("imdb_id") or matches[0].get("id") or "").lower()
    if not IMDB_RE.fullmatch(identifier):
        raise CinemetaError("the metadata service returned an invalid IMDb ID")
    canonical = " ".join(str(matches[0].get("name") or title).split())
    if not canonical:
        raise CinemetaError("the metadata service returned an empty canonical title")
    return {"imdb_id": identifier, "canonical_title": canonical}


def resolve_imdb(kind: str, title: str, year: int) -> str:
    return str(resolve_identity(kind, title, year)["imdb_id"])


def metadata(kind: str, imdb_id: str) -> dict:
    identifier = imdb_id.strip().lower()
    if kind not in {"movie", "series"} or not IMDB_RE.fullmatch(identifier):
        raise CinemetaError("metadata request has an invalid identity")
    value = fetch_json(f"{META_ORIGIN}/meta/{kind}/{identifier}.json")
    meta = value.get("meta")
    if not isinstance(meta, dict) or str(meta.get("id") or meta.get("imdb_id") or "").lower() != identifier:
        raise CinemetaError("metadata response does not match the requested IMDb ID")
    return meta


def artwork(imdb_id: str, kind: str) -> bytes:
    identifier = imdb_id.strip().lower()
    if not IMDB_RE.fullmatch(identifier) or kind not in {"poster", "background", "logo"}:
        raise CinemetaError("artwork request is invalid")
    data = fetch_bytes(
        f"{IMAGE_ORIGIN}/{kind}/original/{identifier}/img",
        MAX_IMAGE_BYTES, "image/*", image=True,
    )
    if not (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    ):
        raise CinemetaError("artwork response is not a supported image")
    return data
