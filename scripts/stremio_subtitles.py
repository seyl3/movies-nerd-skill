#!/usr/bin/env python3
"""Search and stage English/French SRTs through Stremio's no-key subtitle service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from opensubtitles_api import SubtitleApiError, checked_destination
from validate_subtitle import MAX_BYTES, media_duration, validate_bytes

API_ORIGIN = "https://opensubtitles-v3.strem.io"
MAX_JSON = 5 * 1024 * 1024
USER_AGENT = "MoviesNerdSkill no-key-subtitles"
LANGUAGE_CODES = {"en": "eng", "fr": "fre"}
DOWNLOAD_HOST = re.compile(r"subs\d*\.strem\.io", re.IGNORECASE)
DOWNLOAD_PATH = re.compile(
    r"/en/download/subencoding-stremio-utf8/src-api/file/\d+"
)


class StremioSubtitleError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StremioSubtitleError(f"unexpected subtitle service redirect ({code})")


class ApprovedDownloadRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def imdb_id(value: str) -> str:
    if not re.fullmatch(r"tt\d{5,12}", value.strip()):
        raise argparse.ArgumentTypeError("IMDb ID must look like tt1234567")
    return value.strip()


def subtitle_id(value: str) -> str:
    if not re.fullmatch(r"\d{1,20}", value.strip()):
        raise argparse.ArgumentTypeError("invalid subtitle ID")
    return value.strip()


def bounded_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 50:
        raise argparse.ArgumentTypeError("limit must be between 1 and 50")
    return parsed


def language_list(value: str) -> list[str]:
    items = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not items or any(item not in LANGUAGE_CODES for item in items):
        raise argparse.ArgumentTypeError("languages must be en, fr, or en,fr")
    return list(dict.fromkeys(items))


def content_id(kind: str, imdb: str, season: int | None, episode: int | None) -> str:
    checked = imdb_id(imdb)
    if kind == "movie":
        if season is not None or episode is not None:
            raise StremioSubtitleError("season and episode apply only to series")
        return checked
    if season is None or episode is None or season < 0 or episode < 0:
        raise StremioSubtitleError("series searches require non-negative season and episode numbers")
    return f"{checked}:{season}:{episode}"


def service_json(kind: str, identifier: str) -> dict:
    if kind not in {"movie", "series"} or not re.fullmatch(
        r"tt\d{5,12}(?::\d{1,4}:\d{1,4})?", identifier,
    ):
        raise StremioSubtitleError("invalid subtitle service request")
    url = f"{API_ORIGIN}/subtitles/{kind}/{identifier}.json"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(MAX_JSON + 1)
    except HTTPError as exc:
        raise StremioSubtitleError(f"subtitle search failed with HTTP {exc.code}") from exc
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raw = system_curl(url, MAX_JSON, 20, "application/json")
        else:
            raise StremioSubtitleError(f"cannot reach the subtitle service: {exc.reason}") from exc
    if len(raw) > MAX_JSON:
        raise StremioSubtitleError("subtitle search response exceeds 5 MiB")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StremioSubtitleError("subtitle service returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("subtitles"), list):
        raise StremioSubtitleError("subtitle service returned an unexpected response")
    return result


def checked_download_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise StremioSubtitleError("subtitle service returned an unapproved download URL") from exc
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or not DOWNLOAD_HOST.fullmatch(host)
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not DOWNLOAD_PATH.fullmatch(parsed.path)
        or parsed.fragment
        or any(name != "senc" or len(value) > 40 for name, value in query)
    ):
        raise StremioSubtitleError("subtitle service returned an unapproved download URL")
    return value


def candidates(result: dict, languages: list[str], limit: int) -> list[dict]:
    wanted = {LANGUAGE_CODES[item]: item for item in languages}
    counts = {item: 0 for item in languages}
    found: list[dict] = []
    for position, item in enumerate(result.get("subtitles", []), start=1):
        if not isinstance(item, dict):
            continue
        language = wanted.get(item.get("lang"))
        identifier = str(item.get("id", ""))
        if language is None or counts[language] >= limit or not re.fullmatch(r"\d{1,20}", identifier):
            continue
        url = item.get("url")
        if not isinstance(url, str):
            continue
        try:
            checked_download_url(url)
        except StremioSubtitleError:
            continue
        found.append({
            "subtitle_id": identifier,
            "language": language,
            "provider_rank": position,
            "encoding": item.get("SubEncoding"),
        })
        counts[language] += 1
    return found


def search(args: argparse.Namespace) -> dict:
    identifier = content_id(args.kind, args.imdb_id, args.season, args.episode)
    found = candidates(service_json(args.kind, identifier), args.languages, args.limit)
    return {
        "provider": "OpenSubtitles v3 for Stremio",
        "requires_api_key": False,
        "content_id": identifier,
        "count": len(found),
        "candidates": found,
    }


def fetch_srt(url: str) -> bytes:
    request = Request(
        checked_download_url(url),
        headers={"User-Agent": USER_AGENT, "Accept": "application/x-subrip,text/plain"},
    )
    try:
        with build_opener(ApprovedDownloadRedirect()).open(request, timeout=30) as response:
            data = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise StremioSubtitleError(f"subtitle download failed with HTTP {exc.code}") from exc
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            data = system_curl(
                checked_download_url(url), MAX_BYTES, 30,
                "application/x-subrip,text/plain",
            )
        else:
            raise StremioSubtitleError(f"subtitle download failed: {exc.reason}") from exc
    if len(data) > MAX_BYTES:
        raise StremioSubtitleError("subtitle download exceeds 5 MiB")
    return data


def system_curl(url: str, maximum: int, timeout: int, accept: str) -> bytes:
    """Use the operating system trust store without weakening TLS checks."""
    curl = shutil.which("curl")
    if not curl:
        raise StremioSubtitleError(
            "Python TLS trust failed and the operating system HTTPS client is unavailable"
        )
    command = [
        curl, "--fail", "--silent", "--show-error", "--proto", "=https",
        "--max-redirs", "0", "--max-time", str(timeout),
        "--max-filesize", str(maximum), "--header", f"Accept: {accept}",
        "--header", f"User-Agent: {USER_AGENT}", url,
    ]
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StremioSubtitleError("operating system HTTPS request failed") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:200]
        raise StremioSubtitleError(
            f"operating system HTTPS request failed: {detail or 'request error'}"
        )
    if len(completed.stdout) > maximum:
        raise StremioSubtitleError("subtitle service response exceeds its size limit")
    return completed.stdout


def download(args: argparse.Namespace) -> dict:
    if not args.commit:
        raise StremioSubtitleError("refusing to download without --commit")
    destination = checked_destination(args.output, args.language)
    identifier = content_id(args.kind, args.imdb_id, args.season, args.episode)
    result = service_json(args.kind, identifier)
    selected = None
    wanted = LANGUAGE_CODES[args.language]
    for item in result.get("subtitles", []):
        if isinstance(item, dict) and str(item.get("id", "")) == args.subtitle_id and item.get("lang") == wanted:
            selected = item
            break
    if selected is None or not isinstance(selected.get("url"), str):
        raise StremioSubtitleError("selected subtitle ID is unavailable for this title and language")
    data = fetch_srt(selected["url"])
    duration = media_duration(Path(args.media).resolve(strict=True)) if args.media else None
    validation = validate_bytes(data, args.language, duration, args.forced)
    if not validation["valid"]:
        raise StremioSubtitleError(
            "downloaded SRT failed validation: " + "; ".join(validation["reasons"])
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return {
        "provider": "OpenSubtitles v3 for Stremio",
        "written": str(destination),
        "subtitle_id": args.subtitle_id,
        "language": args.language,
        "validation": validation,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    find = sub.add_parser("search")
    find.add_argument("--imdb-id", type=imdb_id, required=True)
    find.add_argument("--kind", choices=("movie", "series"), default="movie")
    find.add_argument("--season", type=int)
    find.add_argument("--episode", type=int)
    find.add_argument("--languages", type=language_list, default=["en", "fr"])
    find.add_argument("--limit", type=bounded_limit, default=10, help="maximum results per language (1-50)")
    get = sub.add_parser("download")
    get.add_argument("--imdb-id", type=imdb_id, required=True)
    get.add_argument("--kind", choices=("movie", "series"), default="movie")
    get.add_argument("--season", type=int)
    get.add_argument("--episode", type=int)
    get.add_argument("--subtitle-id", type=subtitle_id, required=True)
    get.add_argument("--language", choices=("en", "fr"), required=True)
    get.add_argument("--output", required=True)
    get.add_argument("--media")
    get.add_argument("--forced", action="store_true")
    get.add_argument("--commit", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        output = search(args) if args.command == "search" else download(args)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, SubtitleApiError, StremioSubtitleError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
