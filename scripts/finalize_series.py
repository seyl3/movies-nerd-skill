#!/usr/bin/env python3
"""Verify, organize, and completely clean one downloaded series job."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from _common import clean_appledouble_tree, root_for_kind, stage_for_kind
from check_subtitles import embedded_languages
from finalization_queue import artifact_root
from finalize_job import (
    FinalizeError, active_transfer, image_kind, remove_all_owned,
    required_artifact, resolution_tag, stream_maps,
)
from finish_staging import clean_completed_job, verify_final_destination
from job_manifest import ManifestError, load_job, read_json, transition_job, update_job
from media_probe import ffprobe_data
from plan_library import component, episode_plan
from qbittorrent_api import QbtError, connected_client
from remux_mkv import remux
from select_payload import extract_selected, scan_payload
from validate_subtitle import validate_bytes
from write_nfo import render

EPISODE_RE = re.compile(r"(?i)(?:^|[^A-Z0-9])S(\d{1,2})E(\d{1,3})(?:[-_. ]?E?(\d{1,3}))?")


def safe_relative(raw: object, label: str) -> str:
    value = str(raw or "").replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise FinalizeError(f"{label} has an unsafe relative path")
    return str(path)


def checked_metadata(job: dict) -> tuple[dict, list[dict]]:
    value = read_json(required_artifact(job, "metadata"), 2 * 1024 * 1024)
    show = value.get("show") if isinstance(value.get("show"), dict) else {
        key: child for key, child in value.items() if key != "episodes"
    }
    identity = job["identity"]
    if str(show.get("title") or "").strip().casefold() != str(identity["title"]).casefold():
        raise FinalizeError("series metadata title does not match the job")
    if int(show.get("year") or 0) != int(identity["year"]):
        raise FinalizeError("series metadata year does not match the job")
    job_imdb = str(((identity.get("ids") or {}).get("imdb") or "")).lower()
    metadata_imdb = str(((show.get("uniqueids") or {}).get("imdb") or "")).lower()
    if job_imdb and metadata_imdb != job_imdb:
        raise FinalizeError("series metadata IMDb ID does not match the job")
    if job_imdb:
        show.setdefault("uniqueids", {})["imdb"] = job_imdb
        show.setdefault("default_uniqueid", "imdb")
    raw_episodes = value.get("episodes")
    if not isinstance(raw_episodes, list) or not raw_episodes:
        raise FinalizeError("series metadata needs at least one episode")
    episodes = []
    identities = set()
    for raw in raw_episodes:
        if not isinstance(raw, dict):
            raise FinalizeError("episode metadata must contain objects")
        try:
            season = int(raw.get("season"))
            episode = int(raw.get("episode"))
            episode_end = int(raw["episode_end"]) if raw.get("episode_end") is not None else None
        except (TypeError, ValueError) as exc:
            raise FinalizeError("episode metadata has an invalid number") from exc
        if not 0 <= season <= 99 or not 0 <= episode <= 999:
            raise FinalizeError("episode metadata is outside supported numbering")
        if episode_end is not None and not episode < episode_end <= 999:
            raise FinalizeError("multi-episode metadata has an invalid ending")
        key = (season, episode, episode_end)
        if key in identities:
            raise FinalizeError("episode metadata contains a duplicate identity")
        identities.add(key)
        item = dict(raw)
        item.update({"season": season, "episode": episode, "episode_end": episode_end})
        item["title"] = " ".join(str(item.get("title") or f"Episode {episode}").split())
        if raw.get("source"):
            item["source"] = safe_relative(raw["source"], "episode source")
        episodes.append(item)
    return show, episodes


def episode_for_source(source: str, episodes: list[dict], selected_count: int) -> dict:
    exact = [item for item in episodes if item.get("source") == source]
    if len(exact) == 1:
        return exact[0]
    match = EPISODE_RE.search(source)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        end = int(match.group(3)) if match.group(3) else None
        numbered = [
            item for item in episodes
            if item["season"] == season and item["episode"] == episode
            and item.get("episode_end") == end
        ]
        if len(numbered) == 1:
            return numbered[0]
    if selected_count == 1 and len(episodes) == 1:
        return episodes[0]
    raise FinalizeError(f"series metadata does not uniquely identify {source}")


def artifact_member(job: dict, raw: object, label: str) -> Path:
    relative = safe_relative(raw, label)
    root = artifact_root(job).resolve(strict=False)
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise FinalizeError(f"prepared {label} is missing")
    resolved = path.resolve(strict=True)
    if root not in resolved.parents:
        raise FinalizeError(f"prepared {label} is outside this job")
    return resolved


def artwork_bundle(job: dict, seasons: set[int]) -> dict:
    artifact = required_artifact(job, "artwork")
    if artifact.suffix.lower() == ".json":
        value = read_json(artifact, 1024 * 1024)
        poster = artifact_member(job, value.get("poster"), "series poster")
        fanart = artifact_member(job, value.get("fanart"), "series fanart")
        season_raw = value.get("season_posters")
        if not isinstance(season_raw, dict):
            raise FinalizeError("series artwork needs season posters")
        season_posters = {
            season: artifact_member(
                job, season_raw.get(str(season)), f"season {season:02d} poster",
            ) for season in seasons
        }
    else:
        poster = artifact
        fanart = artifact_root(job) / "fanart.jpg"
        season_posters = {
            season: artifact_root(job) / f"season{season:02d}-poster.jpg"
            for season in seasons
        }
        for label, path in [("series fanart", fanart), *[
            (f"season {season:02d} poster", path) for season, path in season_posters.items()
        ]]:
            if path.is_symlink() or not path.is_file():
                raise FinalizeError(f"prepared {label} is missing")
    for path in [poster, fanart, *season_posters.values()]:
        image_kind(path)
    return {"poster": poster, "fanart": fanart, "season_posters": season_posters}


def install_jpeg(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.converting.jpg")
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-frames:v", "1", str(temporary)],
        check=False, capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0 or not temporary.is_file() or image_kind(temporary) != "jpeg":
        temporary.unlink(missing_ok=True)
        raise FinalizeError("series artwork conversion failed")
    os.replace(temporary, target)


def subtitle_map(job: dict, task: str, selected_count: int) -> dict[str, Path]:
    artifact = required_artifact(job, task)
    if artifact.suffix.lower() == ".srt":
        if selected_count != 1:
            raise FinalizeError(f"{task} needs a per-episode manifest for multiple files")
        return {"*": artifact}
    value = read_json(artifact, 2 * 1024 * 1024)
    raw_items = value.get("subtitles")
    if not isinstance(raw_items, list):
        raise FinalizeError(f"{task} manifest needs a subtitles list")
    output = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise FinalizeError(f"{task} manifest contains an invalid item")
        source = safe_relative(raw.get("source"), f"{task} source")
        if source in output:
            raise FinalizeError(f"{task} manifest contains a duplicate source")
        output[source] = artifact_member(job, raw.get("path"), task)
    return output


def checked_destination(title: str, year: int) -> Path:
    destination = root_for_kind("series") / f"{component(title)} ({year})"
    resolved = destination.resolve(strict=False)
    library = root_for_kind("series").resolve(strict=False)
    stage = stage_for_kind("series").resolve(strict=False)
    if library not in resolved.parents or stage in resolved.parents:
        raise FinalizeError("final series destination is outside the series library")
    return resolved


def verify_series_entry(destination: Path, videos: list[Path]) -> dict:
    report = verify_final_destination(destination)
    if not (destination / "tvshow.nfo").is_file():
        raise FinalizeError("final series entry lacks tvshow.nfo")
    for nfo in destination.rglob("*.nfo"):
        try:
            ET.fromstring(nfo.read_bytes())
        except ET.ParseError as exc:
            raise FinalizeError("final series NFO is malformed") from exc
    for relative in videos:
        video = destination / relative
        if video.is_symlink() or not video.is_file():
            raise FinalizeError("a finalized episode is missing")
        languages = {item["language"] for item in embedded_languages(video)}
        stem = video.with_suffix("")
        if stem.with_name(stem.name + ".en.srt").is_file():
            languages.add("eng")
        if stem.with_name(stem.name + ".fr.srt").is_file():
            languages.add("fre")
        if {"eng", "fre"} - languages:
            raise FinalizeError("a finalized episode lacks English/French subtitle coverage")
    return report


def merge_prepared(prepared: Path, destination: Path) -> None:
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(prepared, destination)
        return
    if destination.is_symlink() or not destination.is_dir():
        raise FinalizeError("existing series destination is unsafe")
    moved: list[tuple[Path, Path]] = []
    try:
        for source in sorted((path for path in prepared.rglob("*") if path.is_file())):
            relative = source.relative_to(prepared)
            target = destination / relative
            if target.exists():
                if relative.parts[0] == "tvshow.nfo" or relative.name in {
                    "poster.jpg", "fanart.jpg", "clearlogo.png",
                } or re.fullmatch(r"season\d{2}-poster\.jpg", relative.name):
                    continue
                raise FinalizeError(f"series import would overwrite {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            moved.append((source, target))
    except Exception:
        for source, target in reversed(moved):
            source.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not source.exists():
                os.replace(target, source)
        raise
    shutil.rmtree(prepared)


def finalize(job_path: Path) -> dict:
    checked, job = load_job(job_path)
    if job.get("kind") != "series":
        raise FinalizeError("series finalization requires a series job")
    destination = checked_destination(job["identity"]["title"], int(job["identity"]["year"]))
    client = connected_client(wait_seconds=20)
    if job.get("state") == "imported":
        removed = remove_all_owned(client, job)
        cleanup = clean_completed_job(destination, [], checked)
        return {"ready": True, "destination": str(destination), "removed_transfers": removed, "cleanup": cleanup}
    if job.get("state") not in {"downloaded", "finalizing", "verified"}:
        raise FinalizeError("series job must finish downloading before finalization")
    if job.get("state") == "downloaded":
        transition_job(checked, "finalizing")
        _, job = load_job(checked)
    show, episodes = checked_metadata(job)
    info_hash, transfer = active_transfer(client, job)
    client.request("torrents/stop", {"hashes": info_hash})
    clean_appledouble_tree(transfer)
    stage = stage_for_kind("series")
    clean_root = stage / "clean" / str(job["job_id"])
    report = scan_payload(transfer, series=True)
    media_root = transfer
    if not report.get("safe_to_extract_selected") and clean_root.is_dir() and not clean_root.is_symlink():
        report = scan_payload(clean_root, series=True)
        media_root = clean_root
    if not report.get("safe_to_extract_selected"):
        raise FinalizeError("downloaded episodes did not pass final media verification")
    if report.get("cleanup_required"):
        if clean_root.exists():
            report = scan_payload(clean_root, series=True)
            media_root = clean_root
        else:
            extracted = extract_selected(transfer, clean_root, report, series=True)
            media_root = Path(extracted["clean_payload"])
            report = extracted["verification"]
    selected = report["selected"]
    if not selected:
        raise FinalizeError("series finalization found no verified episodes")
    mapped = [(item, episode_for_source(item["path"], episodes, len(selected))) for item in selected]
    if len({(ep["season"], ep["episode"], ep.get("episode_end")) for _, ep in mapped}) != len(mapped):
        raise FinalizeError("multiple downloaded files map to the same episode")
    seasons = {episode["season"] for _, episode in mapped}
    artwork = artwork_bundle(job, seasons)
    subtitles = {
        "eng": subtitle_map(job, "subtitle-en", len(selected)),
        "fre": subtitle_map(job, "subtitle-fr", len(selected)),
    }
    work = stage / "finalize" / str(job["job_id"])
    prepared = work / destination.name
    if work.exists():
        if work.is_symlink() or not work.is_dir():
            raise FinalizeError("series finalization workspace is unsafe")
        shutil.rmtree(work)
    prepared.mkdir(mode=0o700, parents=True)
    moved_sources: list[tuple[Path, Path]] = []
    relative_videos: list[Path] = []
    try:
        (prepared / "tvshow.nfo").write_bytes(render("tvshow", show))
        install_jpeg(artwork["poster"], prepared / "poster.jpg")
        install_jpeg(artwork["fanart"], prepared / "fanart.jpg")
        for season, source in artwork["season_posters"].items():
            install_jpeg(source, prepared / f"season{season:02d}-poster.jpg")
        logo = artifact_root(job) / "clearlogo.png"
        if logo.is_file() and not logo.is_symlink() and image_kind(logo) == "png":
            shutil.copy2(logo, prepared / "clearlogo.png")
        for item, episode in mapped:
            source = (media_root / item["path"]).resolve(strict=True)
            plan = episode_plan(SimpleNamespace(
                title=show["title"], year=int(show["year"]),
                season=episode["season"], episode=episode["episode"],
                episode_end=episode.get("episode_end"), episode_title=episode["title"],
                resolution=resolution_tag(item["probe"]),
            ))
            relative = Path(plan["video"]).relative_to(destination)
            target_video = prepared / relative
            target_video.parent.mkdir(parents=True, exist_ok=True)
            info = ffprobe_data(item["probe"])
            if source.suffix.lower() == ".mkv":
                os.replace(source, target_video)
                moved_sources.append((source, target_video))
            else:
                audio_map, subtitle_stream_map = stream_maps(show, info)
                remux(source, target_video, info, audio_map, subtitle_stream_map, allow_unknown=True)
            episode_nfo = dict(episode)
            episode_nfo.update({"showtitle": show["title"]})
            (prepared / Path(plan["nfo"]).relative_to(destination)).write_bytes(
                render("episodedetails", episode_nfo)
            )
            present = {entry["language"] for entry in embedded_languages(target_video)}
            for code, language, key in (("eng", "en", "english_subtitle"), ("fre", "fr", "french_subtitle")):
                if code in present:
                    continue
                subtitle = subtitles[code].get(item["path"]) or subtitles[code].get("*")
                if subtitle is None:
                    raise FinalizeError(f"{item['path']} lacks a prepared {language} subtitle")
                validation = validate_bytes(subtitle.read_bytes(), language, float(item["duration_seconds"]))
                if not validation.get("valid") or not validation.get("counts_as_full_coverage"):
                    raise FinalizeError(f"{item['path']} has an invalid {language} subtitle")
                subtitle_target = prepared / Path(plan[key]).relative_to(destination)
                shutil.copy2(subtitle, subtitle_target)
            relative_videos.append(relative)
        clean_appledouble_tree(prepared)
        verify_series_entry(prepared, relative_videos)
        update_job(checked, {
            "destination": str(destination),
            "artifacts": {"series_transaction": {"phase": "prepared", "workspace": str(prepared)}},
        })
        merge_prepared(prepared, destination)
        verify_series_entry(destination, relative_videos)
        update_job(checked, {"artifacts": {"series_transaction": {"phase": "installed"}}})
    except Exception:
        for source, target in reversed(moved_sources):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, source)
        shutil.rmtree(work, ignore_errors=True)
        update_job(checked, {"artifacts": {"series_transaction": None}})
        raise
    if job.get("state") in {"downloaded", "finalizing"}:
        transition_job(checked, "verified")
    transition_job(checked, "imported")
    _, job = load_job(checked)
    removed = remove_all_owned(client, job)
    staged = [path for path in (work, artifact_root(job), clean_root) if path.exists()]
    cleanup = clean_completed_job(destination, staged, checked)
    return {
        "ready": True, "destination": str(destination),
        "episodes": len(relative_videos), "seasons": sorted(seasons),
        "removed_transfers": removed, "cleanup": cleanup,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    if not args.commit:
        parser.error("series finalization requires --commit for the requested download")
    try:
        print(json.dumps(finalize(args.job), ensure_ascii=False, indent=2))
        return 0
    except (FinalizeError, ManifestError, QbtError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc), "resumable": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
