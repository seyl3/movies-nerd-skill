#!/usr/bin/env python3
"""Read-only dependency and path check for Movies Nerd."""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sys
from urllib.parse import urlparse

from _common import library_configuration, library_roots


def command(name: str) -> dict:
    path = shutil.which(name)
    return {"available": path is not None, "path": path}


def root_status(path: Path) -> dict:
    result = {"path": str(path), "exists": path.is_dir(), "writable": os.access(path, os.W_OK)}
    if path.is_dir():
        usage = shutil.disk_usage(path)
        result["free_gib"] = round(usage.free / (1024**3), 2)
    return result


def qbt_endpoint(technical: bool = False) -> dict:
    raw = os.environ.get("QBITTORRENT_URL", "http://127.0.0.1:8080").rstrip("/")
    parsed = urlparse(raw)
    host = parsed.hostname
    loopback = host in {"127.0.0.1", "::1", "localhost"}
    result = {
        "reachable": False,
        "status": "not-ready",
        "message": "qBittorrent app isn't open",
    }
    if technical:
        result.update({"url": raw, "loopback": loopback})
    if not loopback or parsed.scheme != "http" or not host:
        if technical:
            result["error"] = "qBittorrent connection must stay on this computer"
        return result
    try:
        with socket.create_connection((host, parsed.port or 80), timeout=1):
            result["reachable"] = True
            result["status"] = "ready"
            result["message"] = "qBittorrent is ready"
    except OSError as exc:
        if getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM}:
            result["status"] = "needs-local-app-access"
            result["message"] = "qBittorrent check needs local-app access"
            result["retry_with_local_app_access"] = True
        if technical:
            result["error"] = str(exc)
    if technical:
        result["username_set"] = bool(os.environ.get("QBITTORRENT_USERNAME"))
        result["password_set"] = bool(os.environ.get("QBITTORRENT_PASSWORD"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--technical", action="store_true", help="include connection diagnostics for setup troubleshooting")
    args = parser.parse_args()
    movies_root, series_root = library_roots()
    report = {
        "platform": platform.platform(),
        "python": {"version": platform.python_version(), "supported": sys.version_info >= (3, 11)},
        "commands": {name: command(name) for name in ("ffmpeg", "ffprobe", "git", "mkvpropedit")},
        "qBittorrent": qbt_endpoint(args.technical),
        "subtitles": {
            "default_provider": "OpenSubtitles v3 for Stremio",
            "api_key_required": False,
            "optional_opensubtitles_api_key_configured": bool(
                os.environ.get("OPENSUBTITLES_API_KEY", "").strip()
            ),
        },
        "library_configuration": library_configuration(),
        "libraries": {"movies": root_status(movies_root), "series": root_status(series_root)},
    }
    required_ok = (
        report["python"]["supported"]
        and report["commands"]["ffmpeg"]["available"]
        and report["commands"]["ffprobe"]["available"]
        and report["libraries"]["movies"]["exists"]
    )
    report["ready_for_offline_media_work"] = required_ok
    report["ready_for_qbittorrent"] = required_ok and report["qBittorrent"]["reachable"]
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ready_for_qbittorrent"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
