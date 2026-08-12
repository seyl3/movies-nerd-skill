#!/usr/bin/env python3
"""Verify an import, then leave no completed Movies Nerd job artifacts behind."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import sys

from _common import clean_appledouble_tree, library_roots, staging_roots, state_roots
from job_manifest import ManifestError, load_job
from payload_safety import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from qbittorrent_api import QbtError, connected_client, torrent_info


def library_for(destination: Path) -> tuple[Path, Path, Path]:
    resolved = destination.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("final destination must be a regular library directory")
    for root, stage, state in zip(library_roots(), staging_roots(), state_roots()):
        fixed = root.resolve(strict=False)
        if fixed in resolved.parents and stage.resolve(strict=False) not in resolved.parents:
            return fixed, stage.resolve(strict=False), state.resolve(strict=False)
    raise ValueError("final destination must be inside the selected library and outside staging")


def verify_final_destination(destination: Path) -> dict:
    videos = []
    metadata = []
    artwork = []
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise ValueError("final destination contains a symlink")
        if not path.is_file():
            continue
        if path.name == ".DS_Store" or path.name.startswith("._"):
            continue
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("final destination contains a special file")
        suffix = path.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            videos.append(path)
        elif suffix == ".nfo":
            metadata.append(path)
        elif suffix in IMAGE_EXTENSIONS:
            artwork.append(path)
    if not videos or not metadata or not artwork:
        raise ValueError("final destination is not fully organized with media, metadata, and artwork")
    return {"videos": len(videos), "metadata": len(metadata), "artwork": len(artwork)}


def checked_targets(raw_targets: list[Path], stage: Path) -> list[Path]:
    targets = []
    for raw in raw_targets:
        if raw.is_symlink() or not raw.exists():
            raise ValueError("staged cleanup target must exist and must not be a symlink")
        resolved = raw.resolve(strict=True)
        if resolved == stage or stage not in resolved.parents:
            raise ValueError("staged cleanup target must be a job-specific path inside staging")
        targets.append(resolved)
        sidecar = resolved.with_name("._" + resolved.name)
        if sidecar.exists():
            if sidecar.is_symlink() or not sidecar.is_file():
                raise ValueError("staged AppleDouble sidecar is not a regular file")
            targets.append(sidecar.resolve(strict=True))
    unique = sorted(set(targets), key=lambda path: (len(path.parts), str(path)))
    for index, target in enumerate(unique):
        if any(parent == target or parent in target.parents for parent in unique[:index]):
            raise ValueError("staged cleanup targets must not overlap")
    return unique


def checked_state_target(path: Path, state: Path, *, allow_missing: bool = True) -> Path:
    resolved = path.resolve(strict=False)
    if resolved == state or state not in resolved.parents:
        raise ValueError("job state target is outside .movies-nerd")
    if path.is_symlink():
        raise ValueError("job state target must not be a symlink")
    if not allow_missing and not path.exists():
        raise ValueError("job state target does not exist")
    return resolved


def remove_path(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("cleanup refuses symlinks")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def prune_empty(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts), reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            continue
        sidecar = directory.with_name("._" + directory.name)
        if sidecar.is_file() and not sidecar.is_symlink():
            sidecar.unlink(missing_ok=True)


def prune_staging_root(stage: Path) -> None:
    for directory in (stage, stage.parent):
        try:
            directory.rmdir()
        except OSError:
            continue
        sidecar = directory.with_name("._" + directory.name)
        if sidecar.is_file() and not sidecar.is_symlink():
            sidecar.unlink(missing_ok=True)


def verify_recorded_torrents_absent(job: dict) -> dict:
    controller = job.get("controller") or {}
    hashes = {
        str(value) for value in (
            controller.get("active_hash"), controller.get("standby_hash"),
            (job.get("artifacts") or {}).get("torrent_hash"),
            *(controller.get("tried_hashes") or []),
        ) if value
    }
    if not hashes:
        return {"checked": 0, "all_absent": True}
    client = connected_client()
    present = []
    for value in hashes:
        try:
            torrent_info(client, value)
            present.append(value)
        except QbtError as exc:
            if "not present" not in str(exc):
                raise
    if present:
        raise ValueError("remove the exact Movies Nerd qBittorrent job before final cleanup")
    return {"checked": len(hashes), "all_absent": True}


def clean_completed_job(
    destination: Path, staged: list[Path], job_path: Path,
) -> dict:
    library, stage, state = library_for(destination)
    clean_appledouble_tree(destination)
    final = verify_final_destination(destination)
    checked_job, job = load_job(job_path)
    if job.get("state") != "imported":
        raise ValueError("job must be imported before final cleanup")
    if state not in checked_job.parents:
        raise ValueError("job manifest does not belong to the destination library")
    targets = checked_targets(staged, stage)
    qbt_absent = verify_recorded_torrents_absent(job)

    job_id = str(job["job_id"])
    state_targets = [
        checked_job,
        state / "trash" / job_id,
        state / "locks" / f"{job_id}.lock",
        state / "torrents" / job_id,
        checked_job.with_name("._" + checked_job.name),
    ]
    for target in state_targets:
        checked_state_target(target, state)

    for target in sorted(targets, key=lambda path: len(path.parts), reverse=True):
        remove_path(target)
    clean_appledouble_tree(stage)
    clean_appledouble_tree(state)
    for target in state_targets:
        if target.exists():
            remove_path(target)
    prune_empty(stage)
    prune_staging_root(stage)
    clean_appledouble_tree(state)
    prune_empty(state)

    leftovers = [str(path) for path in targets + state_targets if path.exists() or path.is_symlink()]
    if leftovers:
        raise ValueError("completed job cleanup left job-specific artifacts")
    return {
        "clean": True,
        "job_id": job_id,
        "destination": str(destination.resolve(strict=True)),
        "final_destination": final,
        "removed_staged_targets": len(targets),
        "qbit_absent": qbt_absent,
        "persistent_state": [str(state / "cache" / "provider-health.json")]
        if (state / "cache" / "provider-health.json").is_file() else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--staged", type=Path, action="append", default=[])
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        library, stage, state = library_for(args.destination)
        final = verify_final_destination(args.destination.resolve(strict=True))
        targets = checked_targets(args.staged, stage)
        checked_job, job = load_job(args.job)
        output = {
            "mode": "commit" if args.commit else "dry-run",
            "job_id": job["job_id"],
            "destination": str(args.destination.resolve(strict=True)),
            "final_destination": final,
            "staged_targets": [str(path) for path in targets],
            "state_root": str(state),
        }
        if args.commit:
            output.update(clean_completed_job(
                args.destination.resolve(strict=True), args.staged, checked_job,
            ))
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (ManifestError, OSError, QbtError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
