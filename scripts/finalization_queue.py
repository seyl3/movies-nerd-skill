#!/usr/bin/env python3
"""Persist the independent finalization work that runs during a transfer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from _common import remove_appledouble_sibling, stage_for_kind
from job_manifest import ManifestError, load_job, update_job

MOVIE_TASKS = (
    "destination", "metadata", "artwork", "subtitle-en", "subtitle-fr",
    "film-links", "recommendations",
)
SERIES_TASKS = (
    "destination", "metadata", "artwork", "subtitle-en", "subtitle-fr",
)
STATUSES = {"pending", "requested", "running", "complete", "failed"}
ARTIFACT_TASKS = {"metadata", "artwork", "subtitle-en", "subtitle-fr"}


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
        "artifact_root": str(artifact_root(job)),
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
    root = artifact_root(job)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ManifestError("job artifact directory is unsafe")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    remove_appledouble_sibling(root)
    tasks = task_state(job)
    for item in tasks.values():
        if item.get("status") == "pending":
            item.update({"status": "requested", "requested_epoch": time.time()})
    update_job(checked, {
        "enrichment_tasks": tasks,
        "steps": {"enrichment": "running"},
        "artifacts": {"enrichment_requested_at": time.time()},
    })
    return plan(checked)


def artifact_root(job: dict) -> Path:
    return stage_for_kind(str(job["kind"])) / "jobs" / str(job["job_id"])


def checked_artifact(path: Path, job: dict) -> str:
    if path.is_symlink() or not path.is_file():
        raise ManifestError("prepared artifact must be a regular staged file")
    resolved = path.resolve(strict=True)
    root = artifact_root(job).resolve(strict=False)
    if root not in resolved.parents:
        raise ManifestError("prepared artifact must stay inside this job's artifact directory")
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
    if status == "complete" and task in ARTIFACT_TASKS and artifact is None:
        raise ManifestError(f"{task} needs a prepared artifact before completion")
    item = dict(tasks[task])
    item["status"] = status
    item["updated_epoch"] = time.time()
    if artifact:
        item["artifact"] = checked_artifact(artifact, job)
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
    show.add_argument("job_pos", nargs="?", type=Path)
    show.add_argument("--job", dest="job_opt", type=Path)
    start = sub.add_parser("start-all")
    start.add_argument("job_pos", nargs="?", type=Path)
    start.add_argument("--job", dest="job_opt", type=Path)
    update = sub.add_parser("mark")
    update.add_argument("job_pos", nargs="?", type=Path)
    update.add_argument("--job", dest="job_opt", type=Path)
    update.add_argument("--task", required=True)
    update.add_argument("--status", choices=tuple(sorted(STATUSES)), required=True)
    update.add_argument("--artifact", type=Path)
    update.add_argument("--note")
    args = parser.parse_args()
    try:
        job = args.job_opt or args.job_pos
        if not job:
            raise ManifestError("--job is required")
        if args.command == "plan":
            result = plan(job)
        elif args.command == "start-all":
            result = start_all(job)
        else:
            result = mark(job, args.task, args.status, args.artifact, args.note)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ManifestError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
