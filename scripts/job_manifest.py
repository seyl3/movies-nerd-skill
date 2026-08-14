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
import threading
import uuid

from _common import clean_appledouble_tree, remove_appledouble_sibling, state_roots
from qbittorrent_api import QbtError, magnet_hash, safe_magnet
from torrent_metadata import TorrentMetadataError, checked_torrent_url

MAX_MANIFEST_BYTES = 256 * 1024
MAX_DEPTH = 8
MAX_ITEMS = 1_000
SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|cookie|authorization|credential)", re.I,
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UPDATE_LOCKS: dict[str, threading.RLock] = {}
_UPDATE_LOCKS_GUARD = threading.Lock()


def job_update_lock(path: str | Path) -> threading.RLock:
    """Return the in-process lock protecting one manifest's read-modify-write cycle."""
    key = str(Path(path).expanduser().resolve(strict=False))
    with _UPDATE_LOCKS_GUARD:
        return _UPDATE_LOCKS.setdefault(key, threading.RLock())
STATES = {"planned", "confirmed", "downloading", "stalled", "downloaded", "finalizing", "verified", "imported", "failed"}
STEP_STATES = {"pending", "running", "complete", "skipped", "failed"}
IMMUTABLE_KEYS = {"version", "job_id", "created_at", "kind", "identity"}
DEFAULT_STEPS = {
    "search": "pending",
    "confirmation": "pending",
    "metadata_gate": "pending",
    "transfer": "pending",
    "content_gate": "pending",
    "media_probe": "pending",
    "enrichment": "pending",
    "subtitles": "pending",
    "library_import": "pending",
    "cleanup": "pending",
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
    "enrichment-started": {
        "state": "downloading",
        "steps": {"enrichment": "running"},
    },
    "downloaded": {
        "state": "downloaded",
        "steps": {"transfer": "complete"},
    },
    "finalizing": {
        "state": "finalizing",
        "steps": {"content_gate": "running"},
    },
    "verified": {
        "state": "verified",
        "steps": {"content_gate": "complete", "media_probe": "complete"},
    },
    "imported": {
        "state": "imported",
        "steps": {
            "enrichment": "complete", "subtitles": "complete",
            "library_import": "complete", "cleanup": "running",
        },
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
    "enrichment-started": {"downloading"},
    "downloaded": {"downloading"},
    "finalizing": {"downloaded"},
    "verified": {"downloaded", "finalizing"},
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
    source_text = str(path)
    if source_text == "-":
        raw = sys.stdin.buffer.read(max_bytes + 1)
    elif source_text.lstrip().startswith("{"):
        raw = source_text.encode("utf-8")
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
    movie_state, series_state = state_roots(environ)
    state = movie_state if kind == "movie" else series_state
    if state.exists() and state.is_symlink():
        raise ManifestError("Movies Nerd state root must not be a symlink")
    if create:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state, 0o700)
        remove_appledouble_sibling(state)
    root = state / "jobs"
    if root.exists() and root.is_symlink():
        raise ManifestError("jobs directory must not be a symlink")
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        remove_appledouble_sibling(root)
        clean_appledouble_tree(state)
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
        remove_appledouble_sibling(path)
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
        remove_appledouble_sibling(path)
    finally:
        temp.unlink(missing_ok=True)
        remove_appledouble_sibling(temp)


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
        "version": 2,
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
        "controller": {
            "phase": "planned",
            "attempt": 0,
            "active_hash": None,
            "standby_hash": None,
            "tried_hashes": [],
        },
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
    job_id = value.get("job_id")
    kind = value.get("kind")
    if (
        value.get("version") != 2
        or not isinstance(job_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", job_id)
        or kind not in {"movie", "series"}
        or checked.name != f"{job_id}.json"
    ):
        raise ManifestError("unsupported or invalid job manifest")
    expected = jobs_root(kind, environ, create=False)
    if checked.parent != expected.resolve(strict=True):
        raise ManifestError("job manifest is stored under the wrong media type")
    return checked, value


def update_job(
    path: str | Path, patch: dict, environ: dict[str, str] | None = None,
) -> dict:
    with job_update_lock(path):
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
    with job_update_lock(path):
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


def remove_failed_job(
    path: str | Path, environ: dict[str, str] | None = None,
) -> dict:
    """Remove exact terminal failed-job state after payload cleanup."""
    checked, current = load_job(path, environ)
    if current.get("state") != "failed":
        raise ManifestError("only a failed job can be removed")
    state = checked.parent.parent
    if state.name != ".movies-nerd" or checked.parent.name != "jobs":
        raise ManifestError("failed job is outside the exact Movies Nerd state layout")
    trash = state / "trash" / current["job_id"]
    if trash.exists():
        raise ManifestError("failed job trash must be cleared before its manifest")
    checked.unlink()
    sidecar = checked.with_name("._" + checked.name)
    if sidecar.is_file() and not sidecar.is_symlink():
        sidecar.unlink()
    try:
        checked.parent.rmdir()
    except OSError:
        pass
    else:
        jobs_sidecar = checked.parent.with_name("._" + checked.parent.name)
        if jobs_sidecar.is_file() and not jobs_sidecar.is_symlink():
            jobs_sidecar.unlink(missing_ok=True)
    return {"removed": True, "job": current["job_id"], "state_clean": not checked.exists()}


def checked_release(value: object) -> dict:
    if not isinstance(value, dict):
        raise ManifestError("search selection is missing a release")
    fields = {
        key: deepcopy(value.get(key))
        for key in (
            "title", "source", "provider", "size_bytes", "size", "resolution",
            "seeders", "leechers", "score", "warnings", "magnet", "info_hash",
            "torrent_url", "direct_metadata", "reported_peer_health",
            "provider_reliability_bonus", "canonical_title", "language",
        )
    }
    if not str(fields["title"] or "").strip() or not str(fields["source"] or "").strip():
        raise ManifestError("search selection has an invalid title or source")
    try:
        size = int(fields["size_bytes"])
        seeders = int(fields["seeders"])
    except (TypeError, ValueError) as exc:
        raise ManifestError("search selection has invalid size or peer data") from exc
    if size <= 0 or size > 100 * 1024 ** 3 or seeders < 0:
        raise ManifestError("search selection is outside safety bounds")
    fields["size_bytes"] = size
    fields["seeders"] = seeders
    try:
        info_hash = magnet_hash(str(fields["magnet"] or ""))
    except (QbtError, ValueError) as exc:
        raise ManifestError("search selection has an invalid magnet") from exc
    fields["info_hash"] = info_hash
    fields["magnet"] = safe_magnet(info_hash, str(fields["title"])[:300])
    if fields.get("torrent_url"):
        try:
            fields["torrent_url"] = checked_torrent_url(str(fields["torrent_url"]))
        except TorrentMetadataError as exc:
            raise ManifestError("search selection has an invalid direct torrent URL") from exc
    else:
        fields["torrent_url"] = None
    validate_tree(fields)
    return fields


def record_search(
    job: str | Path, search_result: str | Path,
    environ: dict[str, str] | None = None,
) -> dict:
    result = read_json(search_result, 5 * 1024 * 1024)
    return record_search_value(job, result, environ)


def record_search_value(
    job: str | Path, result: dict,
    environ: dict[str, str] | None = None,
) -> dict:
    checked, current = load_job(job, environ)
    validate_tree(result)
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
    request_imdb = str(request.get("imdb_id") or "").lower()
    if request_imdb:
        if not re.fullmatch(r"tt[0-9]{5,10}", request_imdb):
            raise ManifestError("search result has an invalid IMDb ID")
        current_ids = dict((identity.get("ids") or {}))
        existing = str(current_ids.get("imdb") or current_ids.get("imdb_id") or "").lower()
        if existing and existing != request_imdb:
            raise ManifestError("search result IMDb ID does not match the job")
        if not existing:
            if current.get("state") != "planned":
                raise ManifestError("authoritative IDs must be bound before confirmation")
            current_ids["imdb"] = request_imdb
            bound = deepcopy(current)
            bound["identity"]["ids"] = current_ids
            bound["updated_at"] = now_utc()
            atomic_write(checked, bound)
            current = bound
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
    if not isinstance(raw_pool, list) or not 1 <= len(raw_pool) <= 6:
        raise ManifestError("search selection must contain one to six race candidates")
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
        "confirmation_envelope": selection.get("confirmation_envelope") or {},
        "steps": {"search": "complete"},
        "cache": {
            "search_summary": {
                "query": result.get("query"),
                "elapsed_ms": result.get("elapsed_ms"),
                "usable_results": result.get("usable_results"),
            },
        },
    }
    return update_job(checked, patch, environ)


def redacted(value: dict) -> dict:
    def visit(item: object) -> object:
        if isinstance(item, dict):
            output = {}
            for key, child in item.items():
                if key.casefold() == "magnet" and child:
                    output["magnet"] = "<stored safely; audit with info_hash>"
                    output["magnet_stored"] = True
                    if not item.get("info_hash"):
                        try:
                            output["info_hash"] = magnet_hash(str(child))
                        except QbtError:
                            output["info_hash"] = "<invalid>"
                    continue
                output[key] = visit(child)
            return output
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
    create.add_argument("--imdb-id", help="authoritative IMDb ID, for example tt1234567")
    create.add_argument("--tmdb-id", help="authoritative numeric TMDB ID")
    create.add_argument("--runtime-min", type=float, help="authoritative runtime in minutes")
    create.add_argument(
        "--input",
        help="optional JSON file, inline JSON object, or - for stdin",
    )
    update = subparsers.add_parser("update")
    update.add_argument("job")
    update.add_argument(
        "--input", required=True,
        help="JSON file, inline JSON merge object, or - for stdin",
    )
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
    remove_failed = subparsers.add_parser("remove-failed")
    remove_failed.add_argument("job")
    remove_failed.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "create":
            initial = read_json(args.input) if args.input else {}
            ids = dict(((initial.get("identity") or {}).get("ids") or {}))
            if args.imdb_id:
                if not re.fullmatch(r"tt[0-9]{5,10}", args.imdb_id):
                    raise ManifestError("IMDb ID must look like tt1234567")
                ids["imdb"] = args.imdb_id
            if args.tmdb_id:
                if not re.fullmatch(r"[1-9][0-9]{0,11}", args.tmdb_id):
                    raise ManifestError("TMDB ID must be numeric")
                ids["tmdb"] = args.tmdb_id
            if ids:
                initial.setdefault("identity", {})["ids"] = ids
            if args.runtime_min is not None:
                if not 1 <= args.runtime_min <= 1440:
                    raise ManifestError("runtime must be between 1 and 1440 minutes")
                initial.setdefault("cache", {})["runtime_minutes"] = args.runtime_min
            path = create_job(
                args.kind, args.title, args.year,
                initial or None,
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
        elif args.command == "remove-failed":
            if not args.commit:
                raise ManifestError("refusing to remove failed job without --commit")
            print(json.dumps(remove_failed_job(args.job), ensure_ascii=False, indent=2))
        else:
            _, value = load_job(args.job)
            print(json.dumps(value if args.raw else redacted(value), ensure_ascii=False, indent=2))
        return 0
    except (ManifestError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
