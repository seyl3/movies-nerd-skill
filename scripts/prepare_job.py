#!/usr/bin/env python3
"""Search once, authorize from the explicit request, and persist a resumable job."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

import cinemeta
from _common import GIB, format_gib
from job_manifest import ManifestError, create_job, record_search_value, remove_failed_job, transition_job
from provider_health import HealthError, dead_hashes, record_provider
from search_releases import (
    PROVIDER_LABELS, SearchError, checked_query, release_selection, safe_int, search_all,
)
from title_policy import decide


def prepare(
    *, title: str, year: int, kind: str, runtime_minutes: float | None,
    imdb_id: str | None, max_gib: float, timeout: float,
    resolved_identity: dict | None = None,
) -> dict:
    requested_title = title
    if resolved_identity is not None:
        resolved_imdb = str(resolved_identity.get("imdb_id") or "").lower()
        canonical_title = " ".join(str(resolved_identity.get("canonical_title") or "").split())
        try:
            resolved_year = int(resolved_identity.get("year"))
        except (TypeError, ValueError) as exc:
            raise cinemeta.CinemetaError("the resolved identity has no valid year") from exc
        if (
            not cinemeta.IMDB_RE.fullmatch(resolved_imdb)
            or not canonical_title or resolved_year != year
            or (imdb_id and imdb_id.lower() != resolved_imdb)
        ):
            raise cinemeta.CinemetaError("the resolved identity does not match the request")
    elif imdb_id:
        resolved_imdb = imdb_id.lower()
        meta = cinemeta.metadata(kind, resolved_imdb)
        meta_year = cinemeta.release_year(meta.get("year") or meta.get("releaseInfo"))
        if meta_year != year:
            raise cinemeta.CinemetaError("the supplied IMDb ID does not match the requested year")
        canonical_title = " ".join(str(meta.get("name") or title).split())
    else:
        resolved = cinemeta.resolve_identity(kind, title, year)
        resolved_imdb = str(resolved["imdb_id"]).lower()
        canonical_title = str(resolved["canonical_title"])
    policy = decide(requested_title=requested_title, canonical_title=canonical_title)
    aliases = policy["search_titles"]
    canonical_title = aliases[0]
    query = checked_query(canonical_title, year)
    max_bytes = int(max_gib * GIB)
    try:
        excluded = dead_hashes(kind)
    except (HealthError, OSError, ValueError):
        excluded = set()
    started = time.monotonic()
    results, providers, early_success = search_all(
        query, timeout, kind == "series", True,
        title=canonical_title, year=year, max_bytes=max_bytes,
        runtime_minutes=runtime_minutes, imdb_id=resolved_imdb,
        excluded_hashes=excluded,
        search_titles=aliases,
    )
    for name, report in providers.items():
        if "latency_ms" not in report:
            continue
        try:
            record_provider(
                kind, PROVIDER_LABELS.get(name, name), ok=bool(report.get("ok")),
                latency_ms=safe_int(report.get("latency_ms")),
                results=safe_int(report.get("results")),
            )
        except (HealthError, OSError, ValueError):
            pass
    selection = release_selection(
        results, aliases, year, max_bytes, runtime_minutes,
        kind=kind, excluded_hashes=excluded,
    )
    if not selection.get("primary"):
        return {
            "prepared": False,
            "fallback_needed": True,
            "next": "ext-browser",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    extra: dict = {"cache": {"title_policy": policy}}
    if runtime_minutes is not None:
        extra["cache"]["runtime_minutes"] = runtime_minutes
    if resolved_imdb:
        extra["identity"] = {"ids": {"imdb": resolved_imdb}}
    path = create_job(kind, canonical_title, year, extra or None)
    result = {
        "query": query,
        "request": {
            "title": canonical_title, "requested_title": requested_title,
            "year": year, "kind": kind,
            "imdb_id": resolved_imdb,
        },
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "providers": providers,
        "results": results,
        "usable_results": selection.get("eligible_count", 0),
        "early_success": early_success,
        "selection": selection,
        "fallback": {"needed": False, "next": None},
    }
    try:
        recorded = record_search_value(path, result)
        transition_job(path, "confirmed")
    except Exception:
        try:
            transition_job(path, "failed", reason="job preparation failed")
            remove_failed_job(path)
        except Exception:
            pass
        raise
    primary = recorded["release"]
    envelope = recorded.get("confirmation_envelope") or {}
    return {
        "prepared": True,
        "job": str(path),
        "title": f"{policy['display_title']} ({year})",
        "quality": envelope.get("quality") or primary.get("resolution"),
        "maximum_size": format_gib(int(envelope.get("max_size_bytes") or primary["size_bytes"])),
        "recommended_size": format_gib(int(primary["size_bytes"])),
        "reported_seeders": int(primary.get("seeders") or 0),
        "source": primary.get("source"),
        "candidate_count": len(recorded.get("candidate_pool") or []),
        "elapsed_ms": result["elapsed_ms"],
        "authorized": True,
        "authorization": "explicit-download-request",
        "next": "run-job",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--kind", choices=("movie", "series"), default="movie")
    parser.add_argument("--runtime-min", type=float)
    parser.add_argument("--imdb-id")
    parser.add_argument("--max-gib", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    if not 1870 <= args.year <= 2100:
        parser.error("--year must be between 1870 and 2100")
    if args.runtime_min is not None and not 1 <= args.runtime_min <= 1440:
        parser.error("--runtime-min must be between 1 and 1440")
    if args.imdb_id and not re.fullmatch(r"tt[0-9]{5,10}", args.imdb_id.lower()):
        parser.error("--imdb-id must look like tt1234567")
    if not 0 < args.max_gib <= 100:
        parser.error("--max-gib must be between 0 and 100")
    if not 1 <= args.timeout <= 15:
        parser.error("--timeout must be between 1 and 15 seconds")
    try:
        result = prepare(
            title=args.title, year=args.year, kind=args.kind,
            runtime_minutes=args.runtime_min, imdb_id=args.imdb_id,
            max_gib=args.max_gib, timeout=args.timeout,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("prepared") else 4
    except (cinemeta.CinemetaError, ManifestError, SearchError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
