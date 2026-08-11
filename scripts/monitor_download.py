#!/usr/bin/env python3
"""Monitor one qBittorrent transfer and emit a safe different-source failover signal."""

from __future__ import annotations

import argparse
import json
import sys
import time

from qbittorrent_api import QbtError, client_from_env, normalize_hash


def sync_torrent(
    client, torrent_hash: str, rid: int = 0, current: dict | None = None,
) -> tuple[int, dict]:
    payload = client.json(f"sync/maindata?rid={rid}")
    if not isinstance(payload, dict):
        raise QbtError("qBittorrent returned invalid incremental sync data")
    try:
        next_rid = int(payload.get("rid"))
    except (TypeError, ValueError) as exc:
        raise QbtError("qBittorrent returned an invalid sync response ID") from exc
    if next_rid < 0:
        raise QbtError("qBittorrent returned an invalid sync response ID")
    normalized = normalize_hash(torrent_hash)
    removed = {str(value).lower() for value in payload.get("torrents_removed") or []}
    if normalized in removed:
        raise QbtError("torrent was removed from qBittorrent during monitoring")
    merged = {} if payload.get("full_update") else dict(current or {})
    torrents = payload.get("torrents") or {}
    if not isinstance(torrents, dict):
        raise QbtError("qBittorrent returned invalid torrent sync data")
    delta = torrents.get(normalized)
    if delta is None:
        delta = torrents.get(normalized.upper())
    if delta is not None:
        if not isinstance(delta, dict):
            raise QbtError("qBittorrent returned an invalid torrent delta")
        merged.update(delta)
    if not merged:
        raise QbtError("torrent is not present in qBittorrent incremental data")
    merged.setdefault("hash", normalized)
    return next_rid, merged


def next_poll_interval(report: dict, configured: int) -> int:
    state = str(report.get("state") or "").lower()
    if report.get("download_speed", 0) <= 0 or any(
        token in state for token in ("meta", "checking", "queued")
    ):
        return min(configured, 15)
    return configured


def activity_age(info: dict, now: int) -> int | None:
    timestamps = []
    for field in ("last_activity", "added_on"):
        try:
            value = int(info.get(field, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            timestamps.append(value)
    return max(0, now - max(timestamps)) if timestamps else None


def assess(info: dict, threshold: int, source: str) -> dict:
    now = int(time.time())
    age = activity_age(info, now)
    state = str(info.get("state", "unknown"))
    state_lower = state.lower()
    progress = float(info.get("progress", 0) or 0)
    speed = int(info.get("dlspeed", 0) or 0)
    peer_fields = ("num_seeds", "num_leechs", "num_complete", "num_incomplete")
    known_peers = sum(max(0, int(info.get(field, 0) or 0)) for field in peer_fields)
    inactive_long_enough = age is not None and age >= threshold
    running_download = progress < 0.999 and not any(token in state_lower for token in ("paused", "stopped", "queued", "error"))
    stalled_state = "stalled" in state_lower or state_lower == "metadl"
    no_peers = known_peers == 0
    stalled = running_download and speed == 0 and inactive_long_enough and (stalled_state or no_peers)
    reasons = []
    if stalled_state:
        reasons.append(f"qBittorrent state is {state}")
    if no_peers:
        reasons.append("no seeds or peers are known")
    if inactive_long_enough:
        reasons.append(f"no activity for {age} seconds")
    return {
        "hash": str(info.get("hash", "")),
        "name": info.get("name"),
        "source": source,
        "state": state,
        "progress": progress,
        "download_speed": speed,
        "known_peers": known_peers,
        "activity_age_seconds": age,
        "stalled": stalled,
        "reasons": reasons if stalled else [],
        "failover": {
            "required": stalled,
            "exclude_source": source if stalled else None,
            "next": "stop and search one different approved source; rank and confirm the exact replacement" if stalled else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hash", required=True)
    parser.add_argument("--source", required=True, help="source host for the current release")
    parser.add_argument("--stall-minutes", type=int, choices=range(5, 181), default=20)
    parser.add_argument("--watch-minutes", type=int, choices=range(0, 61), default=0)
    parser.add_argument("--interval", type=int, choices=range(15, 301), default=60)
    parser.add_argument("--stop-on-stall", action="store_true")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        if args.stop_on_stall and not args.commit:
            raise QbtError("--stop-on-stall changes qBittorrent state and requires --commit")
        torrent_hash = normalize_hash(args.hash)
        client = client_from_env()
        deadline = time.monotonic() + args.watch_minutes * 60
        rid = 0
        current = None
        while True:
            rid, current = sync_torrent(client, torrent_hash, rid, current)
            report = assess(current, args.stall_minutes * 60, args.source)
            report["sync_rid"] = rid
            if report["stalled"]:
                if args.stop_on_stall:
                    client.request("torrents/stop", {"hashes": torrent_hash})
                    report["stopped"] = True
                    report["partial_data_preserved"] = True
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 7
            if time.monotonic() >= deadline:
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            interval = next_poll_interval(report, args.interval)
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
    except (QbtError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
