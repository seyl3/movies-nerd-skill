#!/usr/bin/env python3
"""Bounded provider and swarm-health memory for Movies Nerd."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

from _common import state_for_kind
from qbittorrent_api import QbtError, normalize_hash

SCHEMA = 1
MAX_BYTES = 256 * 1024
MAX_PROVIDERS = 64
MAX_HASHES = 512
DEAD_TTL_SECONDS = 72 * 60 * 60


class HealthError(ValueError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def cache_path(kind: str, environ: dict[str, str] | None = None, *, create: bool = True) -> Path:
    root = state_for_kind(kind, environ)
    if root.exists() and root.is_symlink():
        raise HealthError("Movies Nerd state root must not be a symlink")
    cache = root / "cache"
    if cache.exists() and cache.is_symlink():
        raise HealthError("Movies Nerd cache must not be a symlink")
    if create:
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        os.chmod(cache, 0o700)
    return cache / "provider-health.json"


def empty() -> dict:
    return {"schema": SCHEMA, "updated_at": now_utc(), "providers": {}, "hashes": {}}


def load(kind: str, environ: dict[str, str] | None = None) -> dict:
    path = cache_path(kind, environ, create=False)
    if not path.exists():
        return empty()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise HealthError("provider-health cache is unsafe or oversized")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise HealthError("provider-health cache is invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise HealthError("provider-health cache schema is unsupported")
    if not isinstance(value.get("providers"), dict) or not isinstance(value.get("hashes"), dict):
        raise HealthError("provider-health cache shape is invalid")
    return prune(value)


def prune(value: dict, now: float | None = None) -> dict:
    current = time.time() if now is None else now
    providers = value.get("providers") or {}
    hashes = value.get("hashes") or {}
    provider_items = sorted(
        ((str(key)[:100], item) for key, item in providers.items() if isinstance(item, dict)),
        key=lambda pair: float(pair[1].get("updated_epoch", 0) or 0),
        reverse=True,
    )[:MAX_PROVIDERS]
    hash_items = []
    for raw_hash, item in hashes.items():
        if not isinstance(item, dict):
            continue
        try:
            info_hash = normalize_hash(str(raw_hash))
        except (QbtError, ValueError):
            continue
        updated = float(item.get("updated_epoch", 0) or 0)
        if item.get("status") == "dead" and current - updated > DEAD_TTL_SECONDS:
            continue
        hash_items.append((info_hash, item))
    hash_items.sort(key=lambda pair: float(pair[1].get("updated_epoch", 0) or 0), reverse=True)
    return {
        "schema": SCHEMA,
        "updated_at": now_utc(),
        "providers": dict(provider_items),
        "hashes": dict(hash_items[:MAX_HASHES]),
    }


def atomic_write(kind: str, value: dict, environ: dict[str, str] | None = None) -> Path:
    path = cache_path(kind, environ, create=True)
    cleaned = prune(value)
    raw = (json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise HealthError("provider-health cache exceeds its size limit")
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temp, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)
    return path


def provider_key(value: object) -> str:
    key = " ".join(str(value or "unknown").strip().casefold().split())[:100]
    return key or "unknown"


def record_provider(
    kind: str, provider: str, *, ok: bool, latency_ms: int, results: int,
    environ: dict[str, str] | None = None,
) -> dict:
    value = load(kind, environ)
    key = provider_key(provider)
    current = dict(value["providers"].get(key) or {})
    attempts = max(0, int(current.get("attempts", 0) or 0)) + 1
    successes = max(0, int(current.get("successes", 0) or 0)) + (1 if ok else 0)
    previous_latency = max(0.0, float(current.get("latency_ms", latency_ms) or latency_ms))
    current.update({
        "attempts": attempts,
        "successes": successes,
        "latency_ms": round(previous_latency * 0.7 + max(0, latency_ms) * 0.3, 1),
        "last_results": max(0, int(results)),
        "updated_epoch": time.time(),
    })
    value["providers"][key] = current
    atomic_write(kind, value, environ)
    return current


def record_hash(
    kind: str, info_hash: str, status: str, *, provider: str | None = None,
    bytes_per_second: int = 0, environ: dict[str, str] | None = None,
) -> dict:
    if status not in {"healthy", "dead", "unknown"}:
        raise HealthError("hash status must be healthy, dead, or unknown")
    normalized = normalize_hash(info_hash)
    value = load(kind, environ)
    item = {
        "status": status,
        "provider": provider_key(provider),
        "bytes_per_second": max(0, int(bytes_per_second)),
        "updated_epoch": time.time(),
    }
    value["hashes"][normalized] = item
    atomic_write(kind, value, environ)
    return item


def dead_hashes(kind: str, environ: dict[str, str] | None = None) -> set[str]:
    value = load(kind, environ)
    return {
        info_hash for info_hash, item in value["hashes"].items()
        if isinstance(item, dict) and item.get("status") == "dead"
    }


def provider_bonus(kind: str, provider: str, environ: dict[str, str] | None = None) -> float:
    item = load(kind, environ)["providers"].get(provider_key(provider)) or {}
    attempts = max(0, int(item.get("attempts", 0) or 0))
    if attempts == 0:
        return 0.0
    successes = max(0, int(item.get("successes", 0) or 0))
    success_rate = (successes + 1) / (attempts + 2)
    latency = max(1.0, float(item.get("latency_ms", 1000) or 1000))
    return round((success_rate - 0.5) * 20.0 - min(8.0, math.log2(latency / 250 + 1)), 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("movie", "series"), required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        value = load(args.kind)
        if args.json:
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            print(f"{len(value['providers'])} providers, {len(value['hashes'])} recent swarms")
        return 0
    except (HealthError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
