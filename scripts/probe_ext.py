#!/usr/bin/env python3
"""Probe only the fixed EXT host allowlist; never bypass access controls."""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

MIRRORS = (
    "https://ext.to/",
    "https://search.extto.com/",
    "https://extto.com/",
)
ALLOWED_HOSTS = {urllib.parse.urlparse(url).hostname for url in MIRRORS}
MAX_BODY = 64 * 1024


class AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise urllib.error.URLError(f"redirect blocked: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def classify(body: bytes, headers) -> tuple[bool, str]:
    text = body.decode("utf-8", "ignore").lower()
    cloudflare = (
        headers.get("cf-mitigated", "").lower() == "challenge"
        or "just a moment" in text
        or "challenge-platform" in text
    )
    if cloudflare:
        return True, "cloudflare challenge; use an approved interactive browser handoff"
    return False, "reachable"


def probe(url: str, timeout: float) -> dict:
    started = time.monotonic()
    opener = urllib.request.build_opener(AllowlistRedirect())
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Movies-Nerd/2.0 (+read-only availability probe)",
            "Accept": "text/html,application/xhtml+xml",
            "Range": f"bytes=0-{MAX_BODY - 1}",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_BODY)
            challenged, note = classify(body, response.headers)
            return {
                "url": url,
                "final_url": response.geturl(),
                "http_status": response.status,
                "reachable": 200 <= response.status < 400 and not challenged,
                "cloudflare_challenge": challenged,
                "note": note,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_BODY)
        challenged, note = classify(body, exc.headers)
        return {
            "url": url,
            "final_url": exc.geturl(),
            "http_status": exc.code,
            "reachable": False,
            "cloudflare_challenge": challenged,
            "note": note if challenged else f"HTTP {exc.code}",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    except (urllib.error.URLError, socket.timeout, ssl.SSLError) as exc:
        return {
            "url": url,
            "http_status": None,
            "reachable": False,
            "cloudflare_challenge": False,
            "note": str(getattr(exc, "reason", exc)),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if not (1 <= args.timeout <= 20):
        parser.error("--timeout must be between 1 and 20 seconds")
    results = [probe(url, args.timeout) for url in MIRRORS]
    print(json.dumps(results, indent=2))
    return 0 if any(item["reachable"] for item in results) else 3


if __name__ == "__main__":
    raise SystemExit(main())
