#!/usr/bin/env python3
"""Choose a credentialed or no-key subtitle workflow without exposing secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def checked_text(value: str, label: str, limit: int) -> str:
    clean = value.strip()
    if not clean or len(clean) > limit or CONTROL_RE.search(clean):
        raise ValueError(f"{label} is empty, too long, or contains control characters")
    return clean


def plan(title: str, year: int, release_name: str | None, languages: list[str], no_key_confirmed: bool, environ: dict[str, str]) -> dict:
    key_configured = bool(environ.get("OPENSUBTITLES_API_KEY", "").strip())
    base = {
        "title": checked_text(title, "title", 300),
        "year": year,
        "languages": languages,
        "release_name": checked_text(release_name, "release name", 500) if release_name else None,
        "credential": {"environment_variable": "OPENSUBTITLES_API_KEY", "configured": key_configured},
    }
    if key_configured:
        return base | {
            "action": "use-opensubtitles-api",
            "provider": "OpenSubtitles",
            "api_base": "https://api.opensubtitles.com/api/v1",
            "next": "search exact IDs/release, show candidates, then download a confirmed file ID to staging",
        }
    if not no_key_confirmed:
        return base | {
            "action": "ask-user-once",
            "question": "Do you have an OpenSubtitles API key you want to configure securely?",
            "if_no": "rerun with --user-has-no-key and continue with Subtitle Cat",
        }
    search_terms = [base["release_name"], f"{base['title']} {year}"]
    return base | {
        "action": "browser-fallback",
        "provider": "Subtitle Cat",
        "url": "https://subtitlecat.com/",
        "search_terms": [term for term in search_terms if term],
        "allowed_hosts": ["subtitlecat.com", "www.subtitlecat.com"],
        "next": "download an exact-match SRT to staging and validate before installation",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--release-name")
    parser.add_argument("--language", action="append", choices=("en", "fr"), dest="languages")
    parser.add_argument("--user-has-no-key", action="store_true")
    args = parser.parse_args()
    try:
        if not 1888 <= args.year <= 2100:
            raise ValueError("year is outside the supported range")
        languages = list(dict.fromkeys(args.languages or ["en", "fr"]))
        print(json.dumps(plan(args.title, args.year, args.release_name, languages, args.user_has_no_key, os.environ), ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
