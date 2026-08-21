#!/usr/bin/env python3
"""Minimal, loopback-only qBittorrent Web API client for Movies Nerd."""

from __future__ import annotations

import argparse
import base64
import binascii
import errno
import http.cookiejar
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener
import uuid

from _common import GIB, format_gib, remove_appledouble_sibling, staging_roots
from payload_safety import BIDI_RE, MAX_PAYLOAD_FILES, VIDEO_EXTENSIONS, filename_reasons

MAX_BYTES = 15 * GIB
LOOPBACKS = {"127.0.0.1", "::1", "localhost"}
EXTRA_RE = re.compile(r"(?:^|[\\/._ -])(sample|trailer|featurette|interview|deleted[ ._-]?scene|behind[ ._-]?the[ ._-]?scenes|bonus)(?:$|[\\/._ -])", re.I)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SKIP_ONLY_REASONS = {
    "hidden payload path",
    "dangerous or archive extension, including inner extension",
    "unexpected extension",
}
SAFE_PUBLIC_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://open.stealth.si:80/announce",
)


class QbtError(RuntimeError):
    pass


class QbtUnavailable(QbtError):
    """The local qBittorrent application cannot currently be reached."""


class QbtAccessDenied(QbtError):
    """The host must grant this process local-app access before retrying."""


def access_denied(exc: BaseException) -> bool:
    """Recognize host permission denials without misreporting qBittorrent as closed."""
    current: object = exc
    for _ in range(5):
        if isinstance(current, PermissionError) or getattr(current, "errno", None) in {
            errno.EACCES,
            errno.EPERM,
        }:
            return True
        next_reason = getattr(current, "reason", None)
        if next_reason is None or next_reason is current:
            break
        current = next_reason
    return False


def checked_base_url(raw: str) -> str:
    value = raw.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACKS or parsed.username or parsed.password:
        raise QbtError("qBittorrent needs its one-time Movies Nerd setup")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise QbtError("qBittorrent needs its one-time Movies Nerd setup")
    return value


def normalize_hash(value: str) -> str:
    candidate = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", candidate) or re.fullmatch(r"[0-9a-f]{64}", candidate):
        return candidate
    raise QbtError("torrent hash must be 40 or 64 hexadecimal characters")


def magnet_hash(magnet: str) -> str:
    if len(magnet.encode("utf-8")) > 64 * 1024 or CONTROL_RE.search(magnet):
        raise QbtError("magnet URL is too large or contains control characters")
    parsed = urlparse(magnet)
    if parsed.scheme.lower() != "magnet":
        raise QbtError("only magnet: URLs are accepted")
    for xt in parse_qs(parsed.query).get("xt", []):
        prefix = "urn:btih:"
        if not xt.lower().startswith(prefix):
            continue
        value = xt[len(prefix):]
        if re.fullmatch(r"[0-9A-Fa-f]{40}", value):
            return value.lower()
        if re.fullmatch(r"[A-Z2-7a-z2-7]{32}", value):
            try:
                return base64.b32decode(value.upper()).hex()
            except binascii.Error as exc:
                raise QbtError("invalid base32 magnet info hash") from exc
    raise QbtError("magnet lacks a supported v1 btih info hash")


def safe_magnet(info_hash: str, title: str, *, trackers: bool = True) -> str:
    normalized = normalize_hash(info_hash)
    clean_title = " ".join(str(title or "torrent").strip().split())[:300] or "torrent"
    if CONTROL_RE.search(clean_title):
        raise QbtError("magnet title contains control characters")
    result = f"magnet:?xt=urn:btih:{normalized}&dn={quote(clean_title, safe='')}"
    if trackers:
        result += "".join(f"&tr={quote(tracker, safe='')}" for tracker in SAFE_PUBLIC_TRACKERS)
    return result


def multipart(fields: dict[str, str | bytes]) -> tuple[bytes, str]:
    boundary = "----MoviesNerd" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise QbtError("invalid multipart field name")
        chunks.append(f"--{boundary}\r\n".encode())
        if isinstance(value, bytes):
            chunks.extend([
                f'Content-Disposition: form-data; name="{name}"; filename="candidate.torrent"\r\n'.encode(),
                b"Content-Type: application/x-bittorrent\r\n\r\n",
                value,
                b"\r\n",
            ])
        else:
            chunks.extend([
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class QbtClient:
    def __init__(self, base_url: str, username: str | None, password: str | None):
        self.base_url = checked_base_url(base_url)
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        if bool(username) != bool(password):
            raise QbtError("qBittorrent needs its one-time Movies Nerd setup")
        if username and password:
            response = self.request("auth/login", {"username": username, "password": password})
            if response.strip() != b"Ok.":
                raise QbtError("qBittorrent needs its one-time Movies Nerd setup")

    def request(self, endpoint: str, fields: dict[str, str | bytes] | None = None, multipart_body: bool = False) -> bytes:
        url = f"{self.base_url}/api/v2/{endpoint}"
        headers = {"Referer": self.base_url, "Origin": self.base_url, "User-Agent": "Movies-Nerd/2"}
        data = None
        if fields is not None:
            if multipart_body:
                data, content_type = multipart(fields)
                headers["Content-Type"] = content_type
            else:
                if any(not isinstance(value, str) for value in fields.values()):
                    raise QbtError("binary qBittorrent fields require multipart encoding")
                data = urlencode(fields).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=headers, method="POST" if fields is not None else "GET")
        try:
            with self.opener.open(request, timeout=8) as response:
                return response.read(16 * 1024 * 1024)
        except HTTPError as exc:
            exc.read(1024)
            if exc.code == 403:
                raise QbtError("qBittorrent needs its one-time Movies Nerd setup") from exc
            raise QbtError("qBittorrent couldn't complete the request. Please try again.") from exc
        except (URLError, TimeoutError, PermissionError) as exc:
            if access_denied(exc):
                raise QbtAccessDenied(
                    "local qBittorrent access needs host approval; retry this command with local-app permission"
                ) from exc
            raise QbtUnavailable("qBittorrent app isn't ready") from exc

    def json(self, endpoint: str) -> object:
        try:
            return json.loads(self.request(endpoint))
        except json.JSONDecodeError as exc:
            raise QbtError(f"qBittorrent returned invalid JSON for {endpoint}") from exc

    def json_post(self, endpoint: str, fields: dict[str, str]) -> object:
        try:
            return json.loads(self.request(endpoint, fields))
        except json.JSONDecodeError as exc:
            raise QbtError(f"qBittorrent returned invalid JSON for {endpoint}") from exc


def client_from_env() -> QbtClient:
    return QbtClient(
        os.environ.get("QBITTORRENT_URL", "http://127.0.0.1:8080"),
        os.environ.get("QBITTORRENT_USERNAME"),
        os.environ.get("QBITTORRENT_PASSWORD"),
    )


def launch_qbittorrent() -> bool:
    """Open the installed qBittorrent app without a shell or extra dependency."""
    system = platform.system()
    try:
        if system == "Darwin":
            completed = subprocess.run(
                ["/usr/bin/open", "-g", "-a", "qBittorrent"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return completed.returncode == 0

        names = ("qbittorrent.exe", "qbittorrent") if system == "Windows" else ("qbittorrent", "qbittorrent-nox")
        executable = next((path for name in names if (path := shutil.which(name))), None)
        if not executable:
            return False
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if system == "Windows":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([executable], **kwargs)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def connected_client(wait_seconds: float = 12.0, retry_interval: float = 0.5) -> QbtClient:
    """Return a ready client, opening qBittorrent once when it is closed."""
    try:
        client = client_from_env()
        client.request("app/version")
        return client
    except QbtUnavailable:
        pass

    if not launch_qbittorrent():
        raise QbtUnavailable("qBittorrent app isn't open. Please open it, then try again.")

    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        try:
            client = client_from_env()
            client.request("app/version")
            return client
        except QbtUnavailable:
            if time.monotonic() >= deadline:
                raise QbtUnavailable("qBittorrent app isn't open. Please open it, then try again.")
            time.sleep(max(0.05, retry_interval))


def safe_stage(kind: str) -> Path:
    movie_stage, series_stage = staging_roots()
    return movie_stage if kind == "movie" else series_stage


def preflight(client: QbtClient) -> dict:
    """Read the transfer state once so acquisition failures are not blamed on a torrent."""
    version = client.request("app/version").decode("utf-8", "replace").strip()
    transfer = client.json("transfer/info")
    sync = client.json("sync/maindata?rid=0")
    if not isinstance(transfer, dict) or not isinstance(sync, dict):
        raise QbtError("qBittorrent readiness data is unavailable")
    server = sync.get("server_state") or {}
    if not isinstance(server, dict):
        server = {}
    connection = str(transfer.get("connection_status") or server.get("connection_status") or "unknown")
    try:
        dht_nodes = max(0, int(server.get("dht_nodes", 0) or 0))
    except (TypeError, ValueError):
        dht_nodes = 0
    try:
        global_limit = max(0, int(server.get("dl_rate_limit", 0) or 0))
    except (TypeError, ValueError):
        global_limit = 0
    return {
        "ready": True,
        "version": version,
        "connection": connection,
        "dht_nodes": dht_nodes,
        "alternative_speed_limits": bool(server.get("use_alt_speed_limits")),
        "global_download_limit": global_limit,
    }


def _transfer_directory(kind: str, torrent_hash: str) -> Path:
    stage = safe_stage(kind)
    if stage.is_symlink():
        raise QbtError("staging root must not be a symlink")
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    remove_appledouble_sibling(stage)
    try:
        stage.chmod(0o700)
    except OSError as exc:
        raise QbtError(f"cannot restrict staging permissions: {exc}") from exc
    transfer = stage / "transfers" / torrent_hash
    if transfer.is_symlink():
        raise QbtError("transfer staging must not be a symlink")
    transfer.mkdir(mode=0o700, parents=True, exist_ok=True)
    transfer.chmod(0o700)
    remove_appledouble_sibling(transfer)
    remove_appledouble_sibling(transfer.parent)
    return transfer


def add_candidate(
    client: QbtClient, *, info_hash: str, kind: str, rename: str | None,
    magnet: str | None = None, torrent_data: bytes | None = None,
) -> dict:
    """Add one already-confirmed magnet or validated .torrent file stopped."""
    normalized = normalize_hash(info_hash)
    if bool(magnet) == bool(torrent_data):
        raise QbtError("candidate must provide exactly one torrent source")
    if magnet and magnet_hash(magnet) != normalized:
        raise QbtError("candidate magnet does not match its expected hash")
    if torrent_data is not None:
        from torrent_metadata import TorrentMetadataError, inspect_torrent
        try:
            inspect_torrent(torrent_data, normalized)
        except TorrentMetadataError as exc:
            raise QbtError(str(exc)) from exc
    transfer = _transfer_directory(kind, normalized)
    fields: dict[str, str | bytes] = {
        "savepath": str(transfer),
        "tags": f"movies-nerd,{kind}",
        "paused": "true",
        "root_folder": "true",
        "autoTMM": "false",
    }
    if magnet:
        fields["urls"] = magnet
    else:
        fields["torrents"] = torrent_data or b""
    if rename:
        if CONTROL_RE.search(rename) or BIDI_RE.search(rename) or "/" in rename or "\\" in rename or rename in {".", ".."}:
            raise QbtError("rename must be a single safe path component")
        fields["rename"] = rename
    response = client.request("torrents/add", fields, multipart_body=True).decode("utf-8", "replace").strip()
    if response not in ("", "Ok."):
        raise QbtError(f"qBittorrent rejected the torrent: {response}")
    return {
        "added_stopped": True,
        "hash": normalized,
        "staging": str(transfer),
        "source_type": "torrent" if torrent_data is not None else "magnet",
    }


def classify_files(
    files: list[dict], series: bool = False, preferred_file_index: int | None = None,
) -> dict:
    if len(files) > MAX_PAYLOAD_FILES:
        return {
            "files": [],
            "unsafe": [{
                "index": -1,
                "name": "<torrent payload>",
                "size": 0,
                "priority": 0,
                "extra": False,
                "unsafe_reasons": [f"payload contains more than {MAX_PAYLOAD_FILES} files"],
            }],
            "main_feature": None,
            "preferred_file_error": None,
            "episodes": [],
            "keep_indices": [],
            "skip_indices": [],
            "selected_size": 0,
        }
    normalized = []
    unsafe = []
    videos = []
    seen_indices: set[int] = set()
    seen_paths: set[str] = set()
    for index, item in enumerate(files):
        reasons = []
        if not isinstance(item, dict):
            item = {}
            reasons.append("invalid torrent metadata record")
        name = str(item.get("name", ""))
        posix = PurePosixPath(name.replace("\\", "/"))
        suffix = posix.suffix.lower()
        reasons.extend(filename_reasons(name))
        portable_path = unicodedata.normalize("NFC", str(posix)).casefold()
        if portable_path in seen_paths:
            reasons.append("duplicate or platform-colliding payload path")
        seen_paths.add(portable_path)
        try:
            file_index = int(item.get("index", index))
        except (TypeError, ValueError):
            file_index = index
            reasons.append("invalid payload file index")
        if file_index < 0 or file_index in seen_indices:
            reasons.append("negative or duplicate payload file index")
        seen_indices.add(file_index)
        try:
            size = int(item.get("size", 0))
        except (TypeError, ValueError):
            size = -1
        if size <= 0:
            reasons.append("invalid or empty payload file size")
        try:
            priority = int(item.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
            reasons.append("invalid payload priority")
        extra = bool(EXTRA_RE.search(name))
        record = {
            "index": file_index,
            "name": name,
            "size": size,
            "priority": priority,
            "extra": extra,
            "unsafe_reasons": reasons,
        }
        normalized.append(record)
        hard_reasons = [reason for reason in reasons if reason not in SKIP_ONLY_REASONS]
        record["hard_reasons"] = hard_reasons
        if hard_reasons:
            unsafe.append(record)
        if suffix in VIDEO_EXTENSIONS and not extra and not reasons:
            videos.append(record)
    main = max(videos, key=lambda item: item["size"], default=None)
    preferred_error = None
    if preferred_file_index is not None:
        if series:
            preferred_error = "a single preferred file cannot select a whole series payload"
        else:
            try:
                if isinstance(preferred_file_index, bool):
                    raise ValueError
                preferred = int(preferred_file_index)
                if not 0 <= preferred <= 100_000:
                    raise ValueError
            except (TypeError, ValueError):
                preferred_error = "preferred movie file index is invalid"
            else:
                main = next((item for item in videos if item["index"] == preferred), None)
                if main is None:
                    preferred_error = "preferred movie file is missing, unsafe, or an extra"
    selected_videos = videos if series else ([main] if main else [])
    keep = [item["index"] for item in selected_videos]
    skip = [item["index"] for item in normalized if item["index"] not in keep]
    discarded = [item for item in normalized if item["index"] in skip]
    all_safe_video_indices = [
        item["index"] for item in normalized
        if PurePosixPath(item["name"]).suffix.lower() in VIDEO_EXTENSIONS
        and not item["unsafe_reasons"]
    ]
    selected_size = sum(item["size"] for item in normalized if item["index"] in keep)
    return {
        "files": normalized,
        "unsafe": unsafe,
        "main_feature": main,
        "preferred_file_error": preferred_error,
        "episodes": videos if series else [],
        "keep_indices": keep,
        "skip_indices": skip,
        "all_safe_video_indices": all_safe_video_indices,
        "discarded_by_default": discarded,
        "selected_size": selected_size,
    }


def torrent_info(client: QbtClient, torrent_hash: str) -> dict:
    result = client.json(f"torrents/info?hashes={torrent_hash}")
    if not isinstance(result, list) or not result:
        raise QbtError("torrent is not present in qBittorrent")
    if not isinstance(result[0], dict):
        raise QbtError("qBittorrent returned invalid torrent metadata")
    return result[0]


def torrent_files(client: QbtClient, torrent_hash: str) -> list[dict]:
    result = client.json(f"torrents/files?hash={torrent_hash}")
    if not isinstance(result, list):
        raise QbtError("qBittorrent returned an invalid file list")
    return result


def validate_torrent(
    client: QbtClient, torrent_hash: str, allow_oversize: bool = False,
    series: bool = False, preferred_file_index: int | None = None,
) -> tuple[dict, dict]:
    info = torrent_info(client, torrent_hash)
    files = classify_files(
        torrent_files(client, torrent_hash), series=series,
        preferred_file_index=preferred_file_index,
    )
    stage_roots = tuple(root.resolve(strict=False) for root in staging_roots())
    raw_save = Path(str(info.get("save_path", ""))).resolve(strict=False)
    if not any(raw_save == root or root in raw_save.parents for root in stage_roots):
        raise QbtError(f"torrent save path is outside Movies Nerd staging: {raw_save}")
    if files["unsafe"]:
        names = ", ".join(item["name"] for item in files["unsafe"][:5])
        raise QbtError(f"torrent contains unsafe or unexpected payload files: {names}")
    if files.get("preferred_file_error"):
        raise QbtError(str(files["preferred_file_error"]))
    if not files["files"]:
        raise QbtError("torrent metadata is not available yet; leave it stopped briefly and inspect again")
    if not files["main_feature"]:
        raise QbtError("no main video feature or episode was identified")
    if files["selected_size"] > MAX_BYTES and not allow_oversize:
        raise QbtError(f"selected payload is {format_gib(files['selected_size'])}, above the 15 GiB limit")
    return info, files


def command_status(client: QbtClient, _args: argparse.Namespace) -> dict:
    return {"app": "qBittorrent", **preflight(client)}


def command_add(client: QbtClient, args: argparse.Namespace) -> dict:
    torrent_hash = magnet_hash(args.magnet)
    if not args.commit:
        raise QbtError("refusing to add torrent without --commit")
    result = add_candidate(
        client, info_hash=torrent_hash, kind=args.kind, rename=args.rename,
        magnet=args.magnet,
    )
    result["next"] = "inspect metadata before start"
    return result


def configure_selection(
    client: QbtClient, torrent_hash: str, *, include_extras: bool = False,
    allow_oversize: bool = False, series: bool = False,
    preferred_file_index: int | None = None,
) -> tuple[dict, dict, int]:
    info, files = validate_torrent(
        client, torrent_hash, allow_oversize, series=series,
        preferred_file_index=preferred_file_index,
    )
    if include_extras:
        keep_indices = files["all_safe_video_indices"]
        skip_indices = [item["index"] for item in files["files"] if item["index"] not in keep_indices]
    else:
        keep_indices = files["keep_indices"]
        skip_indices = files["skip_indices"]
    transfer_size = sum(item["size"] for item in files["files"] if item["index"] in keep_indices)
    if transfer_size > MAX_BYTES and not allow_oversize:
        raise QbtError(f"actual selected transfer is {format_gib(transfer_size)}, above the 15 GiB limit")
    if keep_indices:
        client.request("torrents/filePrio", {"hash": torrent_hash, "id": "|".join(map(str, keep_indices)), "priority": "1"})
    if skip_indices:
        client.request("torrents/filePrio", {"hash": torrent_hash, "id": "|".join(map(str, skip_indices)), "priority": "0"})
    return info, files, transfer_size


def command_inspect(client: QbtClient, args: argparse.Namespace) -> dict:
    torrent_hash = normalize_hash(args.hash)
    deadline = time.monotonic() + args.wait
    while True:
        try:
            info, files = validate_torrent(
                client, torrent_hash, args.allow_oversize, series=args.series,
                preferred_file_index=getattr(args, "preferred_file_index", None),
            )
            break
        except QbtError as exc:
            if "metadata is not available" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(2)
    return {
        "hash": torrent_hash,
        "name": info.get("name"),
        "state": info.get("state"),
        "reported_total": int(info.get("total_size", 0)),
        "selected_size": files["selected_size"],
        "selected_size_gib": round(files["selected_size"] / GIB, 2),
        "main_feature": files["main_feature"],
        "episodes": files["episodes"],
        "skipped_by_default": files["skip_indices"],
        "discarded_files": files["discarded_by_default"],
        "files": files["files"],
        "safe_to_start": True,
    }


def command_fetch_metadata(client: QbtClient, args: argparse.Namespace) -> dict:
    torrent_hash = normalize_hash(args.hash)
    if not args.commit:
        raise QbtError("refusing to start metadata exchange without --commit")
    info = torrent_info(client, torrent_hash)
    state = str(info.get("state", "")).lower()
    if not any(token in state for token in ("paused", "stopped", "error", "metadl")):
        raise QbtError(f"torrent must be stopped before metadata fetch; current state is {state or 'unknown'}")
    existing = torrent_files(client, torrent_hash)
    if existing:
        return {"metadata_available": True, "hash": torrent_hash, "started": False, "files": len(existing)}
    old_limit = int(info.get("dl_limit", 0) or 0)
    if old_limit < 0:
        old_limit = 0
    client.request("torrents/setDownloadLimit", {"hashes": torrent_hash, "limit": str(2 * 1024 * 1024)})
    started = False
    files: list[dict] = []
    try:
        client.request("torrents/start", {"hashes": torrent_hash})
        started = True
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            files = torrent_files(client, torrent_hash)
            if files:
                break
            time.sleep(2)
    finally:
        try:
            if started:
                client.request("torrents/stop", {"hashes": torrent_hash})
        finally:
            client.request("torrents/setDownloadLimit", {"hashes": torrent_hash, "limit": str(old_limit)})
    if not files:
        raise QbtError(f"metadata did not arrive within {args.wait} seconds; torrent was stopped")
    return {
        "metadata_available": True,
        "hash": torrent_hash,
        "files": len(files),
        "torrent_stopped": True,
        "temporary_content_limit_bytes_per_second": 2 * 1024 * 1024,
        "next": "run inspect before starting content transfer",
    }


def command_start(client: QbtClient, args: argparse.Namespace) -> dict:
    torrent_hash = normalize_hash(args.hash)
    if not args.commit:
        raise QbtError("refusing to start content transfer without --commit")
    info, _files, transfer_size = configure_selection(
        client, torrent_hash, include_extras=args.include_extras,
        allow_oversize=args.allow_oversize, series=args.series,
        preferred_file_index=getattr(args, "preferred_file_index", None),
    )
    client.request("torrents/setForceStart", {"hashes": torrent_hash, "value": "true"})
    client.request("torrents/start", {"hashes": torrent_hash})
    return {
        "started": True,
        "hash": torrent_hash,
        "name": info.get("name"),
        "selected_size": transfer_size,
        "extras_included": bool(args.include_extras),
    }


def command_stop(client: QbtClient, args: argparse.Namespace) -> dict:
    torrent_hash = normalize_hash(args.hash)
    if not args.commit:
        raise QbtError("refusing to change qBittorrent state without --commit")
    client.request("torrents/stop", {"hashes": torrent_hash})
    return {"stopped": True, "hash": torrent_hash}


def checked_movies_nerd_transfer(info: dict, torrent_hash: str) -> Path:
    tags = {value.strip().casefold() for value in str(info.get("tags") or "").split(",")}
    if "movies-nerd" not in tags:
        raise QbtError("refusing to remove a torrent not owned by Movies Nerd")
    actual = Path(str(info.get("save_path") or "")).resolve(strict=False)
    expected = [
        root.resolve(strict=False) / "transfers" / torrent_hash
        for root in staging_roots()
    ]
    if actual not in expected:
        raise QbtError("refusing to remove a torrent outside its exact Movies Nerd staging directory")
    return actual


def remove_movies_nerd_torrent(client: QbtClient, torrent_hash: str) -> dict:
    """Remove one exact Movies Nerd torrent and its dedicated staged payload."""
    normalized = normalize_hash(torrent_hash)
    info = torrent_info(client, normalized)
    transfer = checked_movies_nerd_transfer(info, normalized)
    client.request("torrents/stop", {"hashes": normalized})
    client.request("torrents/delete", {"hashes": normalized, "deleteFiles": "true"})
    deadline = time.monotonic() + 10
    absent = False
    while time.monotonic() < deadline:
        try:
            torrent_info(client, normalized)
        except QbtError as exc:
            if "not present" in str(exc):
                absent = True
                break
            raise
        time.sleep(0.1)
    if not absent:
        raise QbtError("qBittorrent did not remove the exact Movies Nerd candidate promptly")
    if transfer.exists():
        if transfer.is_symlink() or not transfer.is_dir():
            raise QbtError("refusing to clean an unsafe Movies Nerd transfer path")
        shutil.rmtree(transfer)
    sidecar = transfer.with_name("._" + transfer.name)
    if sidecar.is_file() and not sidecar.is_symlink():
        sidecar.unlink(missing_ok=True)
    return {"removed": True, "hash": normalized, "staged_payload_removed": not transfer.exists()}


def command_remove(client: QbtClient, args: argparse.Namespace) -> dict:
    if not args.commit:
        raise QbtError("refusing to remove a torrent without --commit")
    return remove_movies_nerd_torrent(client, args.hash)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="test the loopback Web API")
    add = sub.add_parser("add-paused", help="add a magnet in stopped state")
    add.add_argument("--magnet", required=True)
    add.add_argument("--kind", choices=("movie", "series"), required=True)
    add.add_argument("--rename")
    add.add_argument("--commit", action="store_true")
    inspect = sub.add_parser("inspect", help="validate qBittorrent metadata and payload")
    inspect.add_argument("--hash", required=True)
    inspect.add_argument("--wait", type=int, choices=range(0, 121), default=0, metavar="SECONDS")
    inspect.add_argument("--allow-oversize", action="store_true")
    inspect.add_argument("--preferred-file-index", type=int)
    inspect.add_argument("--series", action="store_true", help="keep all non-extra episode videos")
    metadata = sub.add_parser("fetch-metadata", help="briefly fetch magnet metadata under a bounded 2 MiB/s limit")
    metadata.add_argument("--hash", required=True)
    metadata.add_argument("--wait", type=int, choices=range(10, 61), default=25, metavar="SECONDS")
    metadata.add_argument("--commit", action="store_true")
    start = sub.add_parser("start", help="deselect extras and start a validated torrent")
    start.add_argument("--hash", required=True)
    start.add_argument("--include-extras", action="store_true")
    start.add_argument("--allow-oversize", action="store_true")
    start.add_argument("--preferred-file-index", type=int)
    start.add_argument("--series", action="store_true", help="keep all non-extra episode videos")
    start.add_argument("--commit", action="store_true")
    stop = sub.add_parser("stop", help="stop one torrent")
    stop.add_argument("--hash", required=True)
    stop.add_argument("--commit", action="store_true")
    remove = sub.add_parser("remove", help="remove one exact Movies Nerd torrent and staged payload")
    remove.add_argument("--hash", required=True)
    remove.add_argument("--commit", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        client = connected_client()
        handlers = {
            "status": command_status,
            "add-paused": command_add,
            "inspect": command_inspect,
            "fetch-metadata": command_fetch_metadata,
            "start": command_start,
            "stop": command_stop,
            "remove": command_remove,
        }
        print(json.dumps(handlers[args.command](client, args), indent=2, sort_keys=True))
        return 0
    except QbtAccessDenied as exc:
        print(json.dumps({
            "error": str(exc),
            "needs_local_app_access": True,
            "user_action_required": False,
        }, indent=2), file=sys.stderr)
        return 6
    except (QbtError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
