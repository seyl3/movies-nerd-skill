#!/usr/bin/env python3
"""Fast, dependency-free API search with a browser fallback signal."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import re
import shutil
import ssl
import subprocess
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from _common import GIB
from provider_health import HealthError, dead_hashes, provider_bonus, record_provider
from qbittorrent_api import QbtError, connected_client, magnet_hash, safe_magnet
from rank_releases import normalize
from torrent_metadata import TorrentMetadataError, checked_torrent_url
from title_policy import unique_titles

ALLOWED_API_HOSTS = {"api.knaben.org", "apibay.org", "magnetz.eu", "movies-api.accel.li", "yts.gg"}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_RESULTS_PER_PROVIDER = 100
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
INFO_HASH_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
PROVIDER_LABELS = {
    "knaben": "Knaben API",
    "apibay": "APIBay",
    "magnetz": "Magnetz API",
    "yts": "YTS API",
    "qbt_torznab": "qBittorrent/Torznab",
}


class SearchError(RuntimeError):
    pass


def checked_query(title: str, year: int | None) -> str:
    clean = title.strip()
    if not clean or len(clean.encode("utf-8")) > 300 or CONTROL_RE.search(clean):
        raise SearchError("title is empty, too long, or contains control characters")
    if year is not None and not 1870 <= year <= 2100:
        raise SearchError("year must be between 1870 and 2100")
    return f"{clean} {year}" if year else clean


def checked_api_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_API_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise SearchError("API URL is outside the fixed HTTPS allowlist")
    return url


class AllowlistedRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_api_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_json(url: str, timeout: float, payload: dict | None = None) -> object:
    checked_api_url(url)
    data = None
    headers = {"Accept": "application/json", "User-Agent": "Movies-Nerd/2"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    opener = build_opener(AllowlistedRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            checked_api_url(response.geturl())
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise SearchError(f"API returned HTTP {exc.code}") from exc
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raw = fetch_with_system_curl(url, timeout, data, headers)
        else:
            raise SearchError(f"API is unavailable: {exc.reason}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SearchError("API response exceeds 5 MiB")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SearchError("API returned invalid JSON") from exc


def fetch_with_system_curl(url: str, timeout: float, data: bytes | None, headers: dict) -> bytes:
    """Use the OS trust store without weakening certificate verification."""
    curl = shutil.which("curl")
    if not curl:
        raise SearchError("Python TLS trust failed and system curl is unavailable")
    command = [
        curl, "--fail", "--silent", "--show-error", "--proto", "=https",
        "--max-time", str(timeout), "--max-filesize", str(MAX_RESPONSE_BYTES),
        "--header", f"Accept: {headers['Accept']}",
        "--header", f"User-Agent: {headers['User-Agent']}",
    ]
    if data is not None:
        command.extend(["--header", "Content-Type: application/json", "--data-binary", data.decode("utf-8")])
    command.append(url)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SearchError(f"system HTTPS request failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:200]
        raise SearchError(f"system HTTPS request failed: {detail or 'curl error'}")
    if len(completed.stdout) > MAX_RESPONSE_BYTES:
        raise SearchError("API response exceeds 5 MiB")
    return completed.stdout


def safe_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def minimal_magnet(info_hash: object, title: object, *, trackers: bool = True) -> str | None:
    candidate = str(info_hash or "").strip()
    if not INFO_HASH_RE.fullmatch(candidate):
        return None
    try:
        return safe_magnet(candidate, str(title or "torrent"), trackers=trackers)
    except QbtError:
        return None


def sanitized_magnet(value: object, info_hash: object, title: object) -> str | None:
    direct = minimal_magnet(info_hash, title)
    if direct:
        return direct
    raw = str(value or "")
    if not raw:
        return None
    try:
        return minimal_magnet(magnet_hash(raw), title)
    except QbtError:
        return None


def normalize_knaben(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
        raise SearchError("Knaben returned an unexpected response")
    results = []
    for item in payload["hits"][:MAX_RESULTS_PER_PROVIDER]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        magnet = sanitized_magnet(item.get("magnetUrl"), item.get("hash"), title)
        size = safe_int(item.get("bytes"))
        if not title or not magnet or size <= 0:
            continue
        results.append({
            "title": title,
            "size": size,
            "seeders": safe_int(item.get("seeders")),
            "leechers": safe_int(item.get("peers")),
            "source": str(item.get("tracker") or item.get("cachedOrigin") or "Knaben"),
            "provider": "Knaben API",
            "magnet": magnet,
            "info_hash": magnet_hash(magnet),
        })
    return results


def search_knaben(query: str, timeout: float) -> list[dict]:
    payload = {
        "search_type": "100%",
        "search_field": "title",
        "query": query,
        "order_by": "seeders",
        "order_direction": "desc",
        "from": 0,
        "size": MAX_RESULTS_PER_PROVIDER,
        "hide_unsafe": True,
        "hide_xxx": True,
    }
    return normalize_knaben(fetch_json("https://api.knaben.org/v1", timeout, payload))


def normalize_apibay(payload: object) -> list[dict]:
    if not isinstance(payload, list):
        raise SearchError("APIBay returned an unexpected response")
    results = []
    for item in payload[:MAX_RESULTS_PER_PROVIDER]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or "").strip()
        magnet = minimal_magnet(item.get("info_hash"), title)
        size = safe_int(item.get("size"))
        if not title or not magnet or size <= 0:
            continue
        results.append({
            "title": title,
            "size": size,
            "seeders": safe_int(item.get("seeders")),
            "leechers": safe_int(item.get("leechers")),
            "source": "The Pirate Bay",
            "provider": "APIBay",
            "magnet": magnet,
            "info_hash": magnet_hash(magnet),
        })
    return results


def search_apibay(query: str, timeout: float) -> list[dict]:
    url = "https://apibay.org/q.php?" + urlencode({"q": query, "cat": 0})
    return normalize_apibay(fetch_json(url, timeout))


def normalize_magnetz(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise SearchError("Magnetz returned an unexpected response")
    results = []
    for item in payload["data"][:MAX_RESULTS_PER_PROVIDER]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or "").strip()
        magnet = sanitized_magnet(item.get("magnet_link"), item.get("info_hash"), title)
        size = safe_int(item.get("size"))
        if not title or not magnet or size <= 0:
            continue
        results.append({
            "title": title,
            "size": size,
            "seeders": safe_int(item.get("seeders")),
            "leechers": safe_int(item.get("leechers")),
            "source": "Magnetz",
            "provider": "Magnetz API",
            "magnet": magnet,
            "info_hash": magnet_hash(magnet),
        })
    return results


def search_magnetz(query: str, timeout: float) -> list[dict]:
    url = "https://magnetz.eu/api/magnets/search?" + urlencode({"query": query, "page": 1})
    return normalize_magnetz(fetch_json(url, timeout))


def normalize_yts(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise SearchError("YTS returned an unexpected response")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SearchError("YTS returned an unexpected response")
    movies = data.get("movies") or []
    if not isinstance(movies, list):
        raise SearchError("YTS returned an unexpected movie list")
    results = []
    for movie in movies[:MAX_RESULTS_PER_PROVIDER]:
        if not isinstance(movie, dict):
            continue
        movie_title = str(movie.get("title") or "").strip()
        year = safe_int(movie.get("year"))
        imdb_code = str(movie.get("imdb_code") or "").strip().lower()
        torrents = movie.get("torrents") or []
        if not movie_title or not isinstance(torrents, list):
            continue
        for torrent in torrents[:12]:
            if not isinstance(torrent, dict):
                continue
            quality = str(torrent.get("quality") or "").strip()
            release_type = str(torrent.get("type") or "").strip()
            codec = str(torrent.get("video_codec") or "").strip()
            title = " ".join(
                value for value in (movie_title, f"({year})" if year else "", quality, release_type, codec)
                if value
            )
            info_hash = str(torrent.get("hash") or "").strip().lower()
            magnet = minimal_magnet(info_hash, title)
            size = safe_int(torrent.get("size_bytes"))
            if not magnet or size <= 0:
                continue
            torrent_url = None
            raw_url = str(torrent.get("url") or "").strip()
            if raw_url:
                try:
                    torrent_url = checked_torrent_url(raw_url)
                except TorrentMetadataError:
                    torrent_url = None
            results.append({
                "title": title,
                "size": size,
                "seeders": safe_int(torrent.get("seeds")),
                "leechers": safe_int(torrent.get("peers")),
                "source": "YTS",
                "provider": "YTS API",
                "magnet": magnet,
                "info_hash": info_hash,
                "torrent_url": torrent_url,
                "imdb_code": imdb_code if re.fullmatch(r"tt\d{7,10}", imdb_code) else None,
                "canonical_title": str(movie.get("title_english") or movie_title).strip(),
                "language": str(movie.get("language") or "").strip() or None,
                "direct_metadata": bool(torrent_url),
            })
    return results


def search_yts(title: str, timeout: float, imdb_id: str | None = None) -> list[dict]:
    query = imdb_id if imdb_id and re.fullmatch(r"tt\d{7,10}", imdb_id.lower()) else title
    endpoints = (
        "https://movies-api.accel.li/api/v2/list_movies.json",
        "https://yts.gg/api/v2/list_movies.json",
    )
    started = time.monotonic()
    errors = []
    for endpoint in endpoints:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            break
        url = endpoint + "?" + urlencode({"query_term": query, "limit": 50})
        try:
            return normalize_yts(fetch_json(url, remaining))
        except SearchError as exc:
            errors.append(str(exc))
    raise SearchError(errors[-1] if errors else "YTS API timed out")


def normalize_qbt(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SearchError("qBittorrent returned unexpected search results")
    results = []
    for item in payload["results"][:MAX_RESULTS_PER_PROVIDER]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("fileName") or "").strip()
        magnet = sanitized_magnet(item.get("fileUrl"), None, title)
        size = safe_int(item.get("fileSize"))
        if not title or not magnet or size <= 0:
            continue
        site = urlparse(str(item.get("siteUrl") or "")).hostname
        results.append({
            "title": title,
            "size": size,
            "seeders": safe_int(item.get("nbSeeders")),
            "leechers": safe_int(item.get("nbLeechers")),
            "source": site or "qBittorrent search",
            "provider": "qBittorrent/Torznab",
            "magnet": magnet,
            "info_hash": magnet_hash(magnet),
        })
    return results


def search_qbt(query: str, timeout: float, series: bool = False) -> list[dict]:
    client = connected_client(wait_seconds=min(3.0, timeout))
    plugins = client.json("search/plugins")
    if not isinstance(plugins, list):
        raise SearchError("qBittorrent returned an invalid provider list")
    enabled = []
    category = "tv" if series else "movies"
    for plugin in plugins:
        if not isinstance(plugin, dict) or not plugin.get("enabled"):
            continue
        categories = {
            str(item.get("id"))
            for item in plugin.get("supportedCategories", [])
            if isinstance(item, dict)
        }
        if category in categories or "all" in categories:
            enabled.append(str(plugin.get("name") or ""))
    enabled = [name for name in enabled if name]
    if not enabled:
        return []

    started = client.json_post("search/start", {
        "pattern": query,
        "plugins": "|".join(enabled),
        "category": category,
    })
    if not isinstance(started, dict):
        raise SearchError("qBittorrent did not create a search job")
    job_id = safe_int(started.get("id"), -1)
    if job_id < 0:
        raise SearchError("qBittorrent returned an invalid search job")
    deadline = time.monotonic() + timeout
    latest: object = {"results": []}
    try:
        while time.monotonic() < deadline:
            latest = client.json(f"search/results?id={job_id}&limit={MAX_RESULTS_PER_PROVIDER}&offset=0")
            if isinstance(latest, dict) and latest.get("status") == "Stopped":
                break
            if isinstance(latest, dict) and safe_int(latest.get("total")) >= MAX_RESULTS_PER_PROVIDER:
                break
            time.sleep(0.2)
    finally:
        try:
            client.request("search/stop", {"id": str(job_id)})
        except QbtError:
            pass
        try:
            client.request("search/delete", {"id": str(job_id)})
        except QbtError:
            pass
    return normalize_qbt(latest)


def deduplicate(results: list[dict]) -> list[dict]:
    selected: dict[str, dict] = {}
    for item in results:
        try:
            key = magnet_hash(str(item.get("magnet") or ""))
        except QbtError:
            continue
        previous = selected.get(key)
        preferred = (
            bool(item.get("torrent_url")),
            safe_int(item.get("seeders")),
        )
        previous_preferred = (
            bool(previous.get("torrent_url")) if previous else False,
            safe_int(previous.get("seeders")) if previous else -1,
        )
        if previous is None or preferred > previous_preferred:
            selected[key] = item
    return list(selected.values())


def title_tokens(value: object) -> list[str]:
    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value).casefold())
        if not unicodedata.combining(character)
    )
    return re.findall(r"\w+", folded, re.UNICODE)


def matches_requested_title(
    release_title: object, title: str | list[str] | tuple[str, ...],
    year: int | None,
) -> bool:
    release = title_tokens(release_title)
    titles = title if isinstance(title, (list, tuple)) else [title]
    for candidate in titles:
        expected = title_tokens(candidate)
        if not expected or len(release) < len(expected):
            continue
        phrase_found = any(
            release[index:index + len(expected)] == expected
            for index in range(len(release) - len(expected) + 1)
        )
        if phrase_found and (year is None or str(year) in release):
            return True
    return False


def usable_count(
    results: list[dict], title: str | list[str], year: int | None, max_bytes: int,
    runtime_minutes: float | None, excluded_hashes: set[str] | None = None,
) -> int:
    excluded = excluded_hashes or set()
    usable = 0
    for item in results:
        ranked = normalize(item, max_bytes, runtime_minutes)
        try:
            info_hash = magnet_hash(str(item.get("magnet") or ""))
        except QbtError:
            continue
        if (
            matches_requested_title(item.get("title"), title, year)
            and ranked["eligible"]
            and ranked["magnet"]
            and info_hash not in excluded
        ):
            usable += 1
    return usable


def source_key(value: object) -> str:
    text = str(value or "unknown").strip().casefold()
    host = urlparse(text).hostname if "://" in text else None
    normalized = host or text
    if "pirate bay" in normalized or "thepiratebay" in normalized or normalized == "apibay":
        return "the-pirate-bay"
    return normalized


def release_selection(
    results: list[dict], title: str | list[str], year: int | None, max_bytes: int,
    runtime_minutes: float | None, *, kind: str | None = None,
    excluded_hashes: set[str] | None = None,
) -> dict:
    excluded = excluded_hashes or set()
    candidates = []
    bonuses: dict[str, float] = {}
    for item in results:
        ranked = normalize(item, max_bytes, runtime_minutes)
        try:
            info_hash = magnet_hash(str(item.get("magnet") or ""))
        except QbtError:
            continue
        if not (
            matches_requested_title(item.get("title"), title, year)
            and ranked["eligible"]
            and ranked["magnet"]
            and info_hash not in excluded
        ):
            continue
        provider = str(item.get("provider") or "")
        if kind and provider not in bonuses:
            bonuses[provider] = provider_bonus(kind, provider)
        reliability = bonuses.get(provider, 0.0)
        direct_bonus = 8.0 if item.get("torrent_url") else 0.0
        candidates.append({
            "title": ranked["title"],
            "source": ranked["source"],
            "provider": item.get("provider"),
            "size_bytes": ranked["size_bytes"],
            "size": ranked["size"],
            "resolution": ranked["resolution"],
            "seeders": ranked["seeders"],
            "leechers": safe_int(item.get("leechers")),
            "score": round(ranked["score"] + reliability + direct_bonus, 2),
            "warnings": ranked["warnings"],
            "magnet": ranked["magnet"],
            "info_hash": info_hash,
            "torrent_url": item.get("torrent_url"),
            "direct_metadata": bool(item.get("torrent_url")),
            "reported_peer_health": "estimate",
            "provider_reliability_bonus": reliability,
            "canonical_title": item.get("canonical_title"),
            "language": item.get("language"),
        })
    candidates.sort(key=lambda item: (item["score"], item["seeders"]), reverse=True)
    primary = candidates[0] if candidates else None
    backup = None
    if primary:
        primary_source = source_key(primary.get("source"))
        backup = next(
            (item for item in candidates[1:] if source_key(item.get("source")) != primary_source),
            None,
        )
    compatible = []
    if primary:
        envelope_max = min(
            max_bytes,
            max(primary["size_bytes"] + 512 * 1024 ** 2, int(primary["size_bytes"] * 1.2)),
        )
        compatible = [
            item for item in candidates
            if item["resolution"] == primary["resolution"]
            and item["size_bytes"] <= envelope_max
        ]
    race_candidates = []
    seen_sources = set()
    for item in compatible:
        key = source_key(item.get("source"))
        if key in seen_sources:
            continue
        race_candidates.append(item)
        seen_sources.add(key)
        if len(race_candidates) == 6:
            break
    if len(race_candidates) < 6:
        selected_hashes = {magnet_hash(item["magnet"]) for item in race_candidates}
        for item in compatible:
            if magnet_hash(item["magnet"]) in selected_hashes:
                continue
            race_candidates.append(item)
            selected_hashes.add(magnet_hash(item["magnet"]))
            if len(race_candidates) == 6:
                break
    displayed_max = max((item["size_bytes"] for item in race_candidates), default=None)
    return {
        "primary": primary,
        "backup": backup,
        "candidates": race_candidates,
        "eligible_count": len(candidates),
        "confirmation_envelope": {
            "quality": primary.get("resolution") if primary else None,
            "max_size_bytes": displayed_max,
            "max_size": f"{displayed_max / GIB:.2f} GiB" if displayed_max else None,
            "maximum_simultaneous_probes": 3,
            "maximum_waves": 2,
        },
    }


def run_providers(providers: dict[str, object], timeout: float) -> tuple[list[dict], dict]:
    reports: dict[str, dict] = {}
    combined: list[dict] = []
    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=len(providers))
    future_started = {}
    futures = {}
    try:
        for name, call in providers.items():
            future = executor.submit(call)
            futures[future] = name
            future_started[future] = time.monotonic()
        pending = set(futures)
        while pending:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=min(0.1, remaining), return_when=FIRST_COMPLETED)
            for future in done:
                name = futures[future]
                latency = round((time.monotonic() - future_started[future]) * 1000)
                try:
                    items = future.result()
                    combined.extend(items)
                    reports[name] = {"ok": True, "results": len(items), "latency_ms": latency}
                except (SearchError, QbtError, OSError) as exc:
                    reports[name] = {"ok": False, "error": str(exc)[:200], "latency_ms": latency}
        for future in pending:
            name = futures[future]
            future.cancel()
            reports[name] = {
                "ok": False,
                "error": "provider exceeded the shared search deadline",
                "latency_ms": round(timeout * 1000),
            }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return combined, reports


def search_title_aliases(
    searcher, titles: list[str], year: int | None, timeout: float,
) -> list[dict]:
    aliases = unique_titles(titles)
    started = time.monotonic()
    combined = []
    last_error: Exception | None = None
    for index, title in enumerate(aliases):
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            break
        requests_left = len(aliases) - index
        per_request = max(0.1, remaining / requests_left)
        try:
            items = searcher(checked_query(title, year), per_request)
        except (SearchError, QbtError, OSError) as exc:
            last_error = exc
            continue
        combined.extend(items)
        if any(matches_requested_title(item.get("title"), aliases[:index + 1], year) for item in items):
            break
    if not combined and last_error is not None:
        raise last_error
    return deduplicate(combined)


def search_all(
    query: str, timeout: float, series: bool, use_qbt: bool, *, title: str,
    year: int | None, max_bytes: int, runtime_minutes: float | None,
    minimum_usable: int = 3, imdb_id: str | None = None,
    excluded_hashes: set[str] | None = None,
    search_titles: list[str] | None = None,
) -> tuple[list[dict], dict, bool]:
    started = time.monotonic()
    fast_timeout = min(3.5, timeout)
    aliases = unique_titles(search_titles or [], title)
    combined, reports = run_providers({
        "knaben": lambda: search_title_aliases(search_knaben, aliases, year, fast_timeout),
        "apibay": lambda: search_title_aliases(search_apibay, aliases, year, fast_timeout),
        "magnetz": lambda: search_title_aliases(search_magnetz, aliases, year, fast_timeout),
        "yts": lambda: search_yts(aliases[0], fast_timeout, imdb_id),
    }, fast_timeout)
    combined = deduplicate(combined)
    fast_selection = release_selection(
        combined, aliases, year, max_bytes, runtime_minutes,
        excluded_hashes=excluded_hashes,
    )
    early_success = (
        fast_selection["eligible_count"] >= minimum_usable
        and fast_selection["backup"] is not None
    )
    if use_qbt and not early_success:
        remaining = max(0.5, timeout - (time.monotonic() - started))
        qbt_items, qbt_report = run_providers({
            "qbt_torznab": lambda: search_qbt(query, remaining, series),
        }, remaining)
        combined = deduplicate(combined + qbt_items)
        reports.update(qbt_report)
    elif use_qbt:
        reports["qbt_torznab"] = {
            "ok": True,
            "results": 0,
            "skipped": "enough exact healthy API results with a different-source backup",
        }
    return combined, reports, early_success


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("title")
    parser.add_argument("--year", type=int)
    parser.add_argument("--series", action="store_true")
    parser.add_argument("--runtime-min", type=float)
    parser.add_argument("--imdb-id")
    parser.add_argument("--max-gib", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--no-qbt-search", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 15:
        parser.error("--timeout must be between 1 and 15 seconds")
    if not 0 < args.max_gib <= 100:
        parser.error("--max-gib must be between 0 and 100")
    if args.imdb_id and not re.fullmatch(r"tt\d{7,10}", args.imdb_id.lower()):
        parser.error("--imdb-id must look like tt1234567")
    query = checked_query(args.title, args.year)
    started = time.monotonic()
    max_bytes = int(args.max_gib * GIB)
    kind = "series" if args.series else "movie"
    try:
        excluded = dead_hashes(kind)
    except (HealthError, OSError, ValueError):
        excluded = set()
    results, providers, early_success = search_all(
        query, args.timeout, args.series, not args.no_qbt_search,
        title=args.title, year=args.year, max_bytes=max_bytes,
        runtime_minutes=args.runtime_min,
        imdb_id=args.imdb_id, excluded_hashes=excluded,
    )
    for name, report in providers.items():
        if "latency_ms" not in report:
            continue
        try:
            record_provider(
                kind, PROVIDER_LABELS.get(name, name), ok=bool(report.get("ok")),
                latency_ms=safe_int(report.get("latency_ms")), results=safe_int(report.get("results")),
            )
        except (HealthError, OSError, ValueError):
            pass
    selection = release_selection(
        results, args.title, args.year, max_bytes, args.runtime_min,
        kind=kind, excluded_hashes=excluded,
    )
    usable = selection["eligible_count"]
    output = {
        "query": query,
        "request": {
            "title": args.title,
            "year": args.year,
            "kind": kind,
            "imdb_id": args.imdb_id.lower() if args.imdb_id else None,
        },
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "providers": providers,
        "results": results,
        "usable_results": usable,
        "early_success": early_success,
        "selection": selection,
        "fallback": {"needed": usable == 0, "next": "ext-browser" if usable == 0 else None},
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SearchError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
