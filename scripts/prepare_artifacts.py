#!/usr/bin/env python3
"""Prepare metadata, artwork, and English/French subtitles without model orchestration."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time

import cinemeta
from _common import remove_appledouble_sibling
from check_subtitles import embedded_languages
from finalization_queue import artifact_root, mark, plan, start_all, task_state
from job_manifest import ManifestError, load_job
from opensubtitles_api import SubtitleApiError, api_json, api_key, fetch_download
from qbittorrent_api import (
    QbtError, checked_movies_nerd_transfer, connected_client, normalize_hash,
    torrent_info, validate_torrent,
)
from stremio_subtitles import (
    LANGUAGE_CODES, StremioSubtitleError, checked_download_url, fetch_srt, service_json,
)
from validate_subtitle import media_duration, validate_bytes

EPISODE_RE = re.compile(
    r"(?i)(?:^|[^A-Z0-9])S(\d{1,2})E(\d{1,3})(?:[-_. ]?E?(\d{1,3}))?"
)
IMAGE_SUFFIX = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"RIFF": ".webp",
}


class ArtifactPreparationError(RuntimeError):
    pass


def atomic_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        remove_appledouble_sibling(path)
        return path
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: dict) -> Path:
    return atomic_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def runtime_minutes(value: object) -> int | None:
    match = re.search(r"\d{1,4}", str(value or ""))
    if not match:
        return None
    parsed = int(match.group())
    return parsed if 1 <= parsed <= 1440 else None


def date_only(value: object) -> str | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or ""))
    return match.group(1) if match else None


def list_text(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [" ".join(str(item).split()) for item in value if str(item).strip()]


def unique_ids(meta: dict, imdb_id: str, *, series: bool) -> dict:
    result = {"imdb": imdb_id}
    tmdb = meta.get("moviedb_id")
    tvdb = meta.get("tvdb_id")
    if isinstance(tmdb, int) and tmdb > 0:
        result["tmdb"] = str(tmdb)
    if series and isinstance(tvdb, int) and tvdb > 0:
        result["tvdb"] = str(tvdb)
    return result


def movie_metadata(job: dict, meta: dict, imdb_id: str) -> dict:
    directors = list_text(meta.get("director"))
    if not directors:
        raise ArtifactPreparationError("authoritative movie metadata has no director")
    runtime = runtime_minutes(meta.get("runtime"))
    value = {
        "title": job["identity"]["title"],
        "originaltitle": str(meta.get("name") or job["identity"]["title"]),
        "year": int(job["identity"]["year"]),
        "plot": str(meta.get("description") or ""),
        "premiered": date_only(meta.get("released")),
        "runtime": runtime,
        "rating": meta.get("imdbRating"),
        "genres": list_text(meta.get("genre") or meta.get("genres")),
        "directors": directors,
        "country": str(meta.get("country") or ""),
        "uniqueids": unique_ids(meta, imdb_id, series=False),
        "default_uniqueid": "imdb",
        "letterboxd_url": f"https://letterboxd.com/imdb/{imdb_id}/",
        "recommendations": [],
    }
    return {key: child for key, child in value.items() if child is not None and child != ""}


def show_metadata(job: dict, meta: dict, imdb_id: str) -> dict:
    runtime = runtime_minutes(meta.get("runtime"))
    value = {
        "title": job["identity"]["title"],
        "originaltitle": str(meta.get("name") or job["identity"]["title"]),
        "year": int(job["identity"]["year"]),
        "plot": str(meta.get("description") or ""),
        "premiered": date_only(meta.get("released")),
        "status": str(meta.get("status") or ""),
        "runtime": runtime,
        "rating": meta.get("imdbRating"),
        "genres": list_text(meta.get("genre") or meta.get("genres")),
        "uniqueids": unique_ids(meta, imdb_id, series=True),
        "default_uniqueid": "imdb",
    }
    return {key: child for key, child in value.items() if child is not None and child != ""}


def image_name(data: bytes, stem: str) -> str:
    for signature, suffix in IMAGE_SUFFIX.items():
        if data.startswith(signature):
            if signature == b"RIFF" and data[8:12] != b"WEBP":
                continue
            return stem + suffix
    raise ArtifactPreparationError("prepared artwork is not a supported image")


def prepare_movie_artwork(root: Path, imdb_id: str) -> Path:
    poster_data = cinemeta.artwork(imdb_id, "poster")
    poster = atomic_bytes(root / image_name(poster_data, "poster"), poster_data)
    try:
        fanart_data = cinemeta.artwork(imdb_id, "background")
        if fanart_data.startswith(b"\xff\xd8\xff"):
            atomic_bytes(root / "fanart.jpg", fanart_data)
    except cinemeta.CinemetaError:
        pass
    try:
        logo_data = cinemeta.artwork(imdb_id, "logo")
        if logo_data.startswith(b"\x89PNG\r\n\x1a\n"):
            atomic_bytes(root / "clearlogo.png", logo_data)
    except cinemeta.CinemetaError:
        pass
    return poster


def active_episode_sources(job_path: Path, stop: threading.Event | None) -> list[dict]:
    while True:
        if stop and stop.is_set():
            raise ArtifactPreparationError("artifact preparation was cancelled")
        _, job = load_job(job_path)
        if job.get("state") in {"downloaded", "finalizing", "verified"}:
            break
        if job.get("state") in {"failed", "imported"}:
            raise ArtifactPreparationError("series artifacts cannot be prepared in this job state")
        time.sleep(0.5)
    raw_hash = (job.get("controller") or {}).get("active_hash") or (job.get("artifacts") or {}).get("torrent_hash")
    if not raw_hash:
        raise ArtifactPreparationError("completed series job has no transfer identity")
    info_hash = normalize_hash(str(raw_hash))
    client = connected_client(wait_seconds=20)
    info, files = validate_torrent(client, info_hash, series=True)
    transfer = checked_movies_nerd_transfer(info, info_hash)
    release_title = str((job.get("release") or {}).get("title") or "")
    selected = []
    for item in files.get("episodes") or []:
        source = str(item.get("name") or "")
        match = EPISODE_RE.search(source)
        if match is None and len(files.get("episodes") or []) == 1:
            match = EPISODE_RE.search(release_title)
        if match is None:
            raise ArtifactPreparationError(f"cannot identify the episode number for {source}")
        season, episode = int(match.group(1)), int(match.group(2))
        episode_end = int(match.group(3)) if match.group(3) else None
        media = transfer / source
        duration = media_duration(media.resolve(strict=True)) if media.is_file() else None
        languages = {
            item["language"] for item in embedded_languages(media.resolve(strict=True))
        } if media.is_file() else set()
        selected.append({
            "source": source, "season": season, "episode": episode,
            "episode_end": episode_end, "duration": duration,
            "languages": sorted(languages),
        })
    if not selected:
        raise ArtifactPreparationError("completed series transfer has no selected episodes")
    return selected


def series_episodes(meta: dict, sources: list[dict]) -> list[dict]:
    catalog = {}
    for raw in meta.get("videos") or []:
        if not isinstance(raw, dict):
            continue
        try:
            key = (int(raw.get("season")), int(raw.get("episode") or raw.get("number")))
        except (TypeError, ValueError):
            continue
        catalog[key] = raw
    output = []
    for source in sources:
        first = catalog.get((source["season"], source["episode"]))
        if not first:
            raise ArtifactPreparationError(
                f"authoritative metadata lacks S{source['season']:02d}E{source['episode']:02d}"
            )
        title = str(first.get("name") or f"Episode {source['episode']}")
        if source.get("episode_end"):
            last = catalog.get((source["season"], source["episode_end"]))
            if not last:
                raise ArtifactPreparationError("authoritative metadata lacks the end of a multi-episode file")
            ending_title = last.get("name") or f"Episode {source['episode_end']}"
            title = f"{title} / {ending_title}"
        ids = {"imdb": str(first.get("id") or "")}
        if isinstance(first.get("tvdb_id"), int):
            ids["tvdb"] = str(first["tvdb_id"])
        output.append({
            "source": source["source"], "season": source["season"],
            "episode": source["episode"], "episode_end": source.get("episode_end"),
            "title": title, "plot": str(first.get("description") or first.get("overview") or ""),
            "aired": date_only(first.get("released") or first.get("firstAired")),
            "rating": first.get("rating"), "uniqueids": ids,
            "default_uniqueid": "imdb",
        })
    return output


def prepare_series_artwork(
    root: Path, imdb_id: str, sources: list[dict],
) -> Path:
    poster_data = cinemeta.artwork(imdb_id, "poster")
    fanart_data = cinemeta.artwork(imdb_id, "background")
    poster = atomic_bytes(root / image_name(poster_data, "poster"), poster_data)
    fanart = atomic_bytes(root / image_name(fanart_data, "fanart"), fanart_data)
    try:
        logo_data = cinemeta.artwork(imdb_id, "logo")
        if logo_data.startswith(b"\x89PNG\r\n\x1a\n"):
            atomic_bytes(root / "clearlogo.png", logo_data)
    except cinemeta.CinemetaError:
        pass
    season_posters = {}
    for season in sorted({int(item["season"]) for item in sources}):
        target = root / f"season{season:02d}-poster{poster.suffix.lower()}"
        shutil.copy2(poster, target)
        remove_appledouble_sibling(target)
        season_posters[str(season)] = target.name
    return atomic_json(root / "artwork.json", {
        "poster": poster.name, "fanart": fanart.name,
        "season_posters": season_posters,
    })


def opensubtitles_bytes(
    imdb_id: str, language: str, *, kind: str,
    season: int | None, episode: int | None, duration: float | None,
) -> bytes | None:
    if not os.environ.get("OPENSUBTITLES_API_KEY", "").strip():
        return None
    try:
        key = api_key()
        params = [
            ("languages", language),
            ("type", "movie" if kind == "movie" else "episode"),
            ("imdb_id", imdb_id.removeprefix("tt")),
        ]
        if season is not None:
            params.append(("season_number", str(season)))
        if episode is not None:
            params.append(("episode_number", str(episode)))
        result = api_json("/subtitles", key, params=params)
        for item in result.get("data", [])[:10]:
            attributes = item.get("attributes") if isinstance(item, dict) else None
            if not isinstance(attributes, dict) or attributes.get("language") != language:
                continue
            for file_info in (attributes.get("files") or [])[:3]:
                file_id = file_info.get("file_id") if isinstance(file_info, dict) else None
                if not isinstance(file_id, int):
                    continue
                link = api_json("/download", key, payload={"file_id": file_id}).get("link")
                if not isinstance(link, str):
                    continue
                data = fetch_download(link)
                validation = validate_bytes(data, language, duration)
                if validation["valid"] and (duration is None or validation["counts_as_full_coverage"]):
                    return data
    except (OSError, ValueError, SubtitleApiError):
        return None
    return None


def stremio_bytes(
    imdb_id: str, language: str, *, kind: str,
    season: int | None, episode: int | None, duration: float | None,
) -> bytes:
    identifier = imdb_id if kind == "movie" else f"{imdb_id}:{season}:{episode}"
    result = service_json(kind, identifier)
    wanted = LANGUAGE_CODES[language]
    attempted = 0
    for item in result.get("subtitles", []):
        if not isinstance(item, dict) or item.get("lang") != wanted:
            continue
        url = item.get("url")
        if not isinstance(url, str):
            continue
        attempted += 1
        if attempted > 10:
            break
        try:
            data = fetch_srt(checked_download_url(url))
            validation = validate_bytes(data, language, duration)
        except (OSError, ValueError, StremioSubtitleError):
            continue
        if validation["valid"] and (duration is None or validation["counts_as_full_coverage"]):
            return data
    raise ArtifactPreparationError(f"no valid full {language} subtitle was found")


def subtitle_bytes(
    imdb_id: str, language: str, *, kind: str = "movie",
    season: int | None = None, episode: int | None = None,
    duration: float | None = None,
) -> bytes:
    preferred = opensubtitles_bytes(
        imdb_id, language, kind=kind, season=season, episode=episode, duration=duration,
    )
    return preferred if preferred is not None else stremio_bytes(
        imdb_id, language, kind=kind, season=season, episode=episode, duration=duration,
    )


def active_movie_languages(job_path: Path, stop: threading.Event | None) -> set[str]:
    while True:
        if stop and stop.is_set():
            raise ArtifactPreparationError("artifact preparation was cancelled")
        _, job = load_job(job_path)
        if job.get("state") in {"downloaded", "finalizing", "verified"}:
            break
        if job.get("state") in {"failed", "imported"}:
            raise ArtifactPreparationError("movie subtitle coverage cannot be checked in this state")
        time.sleep(0.5)
    raw_hash = (job.get("controller") or {}).get("active_hash") or (job.get("artifacts") or {}).get("torrent_hash")
    if not raw_hash:
        raise ArtifactPreparationError("completed movie job has no transfer identity")
    info_hash = normalize_hash(str(raw_hash))
    client = connected_client(wait_seconds=20)
    info, files = validate_torrent(client, info_hash, series=False)
    feature = files.get("main_feature") or {}
    source = str(feature.get("name") or "")
    media = checked_movies_nerd_transfer(info, info_hash) / source
    if not media.is_file():
        raise ArtifactPreparationError("completed movie file is unavailable for subtitle inspection")
    return {item["language"] for item in embedded_languages(media.resolve(strict=True))}


def movie_subtitle(
    job_path: Path, root: Path, imdb_id: str, language: str,
    duration: float | None, stop: threading.Event | None,
) -> Path | str:
    try:
        data = subtitle_bytes(imdb_id, language, duration=duration)
    except ArtifactPreparationError:
        code = "eng" if language == "en" else "fre"
        if code in active_movie_languages(job_path, stop):
            return f"verified embedded {language} subtitle coverage"
        raise
    return atomic_bytes(root / f"subtitle.{language}.srt", data)


def series_subtitle(root: Path, imdb_id: str, language: str, sources: list[dict]) -> Path:
    entries = []
    code = "eng" if language == "en" else "fre"
    for index, source in enumerate(sources, start=1):
        if code in set(source.get("languages") or []):
            continue
        output = root / "subtitles" / language / f"{index:04d}.srt"
        atomic_bytes(output, subtitle_bytes(
            imdb_id, language, kind="series", season=source["season"],
            episode=source["episode"], duration=source.get("duration"),
        ))
        entries.append({"source": source["source"], "path": str(output.relative_to(root))})
    return atomic_json(root / f"subtitle-{language}.json", {"subtitles": entries})


def run_task(job_path: Path, name: str, work) -> dict:
    _, job = load_job(job_path)
    current = task_state(job)[name]
    if current.get("status") == "complete":
        return {"task": name, "status": "complete", "cached": True}
    mark(job_path, name, "running")
    try:
        value = work()
        if isinstance(value, Path):
            mark(job_path, name, "complete", artifact=value)
        else:
            mark(job_path, name, "complete", note=str(value or "prepared automatically"))
        return {"task": name, "status": "complete", "cached": False}
    except Exception as exc:
        try:
            mark(job_path, name, "failed", note=str(exc))
        except Exception:
            pass
        return {"task": name, "status": "failed", "error": str(exc)}


def prepare(job_path: Path, stop: threading.Event | None = None) -> dict:
    checked, job = load_job(job_path)
    if all(item.get("status") == "complete" for item in task_state(job).values()):
        return plan(checked)
    start_all(checked)
    _, job = load_job(checked)
    identity = job["identity"]
    imdb_id = str((identity.get("ids") or {}).get("imdb") or "").lower()
    if not cinemeta.IMDB_RE.fullmatch(imdb_id):
        raise ArtifactPreparationError("artifact preparation requires an authoritative IMDb ID")
    meta = cinemeta.metadata(str(job["kind"]), imdb_id)
    meta_year = cinemeta.release_year(meta.get("year") or meta.get("releaseInfo"))
    if meta_year != int(identity["year"]):
        raise ArtifactPreparationError("authoritative metadata year does not match the job")
    root = artifact_root(job)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    jobs = {
        "destination": lambda: "canonical destination will be resolved during import",
    }
    if job["kind"] == "movie":
        duration = runtime_minutes(meta.get("runtime"))
        jobs.update({
            "metadata": lambda: atomic_json(root / "metadata.json", movie_metadata(job, meta, imdb_id)),
            "artwork": lambda: prepare_movie_artwork(root, imdb_id),
            "subtitle-en": lambda: movie_subtitle(
                checked, root, imdb_id, "en", duration * 60 if duration else None, stop,
            ),
            "subtitle-fr": lambda: movie_subtitle(
                checked, root, imdb_id, "fr", duration * 60 if duration else None, stop,
            ),
            "film-links": lambda: "stable Letterboxd IMDb link prepared; optional links remain non-blocking",
            "recommendations": lambda: "recommendations remain optional and do not block import",
        })
    else:
        with ThreadPoolExecutor(max_workers=1) as source_pool:
            source_future = source_pool.submit(active_episode_sources, checked, stop)
            jobs.update({
                "metadata": lambda: atomic_json(root / "metadata.json", {
                    "show": show_metadata(job, meta, imdb_id),
                    "episodes": series_episodes(meta, source_future.result()),
                }),
                "artwork": lambda: prepare_series_artwork(root, imdb_id, source_future.result()),
                "subtitle-en": lambda: series_subtitle(root, imdb_id, "en", source_future.result()),
                "subtitle-fr": lambda: series_subtitle(root, imdb_id, "fr", source_future.result()),
            })
            results = execute_tasks(checked, jobs)
        return checked_results(checked, results)
    return checked_results(checked, execute_tasks(checked, jobs))


def execute_tasks(job_path: Path, jobs: dict) -> list[dict]:
    results = []
    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        futures = {pool.submit(run_task, job_path, name, work): name for name, work in jobs.items()}
        for future in as_completed(futures):
            results.append(future.result())
    return results


def checked_results(job_path: Path, results: list[dict]) -> dict:
    failed = [item for item in results if item.get("status") == "failed"]
    if failed:
        detail = "; ".join(f"{item['task']}: {item.get('error', 'failed')}" for item in failed)
        raise ArtifactPreparationError(detail)
    current = plan(job_path)
    if not current.get("ready"):
        raise ArtifactPreparationError("artifact preparation ended before every required task completed")
    return {**current, "automated": True, "results": sorted(results, key=lambda item: item["task"])}


def prepare_when_started(
    job_path: Path, stop: threading.Event | None = None,
) -> dict:
    while True:
        if stop and stop.is_set():
            raise ArtifactPreparationError("artifact preparation was cancelled")
        checked, job = load_job(job_path)
        ready_state = job.get("state") in {"downloading", "stalled", "downloaded", "finalizing", "verified"}
        queue_exists = bool(job.get("enrichment_tasks"))
        if ready_state and (queue_exists or job.get("state") != "downloading"):
            try:
                return prepare(checked, stop)
            except ArtifactPreparationError:
                raise
            except Exception as exc:
                raise ArtifactPreparationError(str(exc)) from exc
        if job.get("state") in {"failed", "imported"}:
            raise ArtifactPreparationError("artifacts cannot be prepared in this job state")
        time.sleep(0.25)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    if not args.commit:
        parser.error("artifact preparation requires --commit for staged writes")
    try:
        print(json.dumps(prepare_when_started(args.job), ensure_ascii=False, indent=2))
        return 0
    except (
        ArtifactPreparationError, cinemeta.CinemetaError, ManifestError,
        QbtError, OSError, ValueError,
    ) as exc:
        print(json.dumps({"error": str(exc), "resumable": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
