#!/usr/bin/env python3
"""Persist the independent finalization work that runs during a transfer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from _common import staging_roots
from job_manifest import ManifestError, load_job, update_job

MOVIE_TASKS = (
    "destination", "metadata", "artwork", "subtitle-en", "subtitle-fr",
    "film-links", "recommendations",
)
SERIES_TASKS = (
    "destination", "metadata", "artwork", "subtitle-en", "subtitle-fr",
)
STATUSES = {"pending", "running", "complete", "failed"}


def required_tasks(job: dict) -> tuple[str, ...]:
    return MOVIE_TASKS if job.get("kind") == "movie" else SERIES_TASKS


def task_state(job: dict) -> dict:
    raw = job.get("enrichment_tasks") or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        name: raw.get(name) if isinstance(raw.get(name), dict) else {"status": "pending"}
        for name in required_tasks(job)
    }


def plan(job_path: Path) -> dict:
    checked, job = load_job(job_path)
    tasks = task_state(job)
    return {
        "job": str(checked),
        "parallel": True,
        "start_immediately": job.get("state") in {"downloading", "stalled"},
        "tasks": [
            {"name": name, "status": item.get("status", "pending")}
            for name, item in tasks.items()
        ],
        "ready": all(item.get("status") == "complete" for item in tasks.values()),
    }


def start_all(job_path: Path) -> dict:
    checked, job = load_job(job_path)
    if job.get("state") not in {"downloading", "stalled", "downloaded", "finalizing"}:
        raise ManifestError("finalization preparation starts only after a transfer starts")
    tasks = task_state(job)
    for item in tasks.values():
        if item.get("status") == "pending":
            item.update({"status": "running", "started_epoch": time.time()})
    update_job(checked, {"enrichment_tasks": tasks, "steps": {"enrichment": "running"}})
    return plan(checked)


def checked_artifact(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ManifestError("prepared artifact must be a regular staged file")
    resolved = path.resolve(strict=True)
    if not any(root.resolve(strict=False) in resolved.parents for root in staging_roots()):
        raise ManifestError("prepared artifact must stay inside Movies Nerd staging")
    return str(resolved)


def mark(
    job_path: Path, task: str, status: str, artifact: Path | None = None,
    note: str | None = None,
) -> dict:
    checked, job = load_job(job_path)
    tasks = task_state(job)
    if task not in tasks:
        raise ManifestError("unknown finalization task for this media type")
    if status not in STATUSES:
        raise ManifestError("invalid finalization task status")
    item = dict(tasks[task])
    item["status"] = status
    item["updated_epoch"] = time.time()
    if artifact:
        item["artifact"] = checked_artifact(artifact)
    if note:
        item["note"] = " ".join(note.strip().split())[:300]
    tasks[task] = item
    ready = all(value.get("status") == "complete" for value in tasks.values())
    update_job(checked, {
        "enrichment_tasks": tasks,
        "steps": {"enrichment": "complete" if ready else "running"},
    })
    return plan(checked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("plan")
    show.add_argument("job", type=Path)
    start = sub.add_parser("start-all")
    start.add_argument("job", type=Path)
    update = sub.add_parser("mark")
    update.add_argument("job", type=Path)
    update.add_argument("--task", required=True)
    update.add_argument("--status", choices=tuple(sorted(STATUSES)), required=True)
    update.add_argument("--artifact", type=Path)
    update.add_argument("--note")
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = plan(args.job)
        elif args.command == "start-all":
            result = start_all(args.job)
        else:
            result = mark(args.job, args.task, args.status, args.artifact, args.note)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ManifestError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
