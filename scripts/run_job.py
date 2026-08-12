#!/usr/bin/env python3
"""Keep one confirmed Movies Nerd job foreground through its ready handoff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import acquire
from acquire import ControllerError, JobLock, TerminalAcquisitionError, bounded_integer
from finalization_queue import required_tasks, start_all, task_state
from finalize_job import FinalizeError, finalize
from job_manifest import ManifestError, load_job
from qbittorrent_api import QbtAccessDenied, QbtError, QbtUnavailable


def ready_for_finalization(job: dict) -> bool:
    tasks = task_state(job)
    return all(tasks[name].get("status") == "complete" for name in required_tasks(job))


def run(
    job_path: Path, *, poll_seconds: int = 5,
    artifact_wait_seconds: int = 600,
) -> dict:
    checked, job = load_job(job_path)
    with JobLock(checked, str(job["job_id"])):
        transfer = acquire.run(
            checked, poll_seconds=poll_seconds, max_seconds=0, acquire_lock=False,
        )
        _, job = load_job(checked)
        if not job.get("enrichment_tasks"):
            start_all(checked)
        deadline = time.monotonic() + artifact_wait_seconds
        last_heartbeat = 0.0
        while True:
            _, job = load_job(checked)
            if ready_for_finalization(job):
                break
            now = time.monotonic()
            if now - last_heartbeat >= 30:
                last_heartbeat = now
                pending = [
                    name for name, item in task_state(job).items()
                    if item.get("status") != "complete"
                ]
                print(json.dumps({
                    "event": "preparing-library-entry",
                    "job": str(checked),
                    "remaining": pending,
                }), flush=True)
            if now >= deadline:
                return {
                    "ready": False,
                    "downloaded": bool(transfer.get("downloaded")),
                    "resumable": True,
                    "job": str(checked),
                    "next": "finish the already-requested preparation and resume this same job",
                }
            time.sleep(min(2, max(0.1, deadline - now)))
        return finalize(checked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument(
        "--poll-seconds", type=bounded_integer(2, 30, "poll seconds"),
        default=5, metavar="2..30",
    )
    parser.add_argument(
        "--artifact-wait-seconds",
        type=bounded_integer(0, 3600, "artifact wait seconds"),
        default=600, metavar="0..3600",
    )
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    if not args.commit:
        parser.error("the foreground workflow requires --commit after confirmation")
    try:
        result = run(
            args.job, poll_seconds=args.poll_seconds,
            artifact_wait_seconds=args.artifact_wait_seconds,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ready") else 8
    except QbtAccessDenied as exc:
        print(json.dumps({
            "error": str(exc), "needs_local_app_access": True,
            "user_action_required": False, "resumable": True,
        }), file=sys.stderr)
        return 6
    except QbtUnavailable as exc:
        print(json.dumps({"error": str(exc), "resumable": True}), file=sys.stderr)
        return 5
    except TerminalAcquisitionError as exc:
        print(json.dumps({"error": str(exc), "cleaned_up": True, "terminal": True}), file=sys.stderr)
        return 2
    except (ControllerError, FinalizeError, ManifestError, QbtError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "resumable": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
