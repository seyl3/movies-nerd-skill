#!/usr/bin/env python3
"""Fast, dependency-free API search with a browser fallback signal."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import shutil
import ssl
import subprocess
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from _common import GIB
from qbittorrent_api import QbtError, connected_client, magnet_hash
from rank_releases import normalize

ALLOWED_API_HOSTS = {"api.knaben.org", "apibay.org"}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_RESULTS_PER_PROVIDER = 100
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
INFO_HASH_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


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
    headers = {"Accept": "application/json", "User-Agent": "Movies-Nerd/1"}
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


def minimal_magnet(info_hash: object, title: object) -> str | None:
    candidate = str(info_hash or "").strip()
    if not INFO_HASH_RE.fullmatch(candidate):
        return None
    display = str(title or "torrent").strip()[:300] or "torrent"
    return f"magnet:?xt=urn:btih:{candidate.lower()}&dn={quote(display, safe='')}"


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
        })
    return results


def search_apibay(query: str, timeout: float) -> list[dict]:
    url = "https://apibay.org/q.php?" + urlencode({"q": query, "cat": 0})
    return normalize_apibay(fetch_json(url, timeout))


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
        if previous is None or item.get("seeders", 0) > previous.get("seeders", 0):
            selected[key] = item
    return list(selected.values())


def title_tokens(value: object) -> list[str]:
    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value).casefold())
        if not unicodedata.combining(character)
    )
    return re.findall(r"\w+", folded, re.UNICODE)


def matches_requested_title(release_title: object, title: str, year: int | None) -> bool:
    release = title_tokens(release_title)
    expected = title_tokens(title)
    if not expected or len(release) < len(expected):
        return False
    phrase_found = any(
        release[index:index + len(expected)] == expected
        for index in range(len(release) - len(expected) + 1)
    )
    return phrase_found and (year is None or str(year) in release)


def usable_count(
    results: list[dict], title: str, year: int | None, max_bytes: int,
    runtime_minutes: float | None,
) -> int:
    usable = 0
    for item in results:
        ranked = normalize(item, max_bytes, runtime_minutes)
        if (
            matches_requested_title(item.get("title"), title, year)
            and ranked["eligible"]
            and ranked["magnet"]
            and ranked["seeders"] > 0
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
    results: list[dict], title: str, year: int | None, max_bytes: int,
    runtime_minutes: float | None,
) -> dict:
    candidates = []
    for item in results:
        ranked = normalize(item, max_bytes, runtime_minutes)
        if not (
            matches_requested_title(item.get("title"), title, year)
            and ranked["eligible"]
            and ranked["magnet"]
            and ranked["seeders"] > 0
        ):
            continue
        candidates.append({
            "title": ranked["title"],
            "source": ranked["source"],
            "provider": item.get("provider"),
            "size_bytes": ranked["size_bytes"],
            "size": ranked["size"],
            "resolution": ranked["resolution"],
            "seeders": ranked["seeders"],
            "leechers": safe_int(item.get("leechers")),
            "score": ranked["score"],
            "warnings": ranked["warnings"],
            "magnet": ranked["magnet"],
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
    return {"primary": primary, "backup": backup, "eligible_count": len(candidates)}


def run_providers(providers: dict[str, object]) -> tuple[list[dict], dict]:
    reports: dict[str, dict] = {}
    combined: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(providers)) as executor:
        futures = {executor.submit(call): name for name, call in providers.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result()
                combined.extend(items)
                reports[name] = {"ok": True, "results": len(items)}
            except (SearchError, QbtError, OSError) as exc:
                reports[name] = {"ok": False, "error": str(exc)[:200]}
    return combined, reports


def search_all(
    query: str, timeout: float, series: bool, use_qbt: bool, *, title: str,
    year: int | None, max_bytes: int, runtime_minutes: float | None,
    minimum_usable: int = 3,
) -> tuple[list[dict], dict, bool]:
    started = time.monotonic()
    fast_timeout = min(2.0, timeout)
    combined, reports = run_providers({
        "knaben": lambda: search_knaben(query, fast_timeout),
        "apibay": lambda: search_apibay(query, fast_timeout),
    })
    combined = deduplicate(combined)
    fast_selection = release_selection(combined, title, year, max_bytes, runtime_minutes)
    early_success = (
        fast_selection["eligible_count"] >= minimum_usable
        and fast_selection["backup"] is not None
    )
    if use_qbt and not early_success:
        remaining = max(0.5, timeout - (time.monotonic() - started))
        qbt_items, qbt_report = run_providers({
            "qbt_torznab": lambda: search_qbt(query, remaining, series),
        })
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
    parser.add_argument("--max-gib", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--no-qbt-search", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 15:
        parser.error("--timeout must be between 1 and 15 seconds")
    if not 0 < args.max_gib <= 100:
        parser.error("--max-gib must be between 0 and 100")
    query = checked_query(args.title, args.year)
    started = time.monotonic()
    max_bytes = int(args.max_gib * GIB)
    results, providers, early_success = search_all(
        query, args.timeout, args.series, not args.no_qbt_search,
        title=args.title, year=args.year, max_bytes=max_bytes,
        runtime_minutes=args.runtime_min,
    )
    selection = release_selection(results, args.title, args.year, max_bytes, args.runtime_min)
    usable = selection["eligible_count"]
    output = {
        "query": query,
        "request": {
            "title": args.title,
            "year": args.year,
            "kind": "series" if args.series else "movie",
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
