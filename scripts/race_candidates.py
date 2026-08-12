#!/usr/bin/env python3
"""Privately compare authorized candidates, keep the best, and remove duplicates."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
from statistics import median
import sys
import time

from job_manifest import (
    ManifestError, checked_release, load_job, remove_failed_job,
    transition_job, update_job,
)
from provider_health import HealthError, record_hash
from qbittorrent_api import (
    QbtAccessDenied, QbtError, QbtUnavailable, add_candidate, command_start,
    configure_selection, connected_client, magnet_hash, normalize_hash,
    remove_movies_nerd_torrent, torrent_files, torrent_info,
)
from torrent_metadata import TorrentMetadataError, fetch_torrent, inspect_torrent

MAX_SIMULTANEOUS = 3
MAX_WAVES = 2
DEFAULT_METADATA_SECONDS = 25
DEFAULT_PROBE_SECONDS = 20
PROBE_LIMIT = 2 * 1024 * 1024
PROBE_TARGET_BYTES = 16 * 1024 * 1024
POLL_SECONDS = 2


@dataclass
class Probe:
    candidate: dict
    info_hash: str
    metadata_seconds: float
    source_type: str
    initial_downloaded: int = 0
    bytes_delta: int = 0
    speeds: list[int] = field(default_factory=list)
    availability: float = 0.0
    peers: int = 0
    state: str = "unknown"

    @property
    def median_speed(self) -> int:
        return int(median(self.speeds)) if self.speeds else 0

    @property
    def live(self) -> bool:
        return self.bytes_delta > 0 or self.median_speed > 0 or (
            self.availability >= 1.0 and self.peers > 0
        )

    def score(self) -> tuple:
        return (
            self.live,
            self.bytes_delta > 0,
            self.median_speed,
            self.bytes_delta,
            self.availability,
            self.peers,
            -self.metadata_seconds,
            float(self.candidate.get("score", 0) or 0),
        )

    def report(self) -> dict:
        return {
            "hash": self.info_hash,
            "provider": self.candidate.get("provider"),
            "source_type": self.source_type,
            "metadata_ms": round(self.metadata_seconds * 1000),
            "downloaded_probe_bytes": self.bytes_delta,
            "median_speed": self.median_speed,
            "availability": round(self.availability, 3),
            "connected_peers": self.peers,
            "live": self.live,
        }


def candidate_hash(candidate: dict) -> str:
    raw = candidate.get("info_hash")
    return normalize_hash(str(raw)) if raw else magnet_hash(str(candidate.get("magnet") or ""))


def pool_from_job(job: dict, excluded: set[str] | None = None) -> list[dict]:
    raw = job.get("candidate_pool") or [job.get("release"), job.get("backup_release")]
    if not isinstance(raw, list):
        raise ManifestError("job candidate pool is invalid")
    excluded = excluded or set()
    envelope = job.get("confirmation_envelope") or {}
    quality = str(envelope.get("quality") or "")
    try:
        max_size = int(envelope.get("max_size_bytes") or 0)
    except (TypeError, ValueError):
        max_size = 0
    candidates = []
    hashes = set()
    for value in raw[:MAX_SIMULTANEOUS * MAX_WAVES]:
        if not value:
            continue
        candidate = checked_release(value)
        info_hash = candidate_hash(candidate)
        if info_hash in hashes or info_hash in excluded:
            continue
        if quality and candidate.get("resolution") != quality:
            continue
        if max_size and int(candidate.get("size_bytes") or 0) > max_size:
            continue
        hashes.add(info_hash)
        candidates.append(candidate)
    if not candidates:
        raise ManifestError("job has no eligible confirmed candidate")
    return candidates


def remove_quietly(client, info_hash: str) -> None:
    try:
        remove_movies_nerd_torrent(client, info_hash)
    except QbtError:
        pass


def remove_and_verify(client, info_hash: str) -> None:
    """Permanently retire one exact owned candidate and prove it is absent."""
    normalized = normalize_hash(info_hash)
    try:
        remove_movies_nerd_torrent(client, normalized)
    except QbtError as exc:
        if "not present" not in str(exc):
            raise
    try:
        torrent_info(client, normalized)
    except QbtError as exc:
        if "not present" in str(exc):
            return
        raise
    raise QbtError("duplicate candidate remained in qBittorrent after removal")


def existing_candidate(client, info_hash: str) -> bool:
    try:
        torrent_info(client, info_hash)
        return True
    except QbtError as exc:
        if "not present" in str(exc):
            return False
        raise


def add_one(client, candidate: dict, kind: str, rename: str) -> str:
    info_hash = candidate_hash(candidate)
    if existing_candidate(client, info_hash):
        return "resumed"
    torrent_url = candidate.get("torrent_url")
    if torrent_url:
        try:
            raw = fetch_torrent(str(torrent_url), timeout=5.0)
            inspect_torrent(raw, info_hash)
            return add_candidate(
                client, info_hash=info_hash, kind=kind, rename=rename,
                torrent_data=raw,
            )["source_type"]
        except (TorrentMetadataError, OSError):
            pass
    return add_candidate(
        client, info_hash=info_hash, kind=kind, rename=rename,
        magnet=str(candidate["magnet"]),
    )["source_type"]


def _downloaded(info: dict) -> int:
    for field in ("downloaded_session", "downloaded"):
        try:
            value = int(info.get(field, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _peers(info: dict) -> int:
    total = 0
    for field in ("num_seeds", "num_leechs", "num_complete", "num_incomplete"):
        try:
            total += max(0, int(info.get(field, 0) or 0))
        except (TypeError, ValueError):
            pass
    return total


def prepare_wave(
    client, candidates: list[dict], kind: str, rename: str,
    metadata_seconds: int,
) -> tuple[list[Probe], list[dict]]:
    started = time.monotonic()
    pending: dict[str, tuple[dict, str]] = {}
    rejected = []
    for candidate in candidates:
        info_hash = candidate_hash(candidate)
        try:
            source_type = add_one(client, candidate, kind, rename)
            client.request("torrents/setDownloadLimit", {"hashes": info_hash, "limit": str(PROBE_LIMIT)})
            client.request("torrents/setForceStart", {"hashes": info_hash, "value": "true"})
            client.request("torrents/start", {"hashes": info_hash})
            pending[info_hash] = (candidate, source_type)
        except QbtError as exc:
            remove_quietly(client, info_hash)
            rejected.append({"hash": info_hash, "reason": str(exc)})

    ready: list[Probe] = []
    deadline = started + metadata_seconds
    while pending and time.monotonic() < deadline:
        for info_hash, (candidate, source_type) in list(pending.items()):
            try:
                if not torrent_files(client, info_hash):
                    continue
                _info, _files, selected_size = configure_selection(
                    client, info_hash, series=kind == "series",
                )
                expected_size = int(candidate.get("size_bytes") or 0)
                tolerance = max(256 * 1024 ** 2, int(expected_size * 0.25))
                if expected_size and abs(selected_size - expected_size) > tolerance:
                    raise QbtError("candidate metadata size materially differs from the confirmed release")
                info = torrent_info(client, info_hash)
                ready.append(Probe(
                    candidate=candidate,
                    info_hash=info_hash,
                    metadata_seconds=time.monotonic() - started,
                    source_type=source_type,
                    initial_downloaded=_downloaded(info),
                ))
                pending.pop(info_hash)
            except QbtError as exc:
                pending.pop(info_hash)
                remove_quietly(client, info_hash)
                rejected.append({"hash": info_hash, "reason": str(exc)})
        if pending:
            time.sleep(1)
    for info_hash in list(pending):
        remove_quietly(client, info_hash)
        rejected.append({"hash": info_hash, "reason": "metadata deadline exceeded"})
    return ready, rejected


def probe_wave(client, probes: list[Probe], seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        all_bounded = True
        for probe in probes:
            try:
                info = torrent_info(client, probe.info_hash)
            except QbtError:
                continue
            current = _downloaded(info)
            probe.bytes_delta = max(probe.bytes_delta, max(0, current - probe.initial_downloaded))
            try:
                speed = max(0, int(info.get("dlspeed", 0) or 0))
            except (TypeError, ValueError):
                speed = 0
            probe.speeds.append(speed)
            try:
                probe.availability = max(probe.availability, float(info.get("availability", 0) or 0))
            except (TypeError, ValueError):
                pass
            probe.peers = max(probe.peers, _peers(info))
            probe.state = str(info.get("state") or "unknown")
            if probe.bytes_delta < PROBE_TARGET_BYTES:
                all_bounded = False
        if all_bounded:
            break
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    for probe in probes:
        try:
            client.request("torrents/stop", {"hashes": probe.info_hash})
            client.request("torrents/setForceStart", {"hashes": probe.info_hash, "value": "false"})
            client.request("torrents/setDownloadLimit", {"hashes": probe.info_hash, "limit": "0"})
        except QbtError:
            pass


def record_outcome(kind: str, probe: Probe) -> None:
    try:
        record_hash(
            kind, probe.info_hash, "healthy" if probe.live else "dead",
            provider=str(probe.candidate.get("provider") or ""),
            bytes_per_second=probe.median_speed,
        )
    except (HealthError, OSError, ValueError):
        pass


def race(
    client, candidates: list[dict], kind: str, rename: str,
    metadata_seconds: int = DEFAULT_METADATA_SECONDS,
    probe_seconds: int = DEFAULT_PROBE_SECONDS,
) -> tuple[dict, dict]:
    outcomes = []
    attempted = []
    for wave_number in range(MAX_WAVES):
        wave = candidates[wave_number * MAX_SIMULTANEOUS:(wave_number + 1) * MAX_SIMULTANEOUS]
        if not wave:
            break
        attempted.extend(candidate_hash(candidate) for candidate in wave)
        ready, rejected = prepare_wave(client, wave, kind, rename, metadata_seconds)
        outcomes.extend({**item, "live": False} for item in rejected)
        if not ready:
            continue
        probe_wave(client, ready, probe_seconds)
        for probe in ready:
            record_outcome(kind, probe)
            outcomes.append(probe.report())
        live = sorted((probe for probe in ready if probe.live), key=lambda probe: probe.score(), reverse=True)
        if not live:
            for probe in ready:
                remove_quietly(client, probe.info_hash)
            continue

        winner = live[0]
        removed_hashes = []
        for probe in ready:
            if probe.info_hash != winner.info_hash:
                remove_and_verify(client, probe.info_hash)
                removed_hashes.append(probe.info_hash)
        result = command_start(client, argparse.Namespace(
            hash=winner.info_hash,
            commit=True,
            include_extras=False,
            allow_oversize=False,
            series=kind == "series",
        ))
        return winner.candidate, {
            **result,
            "winner_hash": winner.info_hash,
            "standby_hash": None,
            "standby_release": None,
            "comparison": {
                "window_seconds": probe_seconds,
                "healthy_candidates": len(live),
                "kept_hash": winner.info_hash,
                "removed_hashes": removed_hashes,
            },
            "probe": winner.report(),
            "outcomes": outcomes,
            "attempted_hashes": attempted,
            "wave": wave_number + 1,
        }

    raise QbtError("authorized candidates had no live swarm during bounded probes")


def cleanup_attempts(client, hashes: list[str]) -> bool:
    clean = True
    for info_hash in hashes:
        remove_quietly(client, info_hash)
        try:
            torrent_info(client, info_hash)
            clean = False
        except QbtError as exc:
            if "not present" not in str(exc):
                clean = False
    return clean


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--metadata-seconds", type=int, choices=range(10, 61), default=DEFAULT_METADATA_SECONDS)
    parser.add_argument("--probe-seconds", type=int, choices=range(5, 61), default=DEFAULT_PROBE_SECONDS)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--replace-hash", help="replace a stalled winner inside the confirmed envelope")
    args = parser.parse_args()
    if not args.commit:
        parser.error("candidate probing requires --commit for the requested download")
    attempted: list[str] = []
    client = None
    try:
        job_path, job = load_job(args.job)
        client = connected_client(wait_seconds=20)
        excluded = set((job.get("controller") or {}).get("tried_hashes") or [])
        if args.replace_hash:
            old_hash = normalize_hash(args.replace_hash)
            if job.get("state") not in {"downloading", "stalled"}:
                raise ManifestError("only an active or stalled job can replace its winner")
            remove_quietly(client, old_hash)
            excluded.add(old_hash)
            transition_job(job_path, "replacement-started")
        else:
            if job.get("state") != "confirmed" or job.get("steps", {}).get("confirmation") != "complete":
                raise ManifestError("job must be authorized by an explicit download request")
            transition_job(job_path, "metadata-started")
        candidates = pool_from_job(job, excluded)
        attempted = [candidate_hash(candidate) for candidate in candidates]
        identity = job["identity"]
        rename = f"{identity['title']} ({identity['year']})"
        winner, result = race(
            client, candidates, job["kind"], rename,
            args.metadata_seconds, args.probe_seconds,
        )
        active_hash = result["winner_hash"]
        tried = list(dict.fromkeys([*excluded, *result["attempted_hashes"]]))
        transition_job(job_path, "downloading", torrent_hash=active_hash, release=winner)
        update_job(job_path, {
            "backup_release": result.get("standby_release"),
            "controller": {
                "phase": "downloading",
                "attempt": int((job.get("controller") or {}).get("attempt", 0) or 0) + 1,
                "active_hash": active_hash,
                "standby_hash": result.get("standby_hash"),
                "tried_hashes": tried,
                "race_outcomes": result["outcomes"],
                "last_comparison": result.get("comparison") or {},
                "last_progress_epoch": time.time(),
            },
            "steps": {"enrichment": "running"},
            "artifacts": {"enrichment_requested_at": time.time()},
        })
        print(json.dumps({
            "started": True,
            "winner": {key: winner.get(key) for key in ("title", "source", "provider", "size", "resolution")},
            "single_transfer": True,
            "transfer": {key: result.get(key) for key in ("hash", "selected_size", "probe", "wave")},
            "job": str(Path(job_path)),
        }, ensure_ascii=False, indent=2))
        return 0
    except QbtAccessDenied as exc:
        print(json.dumps({
            "error": str(exc), "needs_local_app_access": True,
            "user_action_required": False, "job_preserved_for_retry": True,
        }, indent=2), file=sys.stderr)
        return 6
    except QbtUnavailable as exc:
        print(json.dumps({
            "error": str(exc), "qbit_app_not_ready": True,
            "job_preserved_for_retry": True,
        }, indent=2), file=sys.stderr)
        return 5
    except (ManifestError, QbtError, OSError, ValueError) as exc:
        cleaned = client is not None and cleanup_attempts(client, attempted)
        try:
            transition_job(args.job, "metadata-failed", reason=str(exc))
            if cleaned:
                remove_failed_job(args.job)
        except (ManifestError, OSError):
            pass
        print(json.dumps({
            "error": str(exc), "cleaned_up": cleaned,
            "failed_job_state_removed": cleaned,
            "resumable": not cleaned,
        }, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
