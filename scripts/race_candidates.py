#!/usr/bin/env python3
"""Privately race up to three confirmed equivalents and start the first healthy winner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from job_manifest import (
    ManifestError,
    archive_failed_job,
    checked_release,
    load_job,
    transition_job,
)
from qbittorrent_api import (
    QbtAccessDenied,
    QbtError,
    QbtUnavailable,
    command_add,
    command_start,
    connected_client,
    magnet_hash,
    remove_movies_nerd_torrent,
    torrent_files,
    torrent_info,
    normalize_hash,
    validate_torrent,
)

MAX_CANDIDATES = 3
METADATA_LIMIT = 1024


def pool_from_job(job: dict, excluded: set[str] | None = None) -> list[dict]:
    raw = job.get("candidate_pool")
    if not isinstance(raw, list) or not raw:
        raw = [value for value in (job.get("release"), job.get("backup_release")) if value]
    candidates = []
    hashes = set()
    excluded = excluded or set()
    for value in raw[:MAX_CANDIDATES]:
        candidate = checked_release(value)
        info_hash = magnet_hash(candidate["magnet"])
        if info_hash in hashes or info_hash in excluded:
            continue
        hashes.add(info_hash)
        candidates.append(candidate)
    if not candidates:
        raise ManifestError("job has no eligible confirmed candidate")
    return candidates


def health_key(client, candidate: dict) -> tuple:
    info = torrent_info(client, magnet_hash(candidate["magnet"]))
    peers = sum(
        max(0, int(info.get(name, 0) or 0))
        for name in ("num_seeds", "num_leechs", "num_complete", "num_incomplete")
    )
    speed = max(0, int(info.get("dlspeed", 0) or 0))
    return (speed > 0, peers > 0, speed, peers, float(candidate.get("score", 0) or 0))


def remove_quietly(client, info_hash: str) -> None:
    try:
        remove_movies_nerd_torrent(client, info_hash)
    except QbtError:
        pass


def _race(client, candidates: list[dict], kind: str, rename: str, wait: int, settle: int) -> tuple[dict, dict]:
    added = []
    try:
        for candidate in candidates:
            info_hash = magnet_hash(candidate["magnet"])
            command_add(client, SimpleNamespace(
                magnet=candidate["magnet"], kind=kind, rename=rename, commit=True,
            ))
            added.append(info_hash)
            client.request("torrents/setDownloadLimit", {"hashes": info_hash, "limit": str(METADATA_LIMIT)})
            client.request("torrents/start", {"hashes": info_hash})
    except QbtError:
        for info_hash in added:
            remove_quietly(client, info_hash)
        raise

    deadline = time.monotonic() + wait
    first_ready_at = None
    ready: dict[str, tuple[dict, dict]] = {}
    rejected = set()
    while time.monotonic() < deadline:
        for candidate in candidates:
            info_hash = magnet_hash(candidate["magnet"])
            if info_hash in ready or info_hash in rejected:
                continue
            if not torrent_files(client, info_hash):
                continue
            try:
                info, _files = validate_torrent(client, info_hash, series=kind == "series")
            except QbtError:
                rejected.add(info_hash)
                remove_quietly(client, info_hash)
                continue
            ready[info_hash] = (candidate, info)
            first_ready_at = first_ready_at or time.monotonic()
        active_count = len(ready) + len(rejected)
        if ready and (
            time.monotonic() - first_ready_at >= settle
            or active_count == len(candidates)
        ):
            break
        time.sleep(1)

    for info_hash in added:
        if info_hash in rejected:
            continue
        try:
            client.request("torrents/stop", {"hashes": info_hash})
        except QbtError:
            pass
    if not ready:
        for info_hash in added:
            remove_quietly(client, info_hash)
        raise QbtError("none of the confirmed equivalents became available")

    winner_hash, (winner, winner_info) = max(
        ready.items(), key=lambda item: health_key(client, item[1][0]),
    )
    for info_hash in added:
        if info_hash != winner_hash:
            remove_quietly(client, info_hash)
    client.request("torrents/setDownloadLimit", {"hashes": winner_hash, "limit": "0"})
    started = command_start(client, SimpleNamespace(
        hash=winner_hash,
        commit=True,
        include_extras=False,
        allow_oversize=False,
        series=kind == "series",
    ))
    actual = {
        "state": winner_info.get("state"),
        "connected_seeders": max(0, int(winner_info.get("num_seeds", 0) or 0)),
        "connected_peers": max(0, int(winner_info.get("num_leechs", 0) or 0)),
    }
    return winner, {**started, "actual_health": actual, "candidates_tried": len(candidates)}


def race(client, candidates: list[dict], kind: str, rename: str, wait: int, settle: int) -> tuple[dict, dict]:
    try:
        return _race(client, candidates, kind, rename, wait, settle)
    except (QbtError, OSError):
        for candidate in candidates:
            remove_quietly(client, magnet_hash(candidate["magnet"]))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--wait", type=int, choices=range(15, 181), default=75)
    parser.add_argument("--settle", type=int, choices=range(0, 31), default=5)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--replace-hash", help="silently replace a stalled winner from the confirmed pool")
    args = parser.parse_args()
    if not args.commit:
        parser.error("the hidden candidate race requires --commit after user confirmation")
    try:
        job_path, job = load_job(args.job)
        client = connected_client(wait_seconds=20)
        excluded = set()
        if args.replace_hash:
            old_hash = normalize_hash(args.replace_hash)
            if job.get("state") not in {"downloading", "stalled"}:
                raise ManifestError("only an active or stalled job can replace its winner")
            remove_movies_nerd_torrent(client, old_hash)
            excluded.add(old_hash)
            transition_job(job_path, "replacement-started")
        else:
            if job.get("state") != "confirmed" or job.get("steps", {}).get("confirmation") != "complete":
                raise ManifestError("job must record the user's confirmation before candidate racing")
            transition_job(job_path, "metadata-started")
        candidates = pool_from_job(job, excluded)
        identity = job["identity"]
        rename = f"{identity['title']} ({identity['year']})"
        winner, result = race(client, candidates, job["kind"], rename, args.wait, args.settle)
        info_hash = magnet_hash(winner["magnet"])
        transition_job(
            job_path, "downloading", torrent_hash=info_hash, release=winner,
        )
        print(json.dumps({
            "started": True,
            "winner": {
                key: winner.get(key)
                for key in ("title", "source", "provider", "size", "resolution")
            },
            "transfer": result,
            "job": str(Path(job_path)),
        }, ensure_ascii=False, indent=2))
        return 0
    except QbtAccessDenied as exc:
        print(json.dumps({
            "error": str(exc),
            "needs_local_app_access": True,
            "user_action_required": False,
        }, indent=2), file=sys.stderr)
        return 6
    except QbtUnavailable as exc:
        print(json.dumps({
            "error": str(exc),
            "qbit_app_not_ready": True,
            "job_preserved_for_retry": True,
        }, indent=2), file=sys.stderr)
        return 5
    except (ManifestError, QbtError, OSError, ValueError) as exc:
        try:
            transition_job(args.job, "metadata-failed", reason=str(exc))
            archive_failed_job(args.job)
        except (ManifestError, OSError):
            pass
        print(json.dumps({
            "error": str(exc),
            "cleaned_up": True,
            "failed_job_removed_from_incoming": True,
        }, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
