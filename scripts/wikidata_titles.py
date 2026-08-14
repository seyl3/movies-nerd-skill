#!/usr/bin/env python3
"""Resolve no-key original-language and localized title facts from Wikidata."""

from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_ORIGIN = "https://www.wikidata.org"
API_HOST = "www.wikidata.org"
API_PATH = "/w/api.php"
MAX_SEARCH_BYTES = 1024 * 1024
MAX_ENTITY_BYTES = 5 * 1024 * 1024
USER_AGENT = "MoviesNerdSkill title-resolver"
IMDB_RE = re.compile(r"tt\d{5,12}")
QID_RE = re.compile(r"Q\d{1,12}")
COMMON_LANGUAGE_CODES = {
    "Q150": "fr", "Q1860": "en", "Q1321": "es", "Q188": "de",
    "Q652": "it", "Q5146": "pt", "Q5287": "ja", "Q9176": "ko",
    "Q7737": "ru", "Q7850": "zh", "Q1568": "hi", "Q7411": "nl",
    "Q809": "pl", "Q9027": "sv", "Q9035": "da", "Q9165": "fi",
    "Q9043": "no", "Q9288": "he", "Q13955": "ar", "Q9056": "cs",
}


class WikidataTitleError(RuntimeError):
    pass


def checked_url(url: str) -> str:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise WikidataTitleError("title metadata URL is invalid") from exc
    if (
        parsed.scheme != "https" or parsed.hostname != API_HOST
        or port not in {None, 443} or parsed.path != API_PATH
        or parsed.username is not None or parsed.password is not None or parsed.fragment
    ):
        raise WikidataTitleError("title metadata URL is outside the fixed HTTPS allowlist")
    return url


class FixedRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def system_curl(url: str, maximum: int, timeout: float) -> bytes:
    curl = shutil.which("curl")
    if not curl:
        raise WikidataTitleError("title metadata service is unavailable")
    try:
        completed = subprocess.run(
            [
                curl, "--fail", "--silent", "--show-error", "--proto", "=https",
                "--max-redirs", "0", "--max-time", str(timeout),
                "--max-filesize", str(maximum), "--header", "Accept: application/json",
                "--header", f"User-Agent: {USER_AGENT}", url,
            ],
            check=False, capture_output=True, timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WikidataTitleError("title metadata service is unavailable") from exc
    if completed.returncode != 0 or not completed.stdout or len(completed.stdout) > maximum:
        raise WikidataTitleError("title metadata request failed")
    return completed.stdout


def fetch_json(params: dict[str, object], maximum: int, timeout: float) -> dict:
    url = checked_url(API_ORIGIN + API_PATH + "?" + urlencode(params))
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with build_opener(FixedRedirects()).open(request, timeout=timeout) as response:
            checked_url(response.geturl())
            raw = response.read(maximum + 1)
    except HTTPError as exc:
        raise WikidataTitleError(f"title metadata service returned HTTP {exc.code}") from exc
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raw = system_curl(url, maximum, timeout)
        else:
            raise WikidataTitleError("title metadata service is unavailable") from exc
    except (OSError, TimeoutError) as exc:
        raise WikidataTitleError("title metadata service is unavailable") from exc
    if not raw or len(raw) > maximum:
        raise WikidataTitleError("title metadata response is empty or too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WikidataTitleError("title metadata service returned invalid JSON") from exc
    if not isinstance(value, dict) or value.get("error"):
        raise WikidataTitleError("title metadata service returned an unexpected response")
    return value


def claim_values(entity: dict, property_id: str) -> list[object]:
    claims = entity.get("claims") or {}
    statements = claims.get(property_id) if isinstance(claims, dict) else None
    if not isinstance(statements, list):
        return []
    output = []
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
            continue
        snak = statement.get("mainsnak") or {}
        data = snak.get("datavalue") if isinstance(snak, dict) else None
        if isinstance(data, dict) and data.get("value") is not None:
            output.append(data["value"])
    return output


def entity_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    candidate = str(value.get("id") or "")
    if not candidate and isinstance(value.get("numeric-id"), int):
        candidate = f"Q{value['numeric-id']}"
    return candidate if QID_RE.fullmatch(candidate) else None


def term(entity: dict, group: str, language: str) -> str | None:
    values = entity.get(group) or {}
    item = values.get(language) if isinstance(values, dict) else None
    if isinstance(item, dict):
        value = " ".join(str(item.get("value") or "").split())
        return value or None
    return None


def exact_entity(payload: dict, imdb_id: str) -> dict:
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        raise WikidataTitleError("title metadata response has no entities")
    matches = []
    for entity in entities.values():
        if not isinstance(entity, dict):
            continue
        values = {str(value).lower() for value in claim_values(entity, "P345")}
        if imdb_id in values:
            matches.append(entity)
    if len(matches) != 1:
        raise WikidataTitleError("IMDb ID did not resolve to one title metadata entity")
    return matches[0]


def title_facts(imdb_id: str, timeout: float = 8.0) -> dict:
    identifier = imdb_id.strip().lower()
    if not IMDB_RE.fullmatch(identifier) or not 1 <= timeout <= 20:
        raise WikidataTitleError("title metadata request is invalid")
    search = fetch_json({
        "action": "query", "list": "search",
        "srsearch": f"haswbstatement:P345={identifier}",
        "srnamespace": 0, "srlimit": 5, "srprop": "", "format": "json",
        "formatversion": 2,
    }, MAX_SEARCH_BYTES, timeout)
    rows = ((search.get("query") or {}).get("search") or [])
    qids = [str(row.get("title") or "") for row in rows if isinstance(row, dict)]
    qids = list(dict.fromkeys(qid for qid in qids if QID_RE.fullmatch(qid)))
    if not qids:
        raise WikidataTitleError("IMDb ID has no Wikidata title metadata")
    entity_payload = fetch_json({
        "action": "wbgetentities", "ids": "|".join(qids),
        "props": "labels|aliases|claims", "format": "json", "formatversion": 2,
    }, MAX_ENTITY_BYTES, timeout)
    entity = exact_entity(entity_payload, identifier)
    language_ids = list(dict.fromkeys(
        item for value in claim_values(entity, "P364")
        if (item := entity_id(value))
    ))
    original_values = [
        value for value in claim_values(entity, "P1476")
        if isinstance(value, dict) and str(value.get("text") or "").strip()
    ]
    language_codes = list(dict.fromkeys(
        code for code in (
            *[COMMON_LANGUAGE_CODES.get(qid) for qid in language_ids],
            *[str(value.get("language") or "").casefold() for value in original_values],
        ) if code and re.fullmatch(r"[a-z]{2,3}", code)
    ))
    original_value = next((
        value for code in language_codes for value in original_values
        if str(value.get("language") or "").casefold() == code
    ), None)
    if original_value is None and original_values:
        original_value = original_values[0]
    original = (
        " ".join(str(original_value.get("text") or "").split())
        if isinstance(original_value, dict) else None
    )
    original_title_language = (
        str(original_value.get("language") or "").casefold()
        if isinstance(original_value, dict) else None
    )
    if not original:
        original = next((
            term(entity, "labels", code) for code in language_codes
            if term(entity, "labels", code)
        ), None)
        original_title_language = next((
            code for code in language_codes if term(entity, "labels", code)
        ), None)
    policy_languages = [original_title_language] if original_title_language else language_codes
    return {
        "item_id": str(entity.get("id") or ""),
        "original_title": original,
        "original_languages": policy_languages,
        "film_languages": language_codes,
        "original_language_ids": language_ids,
        "french_title": term(entity, "labels", "fr"),
        "english_title": term(entity, "labels", "en"),
    }
