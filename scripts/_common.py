#!/usr/bin/env python3
"""Shared, dependency-free helpers for Movies Nerd scripts."""

from __future__ import annotations

import os
from pathlib import Path
import re

GIB = 1024 ** 3
SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b)?\s*$", re.I)
MOVIES_ROOT_ENV = "MOVIES_NERD_MOVIES_ROOT"
SERIES_ROOT_ENV = "MOVIES_NERD_SERIES_ROOT"


def _checked_library_root(raw: str, label: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} root must be an absolute path")
    resolved = path.resolve(strict=False)
    home = Path.home().resolve(strict=False)
    broad_mount = len(resolved.parts) <= 3 and len(resolved.parts) > 1 and resolved.parts[1] == "Volumes"
    if resolved in {Path("/"), home} or broad_mount:
        raise ValueError(f"{label} root is too broad: {resolved}")
    return resolved


def library_roots(environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    values = os.environ if environ is None else environ
    movies = _checked_library_root(values.get(MOVIES_ROOT_ENV) or str(Path.home() / "Documents" / "Movies"), "movies")
    series = _checked_library_root(values.get(SERIES_ROOT_ENV) or str(Path.home() / "Documents" / "Series"), "series")
    if movies == series or movies in series.parents or series in movies.parents:
        raise ValueError("movies and series roots must be distinct, non-nested directories")
    return movies, series


def staging_roots(environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    movies, series = library_roots(environ)
    return movies / ".incoming" / "Movies Nerd", series / ".incoming" / "Movies Nerd"


def state_roots(environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    """Return the persistent, non-payload state roots for each library."""
    movies, series = library_roots(environ)
    return movies / ".movies-nerd", series / ".movies-nerd"


def root_for_kind(kind: str, environ: dict[str, str] | None = None) -> Path:
    if kind not in {"movie", "series"}:
        raise ValueError("kind must be movie or series")
    movies, series = library_roots(environ)
    return movies if kind == "movie" else series


def stage_for_kind(kind: str, environ: dict[str, str] | None = None) -> Path:
    if kind not in {"movie", "series"}:
        raise ValueError("kind must be movie or series")
    movies, series = staging_roots(environ)
    return movies if kind == "movie" else series


def state_for_kind(kind: str, environ: dict[str, str] | None = None) -> Path:
    if kind not in {"movie", "series"}:
        raise ValueError("kind must be movie or series")
    movies, series = state_roots(environ)
    return movies if kind == "movie" else series


def library_configuration(environ: dict[str, str] | None = None) -> dict:
    values = os.environ if environ is None else environ
    movies, series = library_roots(values)
    movie_stage, series_stage = staging_roots(values)
    movie_state, series_state = state_roots(values)
    return {
        "movies_root": str(movies),
        "series_root": str(series),
        "movie_staging": str(movie_stage),
        "series_staging": str(series_stage),
        "movie_state": str(movie_state),
        "series_state": str(series_state),
        "movies_source": "environment" if values.get(MOVIES_ROOT_ENV) else "default",
        "series_source": "environment" if values.get(SERIES_ROOT_ENV) else "default",
    }


def parse_size(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a size")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("size cannot be negative")
        return value
    if isinstance(value, float):
        if value < 0:
            raise ValueError("size cannot be negative")
        return int(value)
    match = SIZE_RE.match(str(value))
    if not match:
        raise ValueError(f"invalid size: {value!r}")
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    multipliers = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000 ** 2,
        "mib": 1024 ** 2,
        "gb": 1000 ** 3,
        "gib": GIB,
        "tb": 1000 ** 4,
        "tib": 1024 ** 4,
    }
    return int(number * multipliers[unit])


def format_gib(size_bytes: int) -> str:
    return f"{size_bytes / GIB:.2f} GiB"


def first_value(mapping: dict, names: tuple[str, ...], default=None):
    for name in names:
        value = mapping.get(name)
        if value is not None and value != "":
            return value
    return default


def remove_appledouble_sibling(path: Path) -> bool:
    """Remove the exact AppleDouble companion macOS may create for *path*."""
    sidecar = path.with_name("._" + path.name)
    if not sidecar.exists():
        return False
    if sidecar.is_symlink() or not sidecar.is_file():
        raise ValueError(f"unsafe AppleDouble companion: {sidecar}")
    sidecar.unlink()
    return True


def clean_appledouble_tree(root: Path) -> list[str]:
    """Remove only regular ``._*`` files below one already-scoped root."""
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"AppleDouble cleanup root is unsafe: {root}")
    removed: list[str] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_directories = []
        for name in directories:
            candidate = current_path / name
            if candidate.is_symlink():
                continue
            if name.startswith("._"):
                raise ValueError(f"unsafe AppleDouble directory: {candidate}")
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in files:
            if not name.startswith("._"):
                continue
            candidate = current_path / name
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"unsafe AppleDouble companion: {candidate}")
            candidate.unlink()
            removed.append(str(candidate))
    return removed
