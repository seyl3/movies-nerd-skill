#!/usr/bin/env python3
"""Choose search aliases and final library titles from authoritative title facts."""

from __future__ import annotations

import re
import unicodedata

CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
LANGUAGE_ALIASES = {
    "eng": "en",
    "english": "en",
    "fre": "fr",
    "fra": "fr",
    "french": "fr",
    "spa": "es",
    "spanish": "es",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "ita": "it",
    "italian": "it",
    "por": "pt",
    "portuguese": "pt",
    "jpn": "ja",
    "japanese": "ja",
    "kor": "ko",
    "korean": "ko",
    "rus": "ru",
    "russian": "ru",
    "zho": "zh",
    "chi": "zh",
    "chinese": "zh",
    "hin": "hi",
    "hindi": "hi",
}


def clean_title(value: object) -> str | None:
    title = " ".join(str(value or "").split()).strip()
    if not title or CONTROL_RE.search(title) or len(title.encode("utf-8")) > 300:
        return None
    return unicodedata.normalize("NFC", title)


def title_key(value: object) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(character for character in folded if character.isalnum())


def language_code(value: object) -> str | None:
    code = str(value or "").strip().casefold().replace("_", "-")
    code = LANGUAGE_ALIASES.get(code, code)
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", code):
        return None
    return code.split("-", 1)[0]


def unique_titles(*values: object, limit: int = 3) -> list[str]:
    output = []
    seen = set()
    for value in values:
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            title = clean_title(item)
            key = title_key(title)
            if not title or not key or key in seen:
                continue
            output.append(title)
            seen.add(key)
            if len(output) == limit:
                return output
    return output


def display_with_original(french_title: str, original_title: str) -> str:
    if title_key(french_title) == title_key(original_title):
        return french_title
    return f"{french_title} ({original_title})"


def decide(
    *, requested_title: object, canonical_title: object = None,
    original_title: object = None, french_title: object = None,
    english_title: object = None, original_languages: object = None,
) -> dict:
    requested = clean_title(requested_title)
    if not requested:
        raise ValueError("requested title is empty or unsafe")
    canonical = clean_title(canonical_title) or requested
    original = clean_title(original_title) or canonical
    french = clean_title(french_title) or requested
    english = clean_title(english_title) or canonical
    raw_languages = (
        original_languages if isinstance(original_languages, (list, tuple, set))
        else [original_languages]
    )
    languages = []
    for raw in raw_languages:
        code = language_code(raw)
        if code and code not in languages:
            languages.append(code)

    if "en" in languages or "fr" in languages:
        display = original
    elif languages:
        display = display_with_original(french, original)
    else:
        display = canonical

    if "fr" in languages:
        search = unique_titles(original, canonical, english, requested, french)
    elif "en" in languages:
        search = unique_titles(original, english, canonical, requested, french)
    elif languages:
        search = unique_titles(english, canonical, original, french, requested)
    else:
        search = unique_titles(canonical, requested, original, english, french)

    return {
        "requested_title": requested,
        "canonical_title": canonical,
        "original_title": original,
        "french_title": french,
        "english_title": english,
        "original_languages": languages,
        "display_title": display,
        "search_titles": search,
        "resolved": bool(languages),
    }


def from_job(job: dict) -> dict:
    cache = job.get("cache") or {}
    value = cache.get("title_policy") if isinstance(cache, dict) else None
    return value if isinstance(value, dict) else {}


def library_title(job: dict) -> str:
    identity = job.get("identity") or {}
    return (
        clean_title(from_job(job).get("display_title"))
        or clean_title(identity.get("title"))
        or "Untitled"
    )


def search_titles(job: dict) -> list[str]:
    identity = job.get("identity") or {}
    values = from_job(job).get("search_titles")
    return unique_titles(values or [], identity.get("title"))
