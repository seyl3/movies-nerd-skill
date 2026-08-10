#!/usr/bin/env python3
"""Shared, dependency-free helpers for Movies Nerd scripts."""

from __future__ import annotations

import re

GIB = 1024 ** 3
SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b)?\s*$", re.I)


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
