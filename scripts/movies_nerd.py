#!/usr/bin/env python3
"""One public controller from a requested title to a ready library entry."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable

import cinemeta
from _common import MOVIES_ROOT_ENV, SERIES_ROOT_ENV, emit_event, root_for_kind
from acquire import ControllerError, TerminalAcquisitionError
from finalize_job import FinalizeError
from job_manifest import ManifestError, jobs_root, load_job
import library_inventory
import prepare_job
from prepare_artifacts import ArtifactPreparationError
from qbittorrent_api import QbtAccessDenied, QbtError, QbtUnavailable
import run_job

TITLE_YEAR_RE = re.compile(r"^(.*?)\s*\(((?:18|19|20|21)\d{2})\)\s*$")


class MoviesNerdError(RuntimeError):
    pass


def split_title_year(title: str, year: int | None = None) -> tuple[str, int | None]:
    cleaned = " ".join(str(title or "").split())
    if not cleaned:
        raise MoviesNerdError("provide a movie or series title")
    match = TITLE_YEAR_RE.fullmatch(cleaned)
    embedded = int(match.group(2)) if match else None
    if embedded is not None:
        cleaned = match.group(1).strip()
    if year is not None and embedded is not None and year != embedded:
        raise MoviesNerdError("the two supplied release years do not match")
    resolved_year = year if year is not None else embedded
    if resolved_year is not None and not 1870 <= resolved_year <= 2100:
        raise MoviesNerdError("the release year is outside the supported range")
    return cleaned, resolved_year


def resolve_controller_identity(
    kind: str, title: str, year: int | None, imdb_id: str | None,
) -> dict:
    if imdb_id:
        identifier = imdb_id.strip().lower()
        if not cinemeta.IMDB_RE.fullmatch(identifier):
            raise MoviesNerdError("the IMDb ID is invalid")
        meta = cinemeta.metadata(kind, identifier)
        resolved_year = cinemeta.release_year(meta.get("year") or meta.get("releaseInfo"))
        if resolved_year is None or (year is not None and resolved_year != year):
            raise MoviesNerdError("the IMDb ID does not match the requested year")
        canonical = " ".join(str(meta.get("name") or title).split())
        return {"imdb_id": identifier, "canonical_title": canonical, "year": resolved_year}
    return cinemeta.resolve_request(kind, title, year)


def resumable_job(kind: str, imdb_id: str) -> Path | None:
    root = jobs_root(kind, create=False)
    if not root.is_dir() or root.is_symlink():
        return None
    matches = []
    for candidate in sorted(root.glob("*.json")):
        try:
            checked, job = load_job(candidate)
        except (ManifestError, OSError, ValueError):
            continue
        identifier = str(((job.get("identity") or {}).get("ids") or {}).get("imdb") or "").lower()
        if identifier == imdb_id and job.get("state") not in {"failed", "imported"}:
            matches.append(checked)
    if len(matches) > 1:
        raise MoviesNerdError("more than one resumable job exists for this title")
    return matches[0] if matches else None


def download_one(
    *, title: str, year: int | None = None, kind: str = "movie",
    runtime_minutes: float | None = None, imdb_id: str | None = None,
    max_gib: float = 15.0, timeout: float = 5.0, poll_seconds: int = 5,
    artifact_wait_seconds: int = 600,
    resolver: Callable[..., dict] = resolve_controller_identity,
    preparer: Callable[..., dict] = prepare_job.prepare,
    runner: Callable[..., dict] = run_job.run,
) -> dict:
    if kind not in {"movie", "series"}:
        raise MoviesNerdError("kind must be movie or series")
    if runtime_minutes is not None and not 1 <= runtime_minutes <= 1440:
        raise MoviesNerdError("runtime is outside the supported range")
    if not 0 < max_gib <= 100 or not 1 <= timeout <= 15:
        raise MoviesNerdError("the size limit or search timeout is outside its safe range")
    if not 2 <= poll_seconds <= 30 or not 0 <= artifact_wait_seconds <= 3600:
        raise MoviesNerdError("the controller timing is outside its safe range")
    requested_title, requested_year = split_title_year(title, year)
    identity = resolver(kind, requested_title, requested_year, imdb_id)
    resolved_year = int(identity["year"])
    canonical_title = str(identity["canonical_title"])
    existing_job = resumable_job(kind, str(identity["imdb_id"]))
    if existing_job is not None:
        result = runner(
            existing_job, poll_seconds=poll_seconds,
            artifact_wait_seconds=artifact_wait_seconds,
        )
        return {"request": f"{canonical_title} ({resolved_year})", "resumed": True, **result}
    library = root_for_kind(kind)
    present = (
        library_inventory.contains(
            library_inventory.scan(library),
            [requested_title, canonical_title], resolved_year,
        )
        if kind == "movie" else library_inventory.contains_series(
            library, [requested_title, canonical_title], resolved_year,
        )
    )
    if present:
        result = {
            "event": "ready", "ready": True, "already_present": True,
            "title": f"{canonical_title} ({resolved_year})",
        }
        emit_event("ready", title=result["title"], already_present=True)
        return result
    prepared = preparer(
        title=requested_title, year=resolved_year, kind=kind,
        runtime_minutes=runtime_minutes, imdb_id=str(identity["imdb_id"]),
        max_gib=max_gib, timeout=timeout, resolved_identity=identity,
    )
    if not prepared.get("prepared"):
        return {
            "ready": False, "title": f"{canonical_title} ({resolved_year})",
            "fallback_needed": bool(prepared.get("fallback_needed")),
            "next": prepared.get("next"),
        }
    result = runner(
        Path(str(prepared["job"])), poll_seconds=poll_seconds,
        artifact_wait_seconds=artifact_wait_seconds,
    )
    return {
        "request": prepared.get("title") or f"{canonical_title} ({resolved_year})",
        "prepared": True, **result,
    }


def download_many(
    items: list[dict], *, concurrency: int = 0,
    worker: Callable[..., dict] = download_one,
) -> dict:
    if not items:
        raise MoviesNerdError("provide at least one requested title")
    if concurrency < 0:
        raise MoviesNerdError("concurrency cannot be negative")
    workers = len(items) if concurrency == 0 else min(concurrency, len(items))
    results: list[dict | None] = [None] * len(items)

    def execute(index: int, item: dict) -> tuple[int, dict]:
        try:
            return index, worker(**item)
        except Exception as exc:
            return index, {
                "ready": False, "resumable": True,
                "title": str(item.get("title") or "requested title"), "error": str(exc),
            }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(execute, index, item) for index, item in enumerate(items)]
        for future in as_completed(futures):
            index, result = future.result()
            results[index] = result
    completed = [item for item in results if item is not None]
    return {
        "requested": len(items), "ready": sum(bool(item.get("ready")) for item in completed),
        "concurrency": workers, "jobs": completed,
    }


def apply_library(kind: str, library: str | None) -> None:
    if not library:
        return
    path = Path(library).expanduser()
    if not path.is_absolute():
        raise MoviesNerdError("the library must be an absolute path")
    os.environ[MOVIES_ROOT_ENV if kind == "movie" else SERIES_ROOT_ENV] = str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("download", help="download and organize one or more titles")
    download.add_argument("titles", nargs="+")
    download.add_argument("--year", type=int, help="single-title release year")
    download.add_argument("--kind", choices=("movie", "series"), default="movie")
    download.add_argument("--library", help="absolute destination library")
    download.add_argument("--runtime-min", type=float)
    download.add_argument("--imdb-id")
    download.add_argument("--max-gib", type=float, default=15.0)
    download.add_argument("--timeout", type=float, default=5.0)
    download.add_argument("--concurrency", type=int, default=0)
    download.add_argument("--poll-seconds", type=int, default=5)
    download.add_argument("--artifact-wait-seconds", type=int, default=600)
    args = parser.parse_args()
    try:
        apply_library(args.kind, args.library)
        if len(args.titles) > 1 and (args.year is not None or args.imdb_id or args.runtime_min is not None):
            raise MoviesNerdError("per-title year, runtime, or IMDb details belong inside each title")
        items = []
        for raw in args.titles:
            title, year = split_title_year(raw, args.year if len(args.titles) == 1 else None)
            items.append({
                "title": title, "year": year, "kind": args.kind,
                "runtime_minutes": args.runtime_min if len(args.titles) == 1 else None,
                "imdb_id": args.imdb_id if len(args.titles) == 1 else None,
                "max_gib": args.max_gib, "timeout": args.timeout,
                "poll_seconds": args.poll_seconds,
                "artifact_wait_seconds": args.artifact_wait_seconds,
            })
        if len(items) == 1:
            result = download_one(**items[0])
            status = 0 if result.get("ready") else 8
        else:
            result = download_many(items, concurrency=args.concurrency)
            status = 0 if result["ready"] == result["requested"] else 8
        if not (len(items) == 1 and result.get("ready") and not result.get("already_present")):
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return status
    except QbtAccessDenied as exc:
        print(json.dumps({
            "ready": False, "error": str(exc), "needs_local_app_access": True,
            "user_action_required": False, "resumable": True,
        }), file=sys.stderr)
        return 6
    except QbtUnavailable as exc:
        print(json.dumps({"ready": False, "error": str(exc), "resumable": True}), file=sys.stderr)
        return 5
    except TerminalAcquisitionError as exc:
        print(json.dumps({"ready": False, "error": str(exc), "cleaned_up": True}), file=sys.stderr)
        return 2
    except (
        ArtifactPreparationError, cinemeta.CinemetaError, ControllerError, FinalizeError,
        ManifestError, MoviesNerdError, QbtError, OSError, ValueError,
    ) as exc:
        print(json.dumps({"ready": False, "error": str(exc), "resumable": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
