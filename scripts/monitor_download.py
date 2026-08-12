#!/usr/bin/env python3
"""Monitor real byte growth and request fast failover for unhealthy transfers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from statistics import median
import sys
import time

from job_manifest import ManifestError, load_job, transition_job, update_job
from qbittorrent_api import QbtError, connected_client, normalize_hash

CONTINUE_MONITORING = 8
DEFAULT_NO_PROGRESS_SECONDS = 60
DEFAULT_LOW_SPEED_SECONDS = 60
DEFAULT_LOW_SPEED_BPS = 256 * 1024
DEFAULT_METADATA_SECONDS = 30


@dataclass(frozen=True)
class Sample:
    epoch: float
    downloaded: int
    speed: int
    progress: float
    state: str
    peers: int
    availability: float


def sync_torrent(
    client, torrent_hash: str, rid: int = 0, current: dict | None = None,
) -> tuple[int, dict]:
    payload = client.json(f"sync/maindata?rid={rid}")
    if not isinstance(payload, dict):
        raise QbtError("qBittorrent returned invalid incremental sync data")
    try:
        next_rid = int(payload.get("rid"))
    except (TypeError, ValueError) as exc:
        raise QbtError("qBittorrent returned an invalid incremental status") from exc
    if next_rid < 0:
        raise QbtError("qBittorrent returned an invalid incremental status")
    normalized = normalize_hash(torrent_hash)
    removed = {str(value).lower() for value in payload.get("torrents_removed") or []}
    if normalized in removed:
        raise QbtError("torrent was removed during monitoring")
    merged = {} if payload.get("full_update") else dict(current or {})
    torrents = payload.get("torrents") or {}
    if not isinstance(torrents, dict):
        raise QbtError("qBittorrent returned invalid torrent status")
    delta = torrents.get(normalized)
    if delta is None:
        delta = torrents.get(normalized.upper())
    if delta is not None:
        if not isinstance(delta, dict):
            raise QbtError("qBittorrent returned an invalid torrent update")
        merged.update(delta)
    if not merged:
        raise QbtError("torrent is not present during monitoring")
    merged.setdefault("hash", normalized)
    return next_rid, merged


def _int(info: dict, field: str) -> int:
    try:
        return max(0, int(info.get(field, 0) or 0))
    except (TypeError, ValueError):
        return 0


def to_sample(info: dict, epoch: float | None = None) -> Sample:
    downloaded = _int(info, "downloaded_session") or _int(info, "downloaded")
    peers = sum(_int(info, field) for field in (
        "num_seeds", "num_leechs", "num_complete", "num_incomplete",
    ))
    try:
        progress = max(0.0, min(1.0, float(info.get("progress", 0) or 0)))
    except (TypeError, ValueError):
        progress = 0.0
    try:
        availability = max(0.0, float(info.get("availability", 0) or 0))
    except (TypeError, ValueError):
        availability = 0.0
    return Sample(
        epoch=time.time() if epoch is None else epoch,
        downloaded=downloaded,
        speed=_int(info, "dlspeed"),
        progress=progress,
        state=str(info.get("state") or "unknown"),
        peers=peers,
        availability=availability,
    )


def trim(samples: list[Sample], seconds: int) -> list[Sample]:
    if not samples:
        return []
    cutoff = samples[-1].epoch - seconds
    return [sample for sample in samples if sample.epoch >= cutoff]


def assess_samples(
    samples: list[Sample], source: str, *, standby_ready: bool = False,
    no_progress_seconds: int = DEFAULT_NO_PROGRESS_SECONDS,
    low_speed_seconds: int = DEFAULT_LOW_SPEED_SECONDS,
    low_speed_bps: int = DEFAULT_LOW_SPEED_BPS,
    metadata_seconds: int = DEFAULT_METADATA_SECONDS,
) -> dict:
    if not samples:
        raise QbtError("monitor has no transfer samples")
    latest = samples[-1]
    state = latest.state.casefold()
    complete = latest.progress >= 0.999999 and not any(
        token in state for token in ("checking", "moving", "allocating", "dl")
    )
    meta_window = trim(samples, metadata_seconds)
    meta_stuck = (
        "metadl" in state and len(meta_window) >= 2
        and meta_window[-1].epoch - meta_window[0].epoch >= metadata_seconds
    )
    progress_window = trim(samples, no_progress_seconds)
    no_progress = (
        len(progress_window) >= 2
        and progress_window[-1].epoch - progress_window[0].epoch >= no_progress_seconds
        and progress_window[-1].downloaded <= progress_window[0].downloaded
        and progress_window[-1].progress <= progress_window[0].progress
    )
    speed_window = trim(samples, low_speed_seconds)
    median_speed = int(median(sample.speed for sample in speed_window)) if speed_window else 0
    low_speed = (
        standby_ready and len(speed_window) >= 2
        and speed_window[-1].epoch - speed_window[0].epoch >= low_speed_seconds
        and median_speed < low_speed_bps
    )
    unavailable = latest.availability < 1.0 and latest.peers == 0
    stalled = not complete and (meta_stuck or no_progress or low_speed) and (
        standby_ready or unavailable or meta_stuck
    )
    reasons = []
    if meta_stuck:
        reasons.append("metadata did not arrive promptly")
    if no_progress:
        reasons.append("downloaded bytes did not increase")
    if low_speed:
        reasons.append("a faster validated backup is available")
    if unavailable:
        reasons.append("no useful swarm is connected")
    return {
        "source": source,
        "state": latest.state,
        "progress": latest.progress,
        "download_speed": latest.speed,
        "median_speed": median_speed,
        "downloaded": latest.downloaded,
        "known_peers": latest.peers,
        "availability": latest.availability,
        "complete": complete,
        "stalled": stalled,
        "reasons": reasons if stalled else [],
        "failover": {
            "required": stalled,
            "standby_ready": standby_ready,
            "replacement_available": standby_ready,
            "next": "probe the next confirmed candidate without asking again" if stalled else None,
        },
    }


def activity_age(info: dict, now: int) -> int | None:
    values = [_int(info, field) for field in ("last_activity", "added_on")]
    values = [value for value in values if value > 0]
    return max(0, now - max(values)) if values else None


def assess(info: dict, threshold: int, source: str) -> dict:
    """Compatibility helper for one-snapshot callers and simple diagnostics."""
    sample = to_sample(info)
    age = activity_age(info, int(sample.epoch))
    observed_age = max(0, age or 0)
    earlier_epoch = sample.epoch - (threshold if observed_age >= threshold else observed_age)
    earlier = Sample(
        earlier_epoch, sample.downloaded, 0, sample.progress,
        sample.state, sample.peers, sample.availability,
    )
    result = assess_samples(
        [earlier, sample], source,
        no_progress_seconds=threshold,
        low_speed_seconds=max(threshold, DEFAULT_LOW_SPEED_SECONDS),
        metadata_seconds=min(threshold, DEFAULT_METADATA_SECONDS),
    )
    result["activity_age_seconds"] = age
    result["hash"] = str(info.get("hash") or "")
    result["name"] = info.get("name")
    result["failover"]["exclude_source"] = source if result["stalled"] else None
    return result


def next_poll_interval(report: dict, configured: int) -> int:
    if report.get("download_speed", 0) <= 0 or "meta" in str(report.get("state") or "").casefold():
        return min(configured, 2)
    return configured


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hash", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--job")
    parser.add_argument("--watch-minutes", type=int, choices=range(0, 61), default=0)
    parser.add_argument("--interval", type=int, choices=range(2, 31), default=5)
    parser.add_argument("--no-progress-seconds", type=int, choices=range(30, 301), default=DEFAULT_NO_PROGRESS_SECONDS)
    parser.add_argument("--low-speed-seconds", type=int, choices=range(30, 301), default=DEFAULT_LOW_SPEED_SECONDS)
    parser.add_argument("--low-speed-kib", type=int, choices=range(1, 4097), default=DEFAULT_LOW_SPEED_BPS // 1024)
    parser.add_argument("--stop-on-stall", action="store_true")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        if args.stop_on_stall and not args.commit:
            raise QbtError("--stop-on-stall requires --commit")
        torrent_hash = normalize_hash(args.hash)
        client = connected_client()
        deadline = time.monotonic() + args.watch_minutes * 60
        rid = 0
        current = None
        samples: list[Sample] = []
        while True:
            rid, current = sync_torrent(client, torrent_hash, rid, current)
            samples.append(to_sample(current))
            samples = trim(samples, max(args.low_speed_seconds, args.no_progress_seconds, 120) + 10)
            standby_ready = False
            if args.job:
                try:
                    _, job = load_job(args.job)
                    standby_ready = bool((job.get("controller") or {}).get("standby_hash"))
                except (ManifestError, OSError):
                    pass
            report = assess_samples(
                samples, args.source, standby_ready=standby_ready,
                no_progress_seconds=args.no_progress_seconds,
                low_speed_seconds=args.low_speed_seconds,
                low_speed_bps=args.low_speed_kib * 1024,
            )
            report["sync_rid"] = rid
            if report["complete"]:
                if args.job:
                    transition_job(args.job, "downloaded", torrent_hash=torrent_hash)
                    update_job(args.job, {"controller": {"phase": "downloaded"}})
                report["monitoring"] = "complete"
                report["next"] = "finalize and clean the completed job"
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            if report["stalled"]:
                if args.stop_on_stall:
                    client.request("torrents/stop", {"hashes": torrent_hash})
                    report["stopped"] = True
                if args.job:
                    try:
                        transition_job(
                            args.job, "stalled",
                            reason="; ".join(report["reasons"]) or "transfer stalled",
                            torrent_hash=torrent_hash,
                        )
                    except (ManifestError, OSError):
                        pass
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 7
            if time.monotonic() >= deadline:
                report["monitoring"] = "continue"
                report["next"] = "continue the same job automatically"
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return CONTINUE_MONITORING
            time.sleep(min(next_poll_interval(report, args.interval), max(0, deadline - time.monotonic())))
    except (QbtError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
