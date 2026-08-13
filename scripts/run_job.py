#!/usr/bin/env python3
"""Keep one authorized Movies Nerd job foreground through its ready handoff."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import json
from pathlib import Path
import sys
import threading

from _common import emit_event
import acquire
from acquire import ControllerError, JobLock, TerminalAcquisitionError, bounded_integer
from finalize_job import FinalizeError, finalize
from finalize_series import finalize as finalize_series
from job_manifest import ManifestError, load_job
import prepare_artifacts
from prepare_artifacts import ArtifactPreparationError
from qbittorrent_api import QbtAccessDenied, QbtError, QbtUnavailable


def run(
    job_path: Path, *, poll_seconds: int = 5,
    artifact_wait_seconds: int = 600,
) -> dict:
    checked, job = load_job(job_path)
    with JobLock(checked, str(job["job_id"])):
        stop_preparation = threading.Event()
        preparer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="movies-nerd-artifacts")
        prepared = preparer.submit(
            prepare_artifacts.prepare_when_started, checked, stop_preparation,
        )
        try:
            transfer = acquire.run(
                checked, poll_seconds=poll_seconds, max_seconds=0, acquire_lock=False,
            )
        except Exception:
            stop_preparation.set()
            prepared.cancel()
            preparer.shutdown(wait=False, cancel_futures=True)
            raise
        _, job = load_job(checked)
        identity = job["identity"]
        label = f"{identity['title']} ({identity['year']})"
        emit_event("finalization-started", title=label)
        try:
            prepared.result(timeout=artifact_wait_seconds or None)
        except FutureTimeout:
            stop_preparation.set()
            return {
                "ready": False,
                "downloaded": bool(transfer.get("downloaded")),
                "resumable": True,
                "job": str(checked),
                "next": "resume the automated finalization preparation",
            }
        finally:
            preparer.shutdown(wait=False, cancel_futures=True)
        _, job = load_job(checked)
        result = finalize(checked) if job.get("kind") == "movie" else finalize_series(checked)
        if result.get("ready"):
            ready_details = {
                key: result[key]
                for key in (
                    "destination",
                    "quality",
                    "letterboxd",
                    "senscritique",
                    "recommendations",
                    "episodes",
                    "seasons",
                )
                if result.get(key) is not None
            }
            emit_event("ready", title=label, **ready_details)
            result = {"event": "ready", **result}
        return result


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
        parser.error("the foreground workflow requires --commit for the requested download")
    try:
        result = run(
            args.job, poll_seconds=args.poll_seconds,
            artifact_wait_seconds=args.artifact_wait_seconds,
        )
        if not result.get("ready"):
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
    except (
        ArtifactPreparationError, ControllerError, FinalizeError, ManifestError,
        QbtError, OSError, ValueError,
    ) as exc:
        print(json.dumps({"error": str(exc), "resumable": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
