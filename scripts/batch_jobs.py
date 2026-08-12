#!/usr/bin/env python3
"""Prepare and run every requested title concurrently by default."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import sys
from typing import Callable

from job_manifest import ManifestError, load_job, read_json
import prepare_job
import run_job

ALL_JOBS = 0


class BatchError(ValueError):
    pass


def checked_items(value: dict) -> list[dict]:
    items = value.get("items")
    if not isinstance(items, list) or not items:
        raise BatchError("items must contain at least one requested title")
    output = []
    for index, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            raise BatchError(f"item {index} must be an object")
        title = " ".join(str(raw.get("title") or "").split())
        kind = str(raw.get("kind") or "movie")
        try:
            year = int(raw.get("year"))
            runtime = float(raw["runtime_minutes"]) if raw.get("runtime_minutes") is not None else None
            max_gib = float(raw.get("max_gib", 15.0))
        except (TypeError, ValueError) as exc:
            raise BatchError(f"item {index} has an invalid number") from exc
        imdb_id = str(raw.get("imdb_id") or "").lower() or None
        if not title or kind not in {"movie", "series"} or not 1870 <= year <= 2100:
            raise BatchError(f"item {index} has an invalid title, year, or kind")
        if runtime is not None and not 1 <= runtime <= 1440:
            raise BatchError(f"item {index} has an invalid runtime")
        if imdb_id and not re.fullmatch(r"tt[0-9]{5,10}", imdb_id):
            raise BatchError(f"item {index} has an invalid IMDb ID")
        if not 0 < max_gib <= 100:
            raise BatchError(f"item {index} has an invalid size limit")
        output.append({
            "title": title, "year": year, "kind": kind,
            "runtime_minutes": runtime, "imdb_id": imdb_id, "max_gib": max_gib,
        })
    return output


def worker_count(requested: int, concurrency: int) -> int:
    if requested < 1:
        raise BatchError("at least one job is required")
    if concurrency < 0:
        raise BatchError("concurrency cannot be negative")
    return requested if concurrency == ALL_JOBS else min(concurrency, requested)


def prepare_many(value: dict, *, concurrency: int = ALL_JOBS, timeout: float = 5.0) -> dict:
    items = checked_items(value)
    results: list[dict | None] = [None] * len(items)

    def worker(index: int, item: dict) -> tuple[int, dict]:
        try:
            result = prepare_job.prepare(**item, timeout=timeout)
            return index, result
        except Exception as exc:  # Keep independent requests moving.
            return index, {
                "prepared": False,
                "title": f"{item['title']} ({item['year']})",
                "error": str(exc),
            }

    workers = worker_count(len(items), concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, index, item) for index, item in enumerate(items)]
        for future in as_completed(futures):
            index, result = future.result()
            results[index] = result
    completed = [item for item in results if item is not None]
    return {
        "prepared": sum(bool(item.get("prepared")) for item in completed),
        "requested": len(items),
        "concurrency": workers,
        "authorized": True,
        "jobs": completed,
    }


def run_many(
    job_paths: list[Path], *, concurrency: int = ALL_JOBS,
    poll_seconds: int = 5, artifact_wait_seconds: int = 600,
    runner: Callable[..., dict] = run_job.run,
) -> dict:
    if not job_paths:
        raise BatchError("provide at least one job")
    checked: list[Path] = []
    seen = set()
    for raw in job_paths:
        path, _job = load_job(raw)
        if path in seen:
            raise BatchError("the same job was provided twice")
        seen.add(path)
        checked.append(path)
    results: list[dict | None] = [None] * len(checked)

    def worker(index: int, path: Path) -> tuple[int, dict]:
        try:
            result = runner(
                path, poll_seconds=poll_seconds,
                artifact_wait_seconds=artifact_wait_seconds,
            )
            return index, {"job": str(path), **result}
        except Exception as exc:  # One stalled title must not strand the batch.
            return index, {"job": str(path), "ready": False, "resumable": True, "error": str(exc)}

    workers = worker_count(len(checked), concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, index, path) for index, path in enumerate(checked)]
        for future in as_completed(futures):
            index, result = future.result()
            results[index] = result
    completed = [item for item in results if item is not None]
    return {
        "requested": len(checked),
        "ready": sum(bool(item.get("ready")) for item in completed),
        "concurrency": workers,
        "jobs": completed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--input", required=True, help="JSON object with an items list")
    prepare.add_argument(
        "--concurrency", type=int, default=ALL_JOBS,
        help="0 starts every requested search together (default)",
    )
    prepare.add_argument("--timeout", type=float, default=5.0)
    run = sub.add_parser("run")
    run.add_argument("--job", type=Path, action="append", required=True)
    run.add_argument(
        "--concurrency", type=int, default=ALL_JOBS,
        help="0 starts every requested download together (default)",
    )
    run.add_argument("--poll-seconds", type=int, default=5)
    run.add_argument("--artifact-wait-seconds", type=int, default=600)
    run.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            if args.concurrency < 0 or not 1 <= args.timeout <= 15:
                raise BatchError("preparation concurrency or timeout is outside its safe range")
            result = prepare_many(
                read_json(args.input, 1024 * 1024),
                concurrency=args.concurrency, timeout=args.timeout,
            )
            status = 0 if result["prepared"] == result["requested"] else 4
        else:
            if not args.commit:
                parser.error("batch execution requires --commit for the requested downloads")
            if args.concurrency < 0:
                raise BatchError("download concurrency cannot be negative")
            result = run_many(
                args.job, concurrency=args.concurrency,
                poll_seconds=args.poll_seconds,
                artifact_wait_seconds=args.artifact_wait_seconds,
            )
            status = 0 if result["ready"] == result["requested"] else 8
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return status
    except (BatchError, ManifestError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
