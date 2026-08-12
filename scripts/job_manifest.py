#!/usr/bin/env python3
"""Create and update resumable Movies Nerd job state inside staging."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from urllib.parse import quote

from _common import staging_roots
from qbittorrent_api import QbtError, magnet_hash

MAX_MANIFEST_BYTES = 256 * 1024
MAX_DEPTH = 8
MAX_ITEMS = 1_000
SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|cookie|authorization|credential)", re.I,
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
STATES = {"planned", "confirmed", "downloading", "stalled", "downloaded", "verified", "imported", "failed"}
STEP_STATES = {"pending", "running", "complete", "skipped", "failed"}
IMMUTABLE_KEYS = {"version", "job_id", "created_at", "kind", "identity"}
DEFAULT_STEPS = {
    "search": "pending",
    "confirmation": "pending",
    "metadata_gate": "pending",
    "transfer": "pending",
    "content_gate": "pending",
    "media_probe": "pending",
    "subtitles": "pending",
    "library_import": "pending",
}
EVENT_PATCHES = {
    "confirmed": {
        "state": "confirmed",
        "steps": {"search": "complete", "confirmation": "complete"},
    },
    "metadata-started": {
        "state": "confirmed",
        "steps": {"search": "complete", "confirmation": "complete", "metadata_gate": "running"},
    },
    "metadata-failed": {
        "state": "failed",
        "steps": {
            "search": "complete", "confirmation": "complete",
            "metadata_gate": "failed", "transfer": "skipped",
        },
    },
    "replacement-started": {
        "state": "confirmed",
        "artifacts": {"failure_reason": None},
        "steps": {
            "confirmation": "complete", "metadata_gate": "running",
            "transfer": "pending",
        },
    },
    "stalled": {
        "state": "stalled",
        "steps": {"transfer": "failed"},
    },
    "downloading": {
        "state": "downloading",
        "artifacts": {"failure_reason": None},
        "steps": {
            "search": "complete", "confirmation": "complete",
            "metadata_gate": "complete", "transfer": "running",
        },
    },
    "downloaded": {
        "state": "downloaded",
        "steps": {"transfer": "complete"},
    },
    "verified": {
        "state": "verified",
        "steps": {"content_gate": "complete", "media_probe": "complete"},
    },
    "imported": {
        "state": "imported",
        "steps": {"subtitles": "complete", "library_import": "complete"},
    },
    "failed": {"state": "failed"},
}
EVENT_FROM_STATES = {
    "confirmed": {"planned", "confirmed"},
    "metadata-started": {"confirmed"},
    "metadata-failed": {"confirmed"},
    "replacement-started": {"downloading", "stalled"},
    "stalled": {"downloading"},
    "downloading": {"confirmed"},
    "downloaded": {"downloading"},
    "verified": {"downloaded"},
    "imported": {"verified"},
    "failed": STATES - {"imported"},
}


class ManifestError(ValueError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_tree(value: object, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ManifestError("manifest nesting exceeds safety limit")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 16 * 1024 or CONTROL_RE.search(value):
            raise ManifestError("manifest contains an oversized or unsafe string")
        return
    if isinstance(value, list):
        if len(value) > MAX_ITEMS:
            raise ManifestError("manifest list exceeds safety limit")
        for item in value:
            validate_tree(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_ITEMS:
            raise ManifestError("manifest object exceeds safety limit")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 100:
                raise ManifestError("manifest contains an invalid key")
            if SENSITIVE_KEY_RE.search(key):
                raise ManifestError(f"credential-like field is forbidden: {key}")
            validate_tree(item, depth + 1)
        return
    raise ManifestError(f"unsupported manifest value: {type(value).__name__}")


def read_json(path: str | Path, max_bytes: int = MAX_MANIFEST_BYTES) -> dict:
    if str(path) == "-":
        raw = sys.stdin.buffer.read(max_bytes + 1)
    else:
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise ManifestError("JSON input must be a regular non-symlink file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ManifestError("JSON input must be a regular file")
            raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ManifestError(f"JSON input exceeds {max_bytes // 1024} KiB")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError("JSON input is invalid") from exc
    if not isinstance(value, dict):
        raise ManifestError("JSON input must be an object")
    validate_tree(value)
    return value


def jobs_root(
    kind: str, environ: dict[str, str] | None = None, *, create: bool = True,
) -> Path:
    movie_stage, series_stage = staging_roots(environ)
    stage = movie_stage if kind == "movie" else series_stage
    if stage.exists() and stage.is_symlink():
        raise ManifestError("staging root must not be a symlink")
    if create:
        stage.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = stage / "jobs"
    if root.exists() and root.is_symlink():
        raise ManifestError("jobs directory must not be a symlink")
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
    return root.resolve(strict=create)


def checked_job_path(path: str | Path, environ: dict[str, str] | None = None) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ManifestError("job manifest must be a regular non-symlink file")
    resolved = candidate.resolve(strict=True)
    allowed = []
    for kind in ("movie", "series"):
        root = jobs_root(kind, environ, create=False)
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            resolved.relative_to(root)
            allowed.append(root)
        except ValueError:
            pass
    if not allowed or resolved.suffix != ".json":
        raise ManifestError("job manifest is outside the selected staging roots")
    return resolved


def serialized(value: dict) -> bytes:
    validate_tree(value)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("job manifest exceeds 256 KiB")
    return raw


def atomic_write(path: Path, value: dict, *, exclusive: bool = False) -> None:
    raw = serialized(value)
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return
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


def create_job(
    kind: str, title: str, year: int, extra: dict | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    if kind not in {"movie", "series"}:
        raise ManifestError("kind must be movie or series")
    if not title.strip() or CONTROL_RE.search(title):
        raise ManifestError("title is empty or unsafe")
    if not 1870 <= year <= 2100:
        raise ManifestError("year must be between 1870 and 2100")
    timestamp = now_utc()
    job_id = uuid.uuid4().hex
    value = {
        "version": 1,
        "job_id": job_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "kind": kind,
        "identity": {"title": title.strip(), "year": year, "ids": {}},
        "state": "planned",
        "steps": deepcopy(DEFAULT_STEPS),
        "release": {},
        "destination": None,
        "artifacts": {},
        "cache": {},
    }
    if extra:
        forbidden = (IMMUTABLE_KEYS - {"identity"}).intersection(extra)
        identity_patch = extra.get("identity")
        if forbidden or (
            identity_patch is not None
            and (not isinstance(identity_patch, dict) or set(identity_patch) - {"ids"})
        ):
            raise ManifestError("initial data cannot replace immutable job identity fields")
        value = merge_patch(value, extra, creating=True)
    path = jobs_root(kind, environ) / f"{job_id}.json"
    atomic_write(path, value, exclusive=True)
    return path


def deep_merge(current: dict, patch: dict) -> dict:
    result = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def merge_patch(current: dict, patch: dict, *, creating: bool = False) -> dict:
    validate_tree(patch)
    if not creating and IMMUTABLE_KEYS.intersection(patch):
        raise ManifestError("immutable job fields cannot be updated")
    result = deep_merge(current, patch)
    state = result.get("state")
    if state not in STATES:
        raise ManifestError(f"invalid job state: {state}")
    steps = result.get("steps")
    if not isinstance(steps, dict) or any(status not in STEP_STATES for status in steps.values()):
        raise ManifestError("job steps contain an invalid status")
    result["updated_at"] = now_utc()
    validate_tree(result)
    return result


def load_job(path: str | Path, environ: dict[str, str] | None = None) -> tuple[Path, dict]:
    checked = checked_job_path(path, environ)
    value = read_json(checked)
    if value.get("version") != 1 or not isinstance(value.get("job_id"), str):
        raise ManifestError("unsupported or invalid job manifest")
    return checked, value


def update_job(
    path: str | Path, patch: dict, environ: dict[str, str] | None = None,
) -> dict:
    checked, current = load_job(path, environ)
    updated = merge_patch(current, patch)
    atomic_write(checked, updated)
    return updated


def transition_job(
    path: str | Path, event: str, *, reason: str | None = None,
    torrent_hash: str | None = None, release: dict | None = None,
    environ: dict[str, str] | None = None,
) -> dict:
    """Apply one consistent workflow transition instead of ad-hoc state patches."""
    _, current = load_job(path, environ)
    if event not in EVENT_PATCHES:
        raise ManifestError(f"unknown job event: {event}")
    if current.get("state") not in EVENT_FROM_STATES[event]:
        raise ManifestError(
            f"cannot apply {event} while job state is {current.get('state')}"
        )
    patch = deepcopy(EVENT_PATCHES[event])
    artifacts = {}
    if reason:
        artifacts["failure_reason"] = reason.strip()[:1000]
    if torrent_hash:
        try:
            artifacts["torrent_hash"] = magnet_hash(
                f"magnet:?xt=urn:btih:{torrent_hash}"
            )
        except QbtError as exc:
            raise ManifestError("transition has an invalid torrent hash") from exc
    if artifacts:
        patch["artifacts"] = artifacts
    if release is not None:
        patch["release"] = checked_release(release)
    if event == "failed":
        steps = deepcopy(current.get("steps") or {})
        for name, status in steps.items():
            if status == "running":
                steps[name] = "failed"
        patch["steps"] = steps
    return update_job(path, patch, environ)


def archive_failed_job(
    path: str | Path, environ: dict[str, str] | None = None,
) -> dict:
    """Move exact failed job state out of .incoming while keeping it recoverable."""
    checked, current = load_job(path, environ)
    if current.get("state") != "failed":
        raise ManifestError("only a failed job can be archived")
    stage = checked.parent.parent
    library = stage.parent.parent
    if stage.name != "Movies Nerd" or stage.parent.name != ".incoming":
        raise ManifestError("failed job is outside the exact Movies Nerd staging layout")
    archive = (
        library / ".movies-nerd-trash" / "failed-jobs"
        / f"{now_utc().replace(':', '-')}-{current['job_id'][:8]}"
    )
    archive.mkdir(mode=0o700, parents=True, exist_ok=False)
    destination = archive / checked.name
    os.replace(checked, destination)
    sidecar = checked.with_name("._" + checked.name)
    if sidecar.is_file() and not sidecar.is_symlink():
        os.replace(sidecar, archive / sidecar.name)
    try:
        checked.parent.rmdir()
    except OSError:
        pass
    else:
        jobs_sidecar = checked.parent.with_name("._" + checked.parent.name)
        if jobs_sidecar.is_file() and not jobs_sidecar.is_symlink():
            jobs_sidecar.unlink(missing_ok=True)
    transfers = stage / "transfers"
    try:
        transfers.rmdir()
    except OSError:
        pass
    else:
        transfer_sidecar = transfers.with_name("._" + transfers.name)
        if transfer_sidecar.is_file() and not transfer_sidecar.is_symlink():
            transfer_sidecar.unlink(missing_ok=True)
    return {"archived": True, "from": str(checked), "to": str(destination)}


def checked_release(value: object) -> dict:
    if not isinstance(value, dict):
        raise ManifestError("search selection is missing a release")
    fields = {
        key: deepcopy(value.get(key))
        for key in (
            "title", "source", "provider", "size_bytes", "size", "resolution",
            "seeders", "leechers", "score", "warnings", "magnet",
        )
    }
    if not str(fields["title"] or "").strip() or not str(fields["source"] or "").strip():
        raise ManifestError("search selection has an invalid title or source")
    try:
        size = int(fields["size_bytes"])
        seeders = int(fields["seeders"])
    except (TypeError, ValueError) as exc:
        raise ManifestError("search selection has invalid size or peer data") from exc
    if size <= 0 or size > 100 * 1024 ** 3 or seeders <= 0:
        raise ManifestError("search selection is outside safety bounds")
    fields["size_bytes"] = size
    fields["seeders"] = seeders
    try:
        info_hash = magnet_hash(str(fields["magnet"] or ""))
    except (QbtError, ValueError) as exc:
        raise ManifestError("search selection has an invalid magnet") from exc
    fields["magnet"] = (
        f"magnet:?xt=urn:btih:{info_hash}&dn="
        f"{quote(str(fields['title'])[:300], safe='')}"
    )
    validate_tree(fields)
    return fields


def record_search(
    job: str | Path, search_result: str | Path,
    environ: dict[str, str] | None = None,
) -> dict:
    _, current = load_job(job, environ)
    result = read_json(search_result, 5 * 1024 * 1024)
    request = result.get("request")
    selection = result.get("selection")
    if not isinstance(request, dict) or not isinstance(selection, dict):
        raise ManifestError("search result lacks request or selection data")
    identity = current["identity"]
    if (
        str(request.get("title") or "").strip().casefold() != str(identity["title"]).casefold()
        or request.get("year") != identity["year"]
        or request.get("kind") != current["kind"]
    ):
        raise ManifestError("search result identity does not match the job")
    primary = checked_release(selection.get("primary"))
    raw_backup = selection.get("backup")
    backup = checked_release(raw_backup) if raw_backup is not None else None
    if backup:
        if magnet_hash(primary["magnet"]) == magnet_hash(backup["magnet"]):
            raise ManifestError("backup release duplicates the primary release")
        if str(primary["source"]).strip().casefold() == str(backup["source"]).strip().casefold():
            raise ManifestError("backup release must use a different source")
    raw_pool = selection.get("candidates")
    if raw_pool is None:
        raw_pool = [value for value in (selection.get("primary"), raw_backup) if value]
    if not isinstance(raw_pool, list) or not 1 <= len(raw_pool) <= 3:
        raise ManifestError("search selection must contain one to three race candidates")
    candidate_pool = []
    hashes = set()
    for value in raw_pool:
        candidate = checked_release(value)
        info_hash = magnet_hash(candidate["magnet"])
        if info_hash in hashes:
            continue
        hashes.add(info_hash)
        candidate_pool.append(candidate)
    if not candidate_pool or magnet_hash(primary["magnet"]) != magnet_hash(candidate_pool[0]["magnet"]):
        raise ManifestError("race candidates must begin with the selected primary release")
    patch = {
        "release": primary,
        "backup_release": backup,
        "candidate_pool": candidate_pool,
        "steps": {"search": "complete"},
        "cache": {
            "search_summary": {
                "query": result.get("query"),
                "elapsed_ms": result.get("elapsed_ms"),
                "usable_results": result.get("usable_results"),
            },
        },
    }
    return update_job(job, patch, environ)


def redacted(value: dict) -> dict:
    def visit(item: object) -> object:
        if isinstance(item, dict):
            return {
                key: "<stored>" if key.casefold() in {"magnet", "torrent_hash"} and child else visit(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child) for child in item]
        return deepcopy(item)

    return visit(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--kind", choices=("movie", "series"), required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--year", type=int, required=True)
    create.add_argument("--input", help="optional JSON object to merge; use - for stdin")
    update = subparsers.add_parser("update")
    update.add_argument("job")
    update.add_argument("--input", required=True, help="JSON merge object; use - for stdin")
    record = subparsers.add_parser("record-search")
    record.add_argument("job")
    record.add_argument("search_result")
    show = subparsers.add_parser("show")
    show.add_argument("job")
    show.add_argument("--raw", action="store_true")
    transition = subparsers.add_parser("transition")
    transition.add_argument("job")
    transition.add_argument("--event", choices=tuple(EVENT_PATCHES), required=True)
    transition.add_argument("--reason")
    transition.add_argument("--torrent-hash")
    archive = subparsers.add_parser("archive-failed")
    archive.add_argument("job")
    archive.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "create":
            path = create_job(
                args.kind, args.title, args.year,
                read_json(args.input) if args.input else None,
            )
            _, value = load_job(path)
            print(json.dumps({"job": str(path), "manifest": redacted(value)}, ensure_ascii=False, indent=2))
        elif args.command == "update":
            value = update_job(args.job, read_json(args.input))
            print(json.dumps({"job": str(Path(args.job)), "manifest": redacted(value)}, ensure_ascii=False, indent=2))
        elif args.command == "record-search":
            value = record_search(args.job, args.search_result)
            print(json.dumps({"job": str(Path(args.job)), "manifest": redacted(value)}, ensure_ascii=False, indent=2))
        elif args.command == "transition":
            value = transition_job(
                args.job, args.event, reason=args.reason, torrent_hash=args.torrent_hash,
            )
            print(json.dumps({"job": str(Path(args.job)), "manifest": redacted(value)}, ensure_ascii=False, indent=2))
        elif args.command == "archive-failed":
            if not args.commit:
                raise ManifestError("refusing to archive failed job without --commit")
            print(json.dumps(archive_failed_job(args.job), ensure_ascii=False, indent=2))
        else:
            _, value = load_job(args.job)
            print(json.dumps(value if args.raw else redacted(value), ensure_ascii=False, indent=2))
        return 0
    except (ManifestError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
