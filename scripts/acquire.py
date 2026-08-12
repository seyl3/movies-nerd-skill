#!/usr/bin/env python3
"""Run one authorized Movies Nerd transfer through probing, failover, and completion."""

from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import time

from _common import GIB, emit_event, remove_appledouble_sibling
from finalization_queue import start_all as request_finalization
from job_manifest import (
    ManifestError, load_job, remove_failed_job, transition_job, update_job,
)
from monitor_download import (
    DEFAULT_LOW_SPEED_BPS, DEFAULT_LOW_SPEED_SECONDS, DEFAULT_NO_PROGRESS_SECONDS,
    assess_samples, sync_torrent, to_sample, trim,
)
from provider_health import HealthError, record_hash
from qbittorrent_api import (
    QbtAccessDenied, QbtError, QbtUnavailable, command_start, connected_client,
    normalize_hash, preflight, remove_movies_nerd_torrent, torrent_info,
)
from race_candidates import (
    candidate_hash, pool_from_job, race, remove_and_verify, remove_quietly,
)
from search_releases import checked_query, release_selection, search_all


class ControllerError(RuntimeError):
    pass


class TerminalAcquisitionError(ControllerError):
    pass


def bounded_integer(minimum: int, maximum: int, label: str):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{label} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be between {minimum} and {maximum}"
            )
        return parsed
    return parse


class JobLock(AbstractContextManager):
    def __init__(self, job_path: Path, job_id: str):
        self.root = job_path.parent.parent
        self.path = self.root / "locks" / f"{job_id}.lock"

    def __enter__(self):
        if self.root.name != ".movies-nerd":
            raise ControllerError("job state is outside .movies-nerd")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        remove_appledouble_sibling(self.path.parent)
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise ControllerError("job lock is unsafe")
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                pid = int(value.get("pid", 0) or 0)
                if pid > 0:
                    os.kill(pid, 0)
                    raise ControllerError("this Movies Nerd job is already running")
            except ProcessLookupError:
                self.path.unlink(missing_ok=True)
                remove_appledouble_sibling(self.path)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                self.path.unlink(missing_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_epoch": time.time()}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        remove_appledouble_sibling(self.path)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.path.is_file() and not self.path.is_symlink():
            self.path.unlink(missing_ok=True)
        remove_appledouble_sibling(self.path)
        try:
            self.path.parent.rmdir()
        except OSError:
            pass
        else:
            remove_appledouble_sibling(self.path.parent)
        return False


def _ids(job: dict) -> dict:
    ids = (job.get("identity") or {}).get("ids") or {}
    return ids if isinstance(ids, dict) else {}


def _runtime(job: dict) -> float | None:
    for source in (job.get("identity") or {}, job.get("cache") or {}):
        try:
            value = float(source.get("runtime_minutes") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 1440:
            return value
    return None


def refresh_candidates(job_path: Path, job: dict, excluded: set[str]) -> list[dict]:
    identity = job["identity"]
    title = str(identity["title"])
    year = int(identity["year"])
    envelope = job.get("confirmation_envelope") or {}
    quality = str(envelope.get("quality") or "")
    max_bytes = int(envelope.get("max_size_bytes") or 15 * GIB)
    query = checked_query(title, year)
    imdb_id = str(_ids(job).get("imdb") or _ids(job).get("imdb_id") or "") or None
    results, reports, _ = search_all(
        query, 5.0, job["kind"] == "series", True,
        title=title, year=year, max_bytes=max_bytes,
        runtime_minutes=_runtime(job), imdb_id=imdb_id,
        excluded_hashes=excluded,
    )
    selection = release_selection(
        results, title, year, max_bytes, _runtime(job),
        kind=job["kind"], excluded_hashes=excluded,
    )
    candidates = [
        candidate for candidate in selection.get("candidates") or []
        if (not quality or candidate.get("resolution") == quality)
        and int(candidate.get("size_bytes") or 0) <= max_bytes
    ]
    if not candidates:
        raise ControllerError("no additional candidate was found inside the confirmed quality and size")
    update_job(job_path, {
        "candidate_pool": candidates,
        "cache": {
            "refresh_search": {
                "elapsed_ms": round(sum(int(report.get("latency_ms", 0) or 0) for report in reports.values())),
                "providers": len(reports),
            },
        },
        "controller": {"refreshes": int((job.get("controller") or {}).get("refreshes", 0) or 0) + 1},
    })
    return candidates


def start_race(job_path: Path, job: dict, client, excluded: set[str]) -> tuple[str, dict]:
    candidates = pool_from_job(job, excluded)
    identity = job["identity"]
    rename = f"{identity['title']} ({identity['year']})"
    winner, result = race(client, candidates, job["kind"], rename)
    active_hash = normalize_hash(result["winner_hash"])
    attempted = list(dict.fromkeys([
        *(job.get("controller") or {}).get("tried_hashes", []),
        *result.get("attempted_hashes", []),
    ]))
    transition_job(job_path, "downloading", torrent_hash=active_hash, release=winner)
    updated = update_job(job_path, {
        "backup_release": result.get("standby_release"),
        "controller": {
            "phase": "downloading",
            "attempt": int((job.get("controller") or {}).get("attempt", 0) or 0) + 1,
            "active_hash": active_hash,
            "standby_hash": result.get("standby_hash"),
            "tried_hashes": attempted,
            "race_outcomes": result.get("outcomes") or [],
            "last_comparison": result.get("comparison") or {},
            "last_progress_epoch": time.time(),
        },
        "steps": {"enrichment": "running"},
        "artifacts": {"enrichment_requested_at": time.time()},
    })
    return active_hash, updated


def known_candidate_hashes(job: dict) -> set[str]:
    values = set((job.get("controller") or {}).get("tried_hashes") or [])
    standby = (job.get("controller") or {}).get("standby_hash")
    if standby:
        values.add(str(standby))
    for release in [*(job.get("candidate_pool") or []), job.get("release"), job.get("backup_release")]:
        if not isinstance(release, dict):
            continue
        try:
            values.add(candidate_hash(release))
        except (QbtError, ValueError):
            pass
    result = set()
    for value in values:
        try:
            result.add(normalize_hash(str(value)))
        except QbtError:
            pass
    return result


def enforce_single_transfer(job_path: Path, job: dict, client, active_hash: str) -> dict:
    """Keep only the selected candidate, including when resuming a v2.0.0 job."""
    active = normalize_hash(active_hash)
    removed = []
    for info_hash in sorted(known_candidate_hashes(job) - {active}):
        try:
            torrent_info(client, info_hash)
        except QbtError as exc:
            if "not present" in str(exc):
                continue
            raise
        remove_and_verify(client, info_hash)
        removed.append(info_hash)
    controller = job.get("controller") or {}
    if removed or controller.get("standby_hash") or job.get("backup_release"):
        return update_job(job_path, {
            "backup_release": None,
            "controller": {
                "standby_hash": None,
                "single_transfer_checked_epoch": time.time(),
                "duplicate_hashes_removed": removed,
            },
        })
    return job


def replacement_available(job: dict) -> bool:
    tried = set((job.get("controller") or {}).get("tried_hashes") or [])
    for release in job.get("candidate_pool") or []:
        try:
            if candidate_hash(release) not in tried:
                return True
        except (QbtError, ValueError):
            continue
    return int((job.get("controller") or {}).get("refreshes", 0) or 0) < 1


def activate_standby(job_path: Path, job: dict, client, old_hash: str) -> tuple[str, dict] | None:
    controller = job.get("controller") or {}
    standby_hash = controller.get("standby_hash")
    release = job.get("backup_release")
    if not standby_hash or not isinstance(release, dict):
        return None
    standby_hash = normalize_hash(str(standby_hash))
    try:
        torrent_info(client, standby_hash)
    except QbtError:
        return None
    remove_and_verify(client, old_hash)
    transition_job(job_path, "replacement-started")
    command_start(client, argparse.Namespace(
        hash=standby_hash, commit=True, include_extras=False,
        allow_oversize=False, series=job["kind"] == "series",
    ))
    transition_job(job_path, "downloading", torrent_hash=standby_hash, release=release)
    updated = update_job(job_path, {
        "backup_release": None,
        "controller": {
            "phase": "downloading",
            "active_hash": standby_hash,
            "standby_hash": None,
            "last_progress_epoch": time.time(),
        },
    })
    try:
        record_hash(job["kind"], old_hash, "dead", provider=str((job.get("release") or {}).get("provider") or ""))
    except (HealthError, OSError, ValueError):
        pass
    return standby_hash, updated


def replace_active(job_path: Path, job: dict, client, old_hash: str) -> tuple[str, dict]:
    activated = activate_standby(job_path, job, client, old_hash)
    if activated:
        return activated
    remove_and_verify(client, old_hash)
    transition_job(job_path, "replacement-started")
    _, current = load_job(job_path)
    excluded = set((current.get("controller") or {}).get("tried_hashes") or [])
    try:
        return start_race(job_path, current, client, excluded)
    except (ManifestError, QbtError):
        refreshes = int((current.get("controller") or {}).get("refreshes", 0) or 0)
        if refreshes >= 1:
            raise
        refresh_candidates(job_path, current, excluded)
        _, refreshed = load_job(job_path)
        return start_race(job_path, refreshed, client, excluded)


def clean_terminal_failure(job_path: Path, job: dict, client, reason: str) -> None:
    hashes = set((job.get("controller") or {}).get("tried_hashes") or [])
    for candidate in job.get("candidate_pool") or []:
        try:
            hashes.add(candidate_hash(candidate))
        except (QbtError, ValueError):
            pass
    for info_hash in hashes:
        remove_quietly(client, info_hash)
    for info_hash in hashes:
        try:
            torrent_info(client, info_hash)
        except QbtError as exc:
            if "not present" in str(exc):
                continue
            raise ControllerError("terminal cleanup could not be verified") from exc
        raise ControllerError("terminal cleanup left an owned transfer active")
    try:
        transition_job(job_path, "failed", reason=reason)
    except ManifestError:
        update_job(job_path, {"state": "failed", "artifacts": {"failure_reason": reason[:1000]}})
    remove_failed_job(job_path)


def ensure_active(job_path: Path, job: dict, client) -> tuple[str, dict]:
    state = job.get("state")
    if state == "confirmed":
        transition_job(job_path, "metadata-started")
        _, current = load_job(job_path)
        return start_race(job_path, current, client, set())
    if state == "downloading":
        active = (job.get("controller") or {}).get("active_hash") or (job.get("artifacts") or {}).get("torrent_hash")
        if active:
            active = normalize_hash(str(active))
            try:
                torrent_info(client, active)
                return active, job
            except QbtError as exc:
                if "not present" not in str(exc):
                    raise
        transition_job(job_path, "stalled", reason="active transfer disappeared")
        _, current = load_job(job_path)
        return replace_active(job_path, current, client, active or "0" * 40)
    if state == "stalled":
        active = normalize_hash(str((job.get("controller") or {}).get("active_hash") or (job.get("artifacts") or {}).get("torrent_hash")))
        return replace_active(job_path, job, client, active)
    if state == "downloaded":
        active = normalize_hash(str((job.get("controller") or {}).get("active_hash") or (job.get("artifacts") or {}).get("torrent_hash")))
        return active, job
    raise ControllerError(f"job cannot be acquired while state is {state}")


def run(
    job_path: Path, *, poll_seconds: int, max_seconds: int = 0,
    acquire_lock: bool = True,
) -> dict:
    checked, job = load_job(job_path)
    lock = JobLock(checked, str(job["job_id"])) if acquire_lock else nullcontext()
    with lock:
        client = connected_client(wait_seconds=20)
        readiness = preflight(client)
        try:
            active_hash, job = ensure_active(checked, job, client)
        except (ControllerError, ManifestError, QbtError) as exc:
            marker = str(exc).casefold()
            if any(text in marker for text in (
                "no live swarm", "no eligible confirmed candidate",
                "no additional candidate", "no main video",
            )):
                _, latest = load_job(checked)
                clean_terminal_failure(checked, latest, client, str(exc))
                raise TerminalAcquisitionError(str(exc)) from exc
            raise
        job = enforce_single_transfer(checked, job, client, active_hash)
        if job.get("state") == "downloaded":
            return {"downloaded": True, "job": str(checked), "preflight": readiness}
        request_finalization(checked)
        identity = job["identity"]
        label = f"{identity['title']} ({identity['year']})"
        emit_event("download-started", title=label)
        deadline = time.monotonic() + max_seconds if max_seconds else None
        rid = 0
        current = None
        samples = []
        prior_downloaded = -1
        while True:
            rid, current = sync_torrent(client, active_hash, rid, current)
            samples.append(to_sample(current))
            samples = trim(samples, max(DEFAULT_LOW_SPEED_SECONDS, DEFAULT_NO_PROGRESS_SECONDS, 120) + 10)
            _, job = load_job(checked)
            now = time.time()
            if samples[-1].downloaded > prior_downloaded:
                prior_downloaded = samples[-1].downloaded
                update_job(checked, {
                    "controller": {"last_progress_epoch": now},
                })
            report = assess_samples(
                samples, str((job.get("release") or {}).get("source") or "selected source"),
                standby_ready=replacement_available(job),
                no_progress_seconds=DEFAULT_NO_PROGRESS_SECONDS,
                low_speed_seconds=DEFAULT_LOW_SPEED_SECONDS,
                low_speed_bps=DEFAULT_LOW_SPEED_BPS,
            )
            if report["complete"]:
                transition_job(checked, "downloaded", torrent_hash=active_hash)
                update_job(checked, {
                    "controller": {"phase": "downloaded", "last_progress_epoch": time.time()},
                })
                client.request("torrents/stop", {"hashes": active_hash})
                emit_event("download-completed", title=label)
                return {
                    "downloaded": True,
                    "job": str(checked),
                    "hash": active_hash,
                    "preflight": readiness,
                    "next": "finalize, import, and run strict cleanup before reporting ready",
                }
            if report["stalled"]:
                emit_event("download-stalled", title=label)
                transition_job(
                    checked, "stalled", reason="; ".join(report["reasons"]),
                    torrent_hash=active_hash,
                )
                _, stalled_job = load_job(checked)
                try:
                    active_hash, job = replace_active(checked, stalled_job, client, active_hash)
                except (ControllerError, ManifestError, QbtError) as exc:
                    marker = str(exc).casefold()
                    if any(text in marker for text in (
                        "no live swarm", "no eligible confirmed candidate",
                        "no additional candidate",
                    )):
                        _, latest = load_job(checked)
                        clean_terminal_failure(checked, latest, client, str(exc))
                        raise TerminalAcquisitionError(str(exc)) from exc
                    raise
                emit_event("source-replaced", title=label)
                rid = 0
                current = None
                samples = []
                continue
            if deadline is not None and time.monotonic() >= deadline:
                return {
                    "downloaded": False,
                    "job": str(checked),
                    "hash": active_hash,
                    "healthy": True,
                    "resumable": True,
                }
            time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument(
        "--poll-seconds", type=bounded_integer(2, 30, "poll seconds"), default=5,
        metavar="2..30",
    )
    parser.add_argument(
        "--max-seconds", type=bounded_integer(0, 86400, "maximum seconds"), default=0,
        metavar="0..86400", help="0 waits through completion",
    )
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    if not args.commit:
        parser.error("the acquisition controller requires --commit for the requested download")
    checked = None
    try:
        checked, _job = load_job(args.job)
        result = run(checked, poll_seconds=args.poll_seconds, max_seconds=args.max_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("downloaded") else 8
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
    except (ControllerError, ManifestError, QbtError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "resumable": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
