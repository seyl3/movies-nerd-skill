#!/usr/bin/env python3
"""Build a compact recommendation inventory from shallow library structure."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import xml.etree.ElementTree as ET

from _common import root_for_kind

MOVIE_FOLDER_RE = re.compile(r"^(?P<title>.+) \((?P<year>(?:18|19|20|21)\d{2})\)$")
MAX_MOVIES = 10_000
MAX_NFO_BYTES = 256 * 1024
IGNORED_DIRECTORIES = {".incoming", ".movies-nerd"}


class InventoryError(ValueError):
    pass


def normalized(value: object) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def folder_identity(name: str) -> tuple[str, int] | None:
    match = MOVIE_FOLDER_RE.fullmatch(" ".join(name.split()))
    if not match:
        return None
    return match.group("title"), int(match.group("year"))


def nfo_director(movie: Path) -> str | None:
    try:
        entries = list(os.scandir(movie))
    except OSError:
        return None
    for entry in sorted(entries, key=lambda item: item.name.casefold()):
        if not entry.name.lower().endswith(".nfo") or entry.is_symlink():
            continue
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(entry.path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_NFO_BYTES:
                    continue
                data = handle.read(MAX_NFO_BYTES + 1)
            if len(data) > MAX_NFO_BYTES:
                continue
            root = ET.fromstring(data)
        except (OSError, ET.ParseError):
            continue
        director = " ".join((root.findtext("director") or "").split())
        return director[:200] or None
    return None


def scan(root: Path, *, maximum: int = MAX_MOVIES) -> dict:
    library = Path(root).expanduser().resolve(strict=False)
    if not library.exists():
        return {"owned_count": 0, "owned_by_director": {}}
    if not library.is_dir() or library.is_symlink():
        raise InventoryError("the movie library is not a safe directory")
    grouped: dict[str, list[str]] = {}
    count = 0
    try:
        directors = sorted(os.scandir(library), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise InventoryError("the movie library cannot be scanned") from exc
    for director_entry in directors:
        if (
            director_entry.name.startswith(".")
            or director_entry.name in IGNORED_DIRECTORIES
            or director_entry.is_symlink()
            or not director_entry.is_dir(follow_symlinks=False)
        ):
            continue
        direct_movie = folder_identity(director_entry.name)
        if direct_movie:
            movie_entries = [(director_entry, direct_movie)]
            parent_director = "Other"
        else:
            parent_director = " ".join(director_entry.name.split())[:200] or "Other"
            try:
                children = sorted(os.scandir(director_entry.path), key=lambda item: item.name.casefold())
            except OSError:
                continue
            movie_entries = [
                (child, identity) for child in children
                if not child.name.startswith(".") and not child.is_symlink()
                and child.is_dir(follow_symlinks=False)
                and (identity := folder_identity(child.name)) is not None
            ]
        for movie_entry, (title, year) in movie_entries:
            count += 1
            if count > maximum:
                raise InventoryError("the movie inventory exceeds its bounded scan limit")
            director = parent_director
            if normalized(parent_director) == "other":
                director = nfo_director(Path(movie_entry.path)) or "Other"
            label = f"{title} ({year})"
            grouped.setdefault(director, []).append(label)
    ordered = {
        director: sorted(set(titles), key=str.casefold)
        for director, titles in sorted(grouped.items(), key=lambda item: item[0].casefold())
    }
    return {"owned_count": sum(len(items) for items in ordered.values()), "owned_by_director": ordered}


def contains(inventory: dict, titles: list[str], year: int) -> bool:
    wanted = {normalized(title) for title in titles if str(title).strip()}
    for movies in (inventory.get("owned_by_director") or {}).values():
        for label in movies:
            identity = folder_identity(str(label))
            if identity and identity[1] == year and normalized(identity[0]) in wanted:
                return True
    return False


def contains_series(root: Path, titles: list[str], year: int) -> bool:
    library = Path(root).expanduser().resolve(strict=False)
    if not library.is_dir():
        return False
    wanted = {normalized(title) for title in titles if str(title).strip()}
    try:
        entries = os.scandir(library)
    except OSError:
        return False
    with entries:
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            identity = folder_identity(entry.name)
            if identity and identity[1] == year and normalized(identity[0]) in wanted:
                return True
    return False


def recommendation_context(
    root: Path, *, title: str, year: int, director: str,
) -> dict:
    inventory = scan(root)
    grouped = {
        str(name): list(movies)
        for name, movies in (inventory.get("owned_by_director") or {}).items()
    }
    clean_director = " ".join(str(director or "Other").split())[:200] or "Other"
    label = f"{' '.join(str(title).split())} ({int(year)})"
    current = grouped.setdefault(clean_director, [])
    if label not in current:
        current.append(label)
        current.sort(key=str.casefold)
    ordered_names = [clean_director] + sorted(
        (name for name in grouped if name != clean_director), key=str.casefold,
    )
    ordered = {name: grouped[name] for name in ordered_names}
    return {
        "owned_count": sum(len(items) for items in ordered.values()),
        "completed_director": clean_director,
        "owned_by_director": ordered,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path)
    args = parser.parse_args()
    try:
        result = scan(args.library or root_for_kind("movie"))
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (InventoryError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
