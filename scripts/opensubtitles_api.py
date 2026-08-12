#!/usr/bin/env python3
"""Minimal fixed-host OpenSubtitles search and staged SRT downloader."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from _common import staging_roots
from validate_subtitle import MAX_BYTES, media_duration, validate_bytes

API_BASE = "https://api.opensubtitles.com/api/v1"
MAX_JSON = 5 * 1024 * 1024
USER_AGENT = "MoviesNerdSkill v2.0"
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SubtitleApiError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SubtitleApiError(f"unexpected API redirect ({code})")


class OpenSubtitlesRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "opensubtitles.com" or host.endswith(".opensubtitles.com")):
            raise SubtitleApiError("download redirect left the approved OpenSubtitles domain")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def api_key(environ: dict[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    key = values.get("OPENSUBTITLES_API_KEY", "").strip()
    if not key or len(key) > 512 or CONTROL_RE.search(key):
        raise SubtitleApiError("OPENSUBTITLES_API_KEY is missing or invalid; use subtitle_provider.py for the no-key fallback")
    return key


def api_json(endpoint: str, key: str, payload: dict | None = None, params: list[tuple[str, str]] | None = None) -> dict:
    if not re.fullmatch(r"/(subtitles|download)", endpoint):
        raise SubtitleApiError("unsupported API endpoint")
    url = API_BASE + endpoint
    if params:
        url += "?" + urlencode(sorted(params))
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Api-Key": key, "User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(MAX_JSON + 1)
    except HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace").strip()
        raise SubtitleApiError(f"OpenSubtitles HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise SubtitleApiError(f"cannot reach OpenSubtitles: {exc.reason}") from exc
    if len(raw) > MAX_JSON:
        raise SubtitleApiError("OpenSubtitles JSON response exceeds 5 MiB")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SubtitleApiError("OpenSubtitles returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise SubtitleApiError("OpenSubtitles returned an unexpected response")
    return result


def search(key: str, args: argparse.Namespace) -> dict:
    params = [("languages", ",".join(args.languages)), ("type", args.kind)]
    if args.imdb_id:
        params.append(("imdb_id", args.imdb_id.removeprefix("tt")))
    if args.query:
        params.append(("query", args.query))
    if args.year:
        params.append(("year", str(args.year)))
    result = api_json("/subtitles", key, params=params)
    candidates = []
    for item in result.get("data", [])[: args.limit]:
        if not isinstance(item, dict):
            continue
        attributes = item.get("attributes") or {}
        feature = attributes.get("feature_details") or {}
        files = attributes.get("files") or []
        for file_info in files[:3]:
            if not isinstance(file_info, dict) or not isinstance(file_info.get("file_id"), int):
                continue
            candidates.append({
                "file_id": file_info["file_id"],
                "file_name": file_info.get("file_name"),
                "language": attributes.get("language"),
                "release": attributes.get("release"),
                "title": feature.get("title"),
                "year": feature.get("year"),
                "feature_type": feature.get("feature_type"),
                "hearing_impaired": attributes.get("hearing_impaired"),
                "foreign_parts_only": attributes.get("foreign_parts_only"),
                "machine_translated": attributes.get("machine_translated"),
                "download_count": attributes.get("download_count"),
                "ratings": attributes.get("ratings"),
            })
    return {"provider": "OpenSubtitles", "count": len(candidates), "candidates": candidates}


def checked_destination(raw: str, language: str) -> Path:
    destination = Path(raw).expanduser().resolve(strict=False)
    if not destination.name.lower().endswith(f".{language}.srt"):
        raise SubtitleApiError(f"output must end in .{language}.srt")
    allowed = tuple(root.resolve(strict=False) for root in staging_roots())
    if not any(destination == root or root in destination.parents for root in allowed):
        raise SubtitleApiError("output must be inside the selected Movies Nerd staging root")
    if destination.exists():
        raise SubtitleApiError("output already exists; refusing to overwrite")
    return destination


def fetch_download(link: str) -> bytes:
    parsed = urlparse(link)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "opensubtitles.com" or host.endswith(".opensubtitles.com")):
        raise SubtitleApiError("OpenSubtitles returned an unapproved download host")
    request = Request(link, headers={"User-Agent": USER_AGENT, "Accept": "application/x-subrip,text/plain"})
    try:
        with build_opener(OpenSubtitlesRedirect()).open(request, timeout=30) as response:
            data = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise SubtitleApiError(f"subtitle download failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise SubtitleApiError(f"subtitle download failed: {exc.reason}") from exc
    if len(data) > MAX_BYTES:
        raise SubtitleApiError("subtitle download exceeds 5 MiB")
    return data


def download(key: str, args: argparse.Namespace) -> dict:
    if not args.commit:
        raise SubtitleApiError("refusing to download without --commit")
    destination = checked_destination(args.output, args.language)
    response = api_json("/download", key, payload={"file_id": args.file_id})
    link = response.get("link")
    if not isinstance(link, str):
        raise SubtitleApiError("OpenSubtitles did not return a download link")
    data = fetch_download(link)
    duration = media_duration(Path(args.media).resolve(strict=True)) if args.media else None
    validation = validate_bytes(data, args.language, duration, args.forced)
    if not validation["valid"]:
        raise SubtitleApiError("downloaded SRT failed validation: " + "; ".join(validation["reasons"]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
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
        "written": str(destination),
        "file_id": args.file_id,
        "language": args.language,
        "validation": validation,
        "remaining": response.get("remaining"),
        "requests": response.get("requests"),
    }


def language_list(value: str) -> list[str]:
    items = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not items or any(item not in {"en", "fr"} for item in items):
        raise argparse.ArgumentTypeError("languages must be en, fr, or en,fr")
    return list(dict.fromkeys(items))


def imdb_id(value: str) -> str:
    if not re.fullmatch(r"(?:tt)?\d{5,12}", value):
        raise argparse.ArgumentTypeError("invalid IMDb ID")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    find = sub.add_parser("search")
    find.add_argument("--imdb-id", type=imdb_id)
    find.add_argument("--query")
    find.add_argument("--year", type=int)
    find.add_argument("--kind", choices=("movie", "episode", "all"), default="movie")
    find.add_argument("--languages", type=language_list, default=["en", "fr"])
    find.add_argument("--limit", type=int, choices=range(1, 51), default=20)
    get = sub.add_parser("download")
    get.add_argument("--file-id", type=int, required=True)
    get.add_argument("--language", choices=("en", "fr"), required=True)
    get.add_argument("--output", required=True)
    get.add_argument("--media", required=True)
    get.add_argument("--forced", action="store_true")
    get.add_argument("--commit", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        key = api_key()
        if args.command == "search":
            if not args.imdb_id and not args.query:
                raise SubtitleApiError("search requires --imdb-id or --query")
            if not args.languages:
                raise SubtitleApiError("languages must include en and/or fr")
            output = search(key, args)
        else:
            output = download(key, args)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, SubtitleApiError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
