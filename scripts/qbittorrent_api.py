#!/usr/bin/env python3
"""Minimal, loopback-only qBittorrent Web API client for Movies Nerd."""

from __future__ import annotations

import argparse
import base64
import binascii
import http.cookiejar
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener
import uuid

from _common import GIB, format_gib, staging_roots
from payload_safety import BIDI_RE, MAX_PAYLOAD_FILES, VIDEO_EXTENSIONS, filename_reasons

MAX_BYTES = 15 * GIB
LOOPBACKS = {"127.0.0.1", "::1", "localhost"}
EXTRA_RE = re.compile(r"(?:^|[\\/._ -])(sample|trailer|featurette|interview|deleted[ ._-]?scene|behind[ ._-]?the[ ._-]?scenes|bonus)(?:$|[\\/._ -])", re.I)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class QbtError(RuntimeError):
    pass


def checked_base_url(raw: str) -> str:
    value = raw.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACKS or parsed.username or parsed.password:
        raise QbtError("QBITTORRENT_URL must be an HTTP loopback URL without embedded credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise QbtError("QBITTORRENT_URL must contain only scheme, loopback host, and optional port")
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


def multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = "----MoviesNerd" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise QbtError("invalid multipart field name")
        chunks.extend([
            f"--{boundary}\r\n".encode(),
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
            raise QbtError("set both QBITTORRENT_USERNAME and QBITTORRENT_PASSWORD")
        if username and password:
            response = self.request("auth/login", {"username": username, "password": password})
            if response.strip() != b"Ok.":
                raise QbtError("qBittorrent authentication failed")

    def request(self, endpoint: str, fields: dict[str, str] | None = None, multipart_body: bool = False) -> bytes:
        url = f"{self.base_url}/api/v2/{endpoint}"
        headers = {"Referer": self.base_url, "Origin": self.base_url, "User-Agent": "Movies-Nerd/1"}
        data = None
        if fields is not None:
            if multipart_body:
                data, content_type = multipart(fields)
                headers["Content-Type"] = content_type
            else:
                data = urlencode(fields).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=headers, method="POST" if fields is not None else "GET")
        try:
            with self.opener.open(request, timeout=8) as response:
                return response.read(16 * 1024 * 1024)
        except HTTPError as exc:
            detail = exc.read(1024).decode("utf-8", "replace").strip()
            if exc.code == 403:
                raise QbtError("qBittorrent rejected authentication; check Web UI credentials and IP bans") from exc
            raise QbtError(f"qBittorrent HTTP {exc.code}: {detail or exc.reason}") from exc
        except URLError as exc:
            raise QbtError(f"cannot reach qBittorrent at {self.base_url}: {exc.reason}") from exc

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


def safe_stage(kind: str) -> Path:
    movie_stage, series_stage = staging_roots()
    return movie_stage if kind == "movie" else series_stage


def classify_files(files: list[dict], series: bool = False) -> dict:
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
        if reasons:
            unsafe.append(record)
        if suffix in VIDEO_EXTENSIONS and not extra and not reasons:
            videos.append(record)
    main = max(videos, key=lambda item: item["size"], default=None)
    keep = []
    skip = []
    for item in normalized:
        suffix = PurePosixPath(item["name"]).suffix.lower()
        if item["unsafe_reasons"] or item["extra"]:
            skip.append(item["index"])
        elif not series and suffix in VIDEO_EXTENSIONS and main and item["index"] != main["index"]:
            skip.append(item["index"])
        else:
            keep.append(item["index"])
    selected_size = sum(item["size"] for item in normalized if item["index"] in keep)
    return {
        "files": normalized,
        "unsafe": unsafe,
        "main_feature": main,
        "episodes": videos if series else [],
        "keep_indices": keep,
        "skip_indices": skip,
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


def validate_torrent(client: QbtClient, torrent_hash: str, allow_oversize: bool = False, series: bool = False) -> tuple[dict, dict]:
    info = torrent_info(client, torrent_hash)
    files = classify_files(torrent_files(client, torrent_hash), series=series)
    stage_roots = tuple(root.resolve(strict=False) for root in staging_roots())
    raw_save = Path(str(info.get("save_path", ""))).resolve(strict=False)
    if not any(raw_save == root or root in raw_save.parents for root in stage_roots):
        raise QbtError(f"torrent save path is outside Movies Nerd staging: {raw_save}")
    if files["unsafe"]:
        names = ", ".join(item["name"] for item in files["unsafe"][:5])
        raise QbtError(f"torrent contains unsafe or unexpected payload files: {names}")
    if not files["files"]:
        raise QbtError("torrent metadata is not available yet; leave it stopped briefly and inspect again")
    if not files["main_feature"]:
        raise QbtError("no main video feature or episode was identified")
    if files["selected_size"] > MAX_BYTES and not allow_oversize:
        raise QbtError(f"selected payload is {format_gib(files['selected_size'])}, above the 15 GiB limit")
    return info, files


def command_status(client: QbtClient, _args: argparse.Namespace) -> dict:
    version = client.request("app/version").decode("utf-8", "replace").strip()
    api = client.request("app/webapiVersion").decode("utf-8", "replace").strip()
    return {"connected": True, "qBittorrent": version, "web_api": api, "url": client.base_url}


def command_add(client: QbtClient, args: argparse.Namespace) -> dict:
    torrent_hash = magnet_hash(args.magnet)
    if not args.commit:
        raise QbtError("refusing to add torrent without --commit")
    stage = safe_stage(args.kind)
    if stage.is_symlink():
        raise QbtError("staging root must not be a symlink")
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        stage.chmod(0o700)
    except OSError as exc:
        raise QbtError(f"cannot restrict staging permissions: {exc}") from exc
    fields = {
        "urls": args.magnet,
        "savepath": str(stage),
        "tags": f"movies-nerd,{args.kind}",
        "paused": "true",
        "root_folder": "true",
        "autoTMM": "false",
    }
    if args.rename:
        if CONTROL_RE.search(args.rename) or BIDI_RE.search(args.rename) or "/" in args.rename or "\\" in args.rename or args.rename in {".", ".."}:
            raise QbtError("rename must be a single safe path component")
        fields["rename"] = args.rename
    response = client.request("torrents/add", fields, multipart_body=True).decode("utf-8", "replace").strip()
    if response not in ("", "Ok."):
        raise QbtError(f"qBittorrent rejected the torrent: {response}")
    return {"added_stopped": True, "hash": torrent_hash, "staging": str(stage), "next": "inspect metadata before start"}


def command_inspect(client: QbtClient, args: argparse.Namespace) -> dict:
    torrent_hash = normalize_hash(args.hash)
    deadline = time.monotonic() + args.wait
    while True:
        try:
            info, files = validate_torrent(client, torrent_hash, args.allow_oversize, series=args.series)
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
    client.request("torrents/setDownloadLimit", {"hashes": torrent_hash, "limit": "1024"})
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
        "temporary_content_limit_bytes_per_second": 1024,
        "next": "run inspect before starting content transfer",
    }


def command_start(client: QbtClient, args: argparse.Namespace) -> dict:
    torrent_hash = normalize_hash(args.hash)
    if not args.commit:
        raise QbtError("refusing to start content transfer without --commit")
    info, files = validate_torrent(client, torrent_hash, args.allow_oversize, series=args.series)
    transfer_size = sum(item["size"] for item in files["files"]) if args.include_extras else files["selected_size"]
    if transfer_size > MAX_BYTES and not args.allow_oversize:
        raise QbtError(f"actual selected transfer is {format_gib(transfer_size)}, above the 15 GiB limit")
    if args.include_extras:
        keep_indices = [item["index"] for item in files["files"]]
        skip_indices = []
    else:
        keep_indices = files["keep_indices"]
        skip_indices = files["skip_indices"]
    if keep_indices:
        client.request("torrents/filePrio", {"hash": torrent_hash, "id": "|".join(map(str, keep_indices)), "priority": "1"})
    if skip_indices:
        client.request("torrents/filePrio", {"hash": torrent_hash, "id": "|".join(map(str, skip_indices)), "priority": "0"})
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
    inspect.add_argument("--series", action="store_true", help="keep all non-extra episode videos")
    metadata = sub.add_parser("fetch-metadata", help="briefly start at 1 KiB/s until magnet metadata arrives")
    metadata.add_argument("--hash", required=True)
    metadata.add_argument("--wait", type=int, choices=range(10, 121), default=60, metavar="SECONDS")
    metadata.add_argument("--commit", action="store_true")
    start = sub.add_parser("start", help="deselect extras and start a validated torrent")
    start.add_argument("--hash", required=True)
    start.add_argument("--include-extras", action="store_true")
    start.add_argument("--allow-oversize", action="store_true")
    start.add_argument("--series", action="store_true", help="keep all non-extra episode videos")
    start.add_argument("--commit", action="store_true")
    stop = sub.add_parser("stop", help="stop one torrent")
    stop.add_argument("--hash", required=True)
    stop.add_argument("--commit", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        client = client_from_env()
        handlers = {
            "status": command_status,
            "add-paused": command_add,
            "inspect": command_inspect,
            "fetch-metadata": command_fetch_metadata,
            "start": command_start,
            "stop": command_stop,
        }
        print(json.dumps(handlers[args.command](client, args), indent=2, sort_keys=True))
        return 0
    except (QbtError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
