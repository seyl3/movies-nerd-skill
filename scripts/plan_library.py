#!/usr/bin/env python3
"""Produce deterministic final library paths without mutating the filesystem."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from _common import library_roots


def component(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = value.replace("/", " - ").replace(":", " - ")
    value = value.replace("?", "").replace("*", "")
    value = " ".join(value.split()).strip(" .")
    if not value or value in {".", ".."}:
        raise ValueError("invalid empty or traversal-like path component")
    return value


def resolution(value: str) -> str:
    match = re.search(r"(4320|2160|1080|720|576|480)", value)
    if not match:
        raise ValueError("resolution must contain 4320, 2160, 1080, 720, 576, or 480")
    return match.group(1) + "p"


def movie_plan(args) -> dict:
    title = component(args.title)
    director = component(args.director)
    res = resolution(args.resolution)
    movies_root, _series_root = library_roots()
    folder = movies_root / director / f"{title} ({args.year})"
    base = f"{title} ({args.year}) [{res}]"
    return {
        "kind": "movie",
        "folder": str(folder),
        "video": str(folder / f"{base}.mkv"),
        "nfo": str(folder / f"{base}.nfo"),
        "poster": str(folder / f"{title} ({args.year}).png"),
        "fanart": str(folder / "fanart.jpg"),
        "clearlogo": str(folder / "clearlogo.png"),
        "english_subtitle": str(folder / f"{base}.en.srt"),
        "french_subtitle": str(folder / f"{base}.fr.srt"),
    }


def episode_plan(args) -> dict:
    title = component(args.title)
    episode_title = component(args.episode_title)
    res = resolution(args.resolution)
    _movies_root, series_root = library_roots()
    show = series_root / f"{title} ({args.year})"
    season = show / f"Season {args.season:02d}"
    episode_end = getattr(args, "episode_end", None)
    if episode_end is not None and episode_end <= args.episode:
        raise ValueError("multi-episode end must be greater than its first episode")
    code = f"S{args.season:02d}E{args.episode:02d}"
    if episode_end is not None:
        code += f"-E{episode_end:02d}"
    base = f"{title} ({args.year}) - {code} - {episode_title} [{res}]"
    return {
        "kind": "episode",
        "show_folder": str(show),
        "season_folder": str(season),
        "video": str(season / f"{base}.mkv"),
        "nfo": str(season / f"{base}.nfo"),
        "thumbnail": str(season / f"{base}.jpg"),
        "english_subtitle": str(season / f"{base}.en.srt"),
        "french_subtitle": str(season / f"{base}.fr.srt"),
        "tvshow_nfo": str(show / "tvshow.nfo"),
        "poster": str(show / "poster.jpg"),
        "fanart": str(show / "fanart.jpg"),
        "clearlogo": str(show / "clearlogo.png"),
        "season_poster": str(show / f"season{args.season:02d}-poster.jpg"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="kind", required=True)
    movie = sub.add_parser("movie")
    movie.add_argument("--title", required=True)
    movie.add_argument("--year", type=int, required=True)
    movie.add_argument("--director", required=True)
    movie.add_argument("--resolution", required=True)
    episode = sub.add_parser("episode")
    episode.add_argument("--title", required=True)
    episode.add_argument("--year", type=int, required=True)
    episode.add_argument("--season", type=int, required=True)
    episode.add_argument("--episode", type=int, required=True)
    episode.add_argument("--episode-end", type=int)
    episode.add_argument("--episode-title", required=True)
    episode.add_argument("--resolution", required=True)
    args = parser.parse_args()
    if not (1888 <= args.year <= 2100):
        parser.error("year is outside the supported range")
    if args.kind == "episode" and not (
        0 <= args.season <= 99 and 0 <= args.episode <= 999
        and (args.episode_end is None or 0 <= args.episode_end <= 999)
    ):
        parser.error("season or episode number is outside the supported range")
    plan = movie_plan(args) if args.kind == "movie" else episode_plan(args)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")
