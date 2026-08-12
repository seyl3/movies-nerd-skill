#!/usr/bin/env python3
"""Verify, organize, and completely clean one downloaded movie job."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from _common import clean_appledouble_tree, root_for_kind, stage_for_kind
from check_subtitles import embedded_languages
from finalization_queue import artifact_root, task_state
from finish_staging import clean_completed_job, verify_final_destination
from job_manifest import ManifestError, load_job, read_json, transition_job, update_job
from media_probe import ffprobe_data
from plan_library import movie_plan
from qbittorrent_api import (
    QbtError, checked_movies_nerd_transfer, connected_client,
    normalize_hash, remove_movies_nerd_torrent, torrent_info,
)
from remux_mkv import LANG_NORMALIZE, remux
from select_payload import extract_selected, scan_payload
from validate_subtitle import validate_bytes
from write_nfo import render

MAX_ARTWORK_BYTES = 50 * 1024 * 1024


class FinalizeError(ValueError):
    pass


def required_artifact(job: dict, name: str) -> Path:
    item = task_state(job).get(name) or {}
    raw = item.get("artifact")
    if item.get("status") != "complete" or not raw:
        raise FinalizeError(f"prepared {name} is not complete")
    path = Path(str(raw))
    if path.is_symlink() or not path.is_file():
        raise FinalizeError(f"prepared {name} is not a regular file")
    resolved = path.resolve(strict=True)
    root = artifact_root(job).resolve(strict=False)
    if root not in resolved.parents:
        raise FinalizeError(f"prepared {name} is outside this job")
    return resolved


def optional_artifact(job: dict, name: str) -> Path | None:
    item = task_state(job).get(name) or {}
    if item.get("status") != "complete" or not item.get("artifact"):
        return None
    return required_artifact(job, name)


def checked_metadata(job: dict) -> dict:
    value = read_json(required_artifact(job, "metadata"), 1024 * 1024)
    identity = job["identity"]
    if str(value.get("title") or "").strip().casefold() != str(identity["title"]).casefold():
        raise FinalizeError("metadata title does not match the job")
    if int(value.get("year") or 0) != int(identity["year"]):
        raise FinalizeError("metadata year does not match the job")
    directors = value.get("directors")
    if not isinstance(directors, list) or not directors or not str(directors[0]).strip():
        raise FinalizeError("metadata needs at least one director")
    job_imdb = str(((identity.get("ids") or {}).get("imdb") or "")).lower()
    metadata_imdb = str(((value.get("uniqueids") or {}).get("imdb") or "")).lower()
    if job_imdb and metadata_imdb != job_imdb:
        raise FinalizeError("metadata IMDb ID does not match the job")
    if job_imdb:
        value.setdefault("uniqueids", {})["imdb"] = job_imdb
        value.setdefault("default_uniqueid", "imdb")
    return value


def image_kind(path: Path) -> str:
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_ARTWORK_BYTES:
        raise FinalizeError("artwork is empty or too large")
    with path.open("rb") as handle:
        head = handle.read(32)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(head) < 24 or head[12:16] != b"IHDR":
            raise FinalizeError("PNG artwork has no valid image header")
        width, height = struct.unpack(">II", head[16:24])
        if not 1 <= width <= 20_000 or not 1 <= height <= 20_000:
            raise FinalizeError("PNG artwork dimensions are invalid")
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    raise FinalizeError("artwork is not a supported image")


def install_poster(source: Path, target: Path) -> None:
    kind = image_kind(source)
    if kind == "png":
        shutil.copy2(source, target)
        return
    temporary = target.with_name(".poster.converting.png")
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-frames:v", "1", str(temporary)],
        check=False, capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise FinalizeError("poster conversion failed")
    if image_kind(temporary) != "png":
        temporary.unlink(missing_ok=True)
        raise FinalizeError("poster conversion did not produce PNG")
    os.replace(temporary, target)


def resolution_tag(probe: dict) -> str:
    height = int((probe.get("summary") or {}).get("height") or 0)
    if height >= 1800:
        return "2160p"
    if height >= 900:
        return "1080p"
    if height >= 650:
        return "720p"
    if height >= 540:
        return "576p"
    return "480p"


def stream_maps(metadata: dict, info: dict) -> tuple[dict[int, str], dict[int, str]]:
    configured = metadata.get("stream_languages") or {}
    output = []
    for kind in ("audio", "subtitle"):
        raw = configured.get(kind) or {}
        if not isinstance(raw, dict):
            raise FinalizeError("stream_languages entries must be objects")
        mapping = {}
        for index, language in raw.items():
            try:
                parsed_index = int(index)
            except (TypeError, ValueError) as exc:
                raise FinalizeError("stream language index is invalid") from exc
            normalized = LANG_NORMALIZE.get(str(language).lower())
            if not normalized:
                raise FinalizeError("stream language is unsupported")
            mapping[parsed_index] = normalized
        output.append(mapping)
    original = LANG_NORMALIZE.get(str(metadata.get("original_language") or "").lower())
    names = {
        "english": "eng", "french": "fre", "français": "fre",
        "italian": "ita", "spanish": "spa", "german": "ger",
        "japanese": "jpn", "korean": "kor", "arabic": "ara",
        "portuguese": "por",
    }
    for stream in info.get("streams") or []:
        kind = stream.get("codec_type")
        mapping = output[0] if kind == "audio" else output[1] if kind == "subtitle" else None
        if mapping is None:
            continue
        index = int(stream["index"])
        if index in mapping:
            continue
        raw = str((stream.get("tags") or {}).get("language") or "und").lower()
        if LANG_NORMALIZE.get(raw):
            continue
        label = " ".join(str(value or "") for value in (
            (stream.get("tags") or {}).get("title"),
            (stream.get("tags") or {}).get("handler_name"),
        )).lower()
        inferred = next((code for name, code in names.items() if name in label), None)
        if inferred or (kind == "audio" and original):
            mapping[index] = inferred or original
    return output[0], output[1]


def checked_url(value: object, host: str) -> str | None:
    if not value:
        return None
    parsed = urlsplit(str(value))
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None or parsed.password is not None
        or parsed.port not in {None, 443}
        or not (hostname == host or hostname.endswith("." + host))
    ):
        raise FinalizeError(f"film link must use {host}")
    return str(value)


def link_summary(metadata: dict) -> dict:
    recommendations = []
    for item in metadata.get("recommendations") or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:200]
        if not title:
            continue
        recommendations.append({
            "title": title,
            "letterboxd": checked_url(item.get("letterboxd_url"), "letterboxd.com"),
            "senscritique": checked_url(item.get("senscritique_url"), "senscritique.com"),
        })
    return {
        "letterboxd": checked_url(metadata.get("letterboxd_url"), "letterboxd.com"),
        "senscritique": checked_url(metadata.get("senscritique_url"), "senscritique.com"),
        "recommendations": recommendations[:2],
    }


def checked_destination(path: Path) -> Path:
    if not path.is_absolute():
        raise FinalizeError("final movie destination must be absolute")
    resolved = path.resolve(strict=False)
    library = root_for_kind("movie").resolve(strict=False)
    stage = stage_for_kind("movie").resolve(strict=False)
    if resolved == library or library not in resolved.parents or stage in resolved.parents:
        raise FinalizeError("final movie destination is outside the movie library")
    return resolved


def verify_movie_entry(destination: Path) -> dict:
    report = verify_final_destination(destination)
    videos = [
        path for path in destination.iterdir()
        if path.is_file() and not path.is_symlink()
        and path.suffix.lower() in {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".webm", ".ts", ".m2ts"}
    ]
    if len(videos) != 1:
        raise FinalizeError("final movie entry must contain exactly one main video")
    for nfo in destination.glob("*.nfo"):
        if nfo.is_symlink() or not nfo.is_file():
            raise FinalizeError("final NFO is unsafe")
        try:
            ET.fromstring(nfo.read_bytes())
        except ET.ParseError as exc:
            raise FinalizeError("final NFO is malformed") from exc
    posters = [
        path for path in destination.glob("*.png")
        if path.name != "clearlogo.png" and path.is_file() and not path.is_symlink()
    ]
    if not posters or image_kind(posters[0]) != "png":
        raise FinalizeError("final poster is missing or invalid")
    languages = {item["language"] for item in embedded_languages(videos[0])}
    for sidecar in destination.glob("*.srt"):
        name = sidecar.name.casefold()
        if re.search(r"(?:^|[._ -])en(?:[._ -]|$)", name):
            languages.add("eng")
        if re.search(r"(?:^|[._ -])fr(?:[._ -]|$)", name):
            languages.add("fre")
    missing = {"eng", "fre"} - languages
    if missing:
        raise FinalizeError("final movie entry lacks complete English/French subtitle coverage")
    return {**report, "subtitle_languages": sorted(languages)}


def active_transfer(client, job: dict) -> tuple[str, Path]:
    controller = job.get("controller") or {}
    raw_hash = controller.get("active_hash") or (job.get("artifacts") or {}).get("torrent_hash")
    if not raw_hash:
        raise FinalizeError("downloaded job has no active transfer")
    info_hash = normalize_hash(str(raw_hash))
    info = torrent_info(client, info_hash)
    return info_hash, checked_movies_nerd_transfer(info, info_hash)


def remove_all_owned(client, job: dict) -> list[str]:
    controller = job.get("controller") or {}
    hashes = {
        normalize_hash(str(value)) for value in (
            controller.get("active_hash"), controller.get("standby_hash"),
            (job.get("artifacts") or {}).get("torrent_hash"),
            *(controller.get("tried_hashes") or []),
        ) if value
    }
    removed = []
    for info_hash in sorted(hashes):
        try:
            torrent_info(client, info_hash)
        except QbtError as exc:
            if "not present" in str(exc):
                continue
            raise
        result = remove_movies_nerd_torrent(client, info_hash)
        if not result.get("staged_payload_removed"):
            raise FinalizeError("download cleanup did not remove its staging directory")
        removed.append(info_hash)
    return removed


def finalize(job_path: Path) -> dict:
    checked, job = load_job(job_path)
    if job.get("kind") != "movie":
        raise FinalizeError("movie finalization requires a movie job")
    cached_handoff = ((job.get("cache") or {}).get("handoff") or {})
    if job.get("state") == "imported":
        metadata = None
        try:
            metadata = checked_metadata(job)
        except (FinalizeError, ManifestError, OSError):
            if not cached_handoff:
                raise
        client = connected_client(wait_seconds=20)
        destination = Path(str(job.get("destination") or ""))
        removed = remove_all_owned(client, job)
        cleanup = clean_completed_job(destination, [], checked)
        summary = cached_handoff or link_summary(metadata or {})
        return {
            "ready": True, "destination": str(destination),
            "removed_transfers": removed, "cleanup": cleanup, **summary,
        }
    metadata = checked_metadata(job)
    poster = required_artifact(job, "artwork")
    image_kind(poster)
    handoff = link_summary(metadata)
    update_job(checked, {"cache": {"handoff": handoff}})
    _, job = load_job(checked)
    client = connected_client(wait_seconds=20)
    if job.get("state") not in {"downloaded", "finalizing", "verified"}:
        raise FinalizeError("job must finish downloading before finalization")

    if job.get("state") == "downloaded":
        transition_job(checked, "finalizing")
        _, job = load_job(checked)
    transaction = (job.get("artifacts") or {}).get("finalization_transaction")
    if isinstance(transaction, dict) and transaction.get("phase") == "building":
        destination = checked_destination(Path(str(transaction.get("destination") or "")))
        importing = Path(str(transaction.get("importing") or ""))
        expected_importing = destination.parent / f".{destination.name}.{job['job_id']}.importing"
        source = Path(str(transaction.get("source") or ""))
        target_name = str(transaction.get("target_name") or "")
        stage = stage_for_kind("movie").resolve(strict=False)
        try:
            source_resolved = source.resolve(strict=False)
        except OSError as exc:
            raise FinalizeError("saved finalization source is invalid") from exc
        if (
            importing != expected_importing
            or Path(target_name).name != target_name or not target_name
            or stage not in source_resolved.parents
        ):
            raise FinalizeError("saved finalization transaction is inconsistent")
        installed = False
        if destination.exists():
            verify_movie_entry(destination)
            installed = True
        elif importing.exists():
            try:
                verify_movie_entry(importing)
            except (FinalizeError, ValueError):
                staged_video = importing / target_name
                if not source.exists():
                    if staged_video.is_symlink() or not staged_video.is_file():
                        raise FinalizeError("interrupted finalization cannot restore its staged video")
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged_video, source)
                shutil.rmtree(importing)
                work = stage / "finalize" / str(job["job_id"])
                if work.exists() and not work.is_symlink():
                    shutil.rmtree(work)
                update_job(checked, {"artifacts": {"finalization_transaction": None}})
                _, job = load_job(checked)
                transaction = None
            else:
                os.replace(importing, destination)
                installed = True
        elif source.exists():
            update_job(checked, {"artifacts": {"finalization_transaction": None}})
            _, job = load_job(checked)
            transaction = None
        else:
            raise FinalizeError("interrupted finalization lost both its source and prepared entry")
        if installed:
            update_job(checked, {
                "artifacts": {
                    "finalization_transaction": {
                        **transaction, "phase": "installed",
                    },
                },
            })
            _, job = load_job(checked)
            transaction = (job.get("artifacts") or {}).get("finalization_transaction")
    if isinstance(transaction, dict) and transaction.get("phase") in {"prepared", "installed"}:
        destination = checked_destination(Path(str(transaction.get("destination") or "")))
        importing = Path(str(transaction.get("importing") or ""))
        expected_importing = destination.parent / f".{destination.name}.{job['job_id']}.importing"
        if str(job.get("destination") or "") != str(destination) or importing != expected_importing:
            raise FinalizeError("saved finalization transaction is inconsistent")
        if destination.exists() and importing.exists():
            raise FinalizeError("both prepared and installed movie folders exist")
        if importing.exists():
            verify_movie_entry(importing)
            os.replace(importing, destination)
        if not destination.exists():
            raise FinalizeError("prepared movie folder disappeared before installation")
        verify_movie_entry(destination)
        update_job(checked, {
            "artifacts": {
                "finalization_transaction": {
                    "phase": "installed",
                    "destination": str(destination),
                    "importing": str(importing),
                },
            },
        })
        _, job = load_job(checked)
        if job.get("state") != "verified":
            transition_job(checked, "verified")
        transition_job(checked, "imported")
        _, job = load_job(checked)
        removed = remove_all_owned(client, job)
        stage = stage_for_kind("movie")
        staged = [
            path for path in (
                stage / "finalize" / str(job["job_id"]), artifact_root(job),
                stage / "clean" / str(job["job_id"]),
            ) if path.exists()
        ]
        cleanup = clean_completed_job(destination, staged, checked)
        return {
            "ready": True, "destination": str(destination),
            "removed_transfers": removed, "cleanup": cleanup, **handoff,
        }
    info_hash, transfer = active_transfer(client, job)
    client.request("torrents/stop", {"hashes": info_hash})
    clean_appledouble_tree(transfer)
    report = scan_payload(transfer, series=False)

    stage = stage_for_kind("movie")
    clean_root = stage / "clean" / str(job["job_id"])
    if not report.get("safe_to_extract_selected") and clean_root.is_dir() and not clean_root.is_symlink():
        report = scan_payload(clean_root, series=False)
        media_root = clean_root
    else:
        media_root = transfer
    if not report.get("safe_to_extract_selected"):
        raise FinalizeError("downloaded video did not pass final media verification")
    work = stage / "finalize" / str(job["job_id"])
    if work.exists() and (work.is_symlink() or not work.is_dir()):
        raise FinalizeError("finalization workspace is unsafe")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    if report.get("cleanup_required"):
        if clean_root.exists():
            cleaned = scan_payload(clean_root, series=False)
            if not cleaned.get("safe_to_extract_selected"):
                raise FinalizeError("existing clean media workspace is incomplete")
            media_root = clean_root
            report = cleaned
        else:
            extracted = extract_selected(transfer, clean_root, report, series=False)
            media_root = Path(extracted["clean_payload"])
            report = extracted["verification"]
    selected = report["selected"]
    if len(selected) != 1:
        raise FinalizeError("movie finalization requires exactly one verified main video")
    source = (media_root / selected[0]["path"]).resolve(strict=True)
    probe = selected[0]["probe"]
    duration = float(selected[0]["duration_seconds"])
    plan = movie_plan(SimpleNamespace(
        title=job["identity"]["title"], year=int(job["identity"]["year"]),
        director=str(metadata["directors"][0]), resolution=resolution_tag(probe),
    ))
    destination = checked_destination(Path(plan["folder"]))

    if destination.exists():
        if job.get("state") in {"verified", "imported"}:
            verify_final_destination(destination)
        else:
            raise FinalizeError("final movie folder already exists; refusing to overwrite it")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        importing = destination.parent / f".{destination.name}.{job['job_id']}.importing"
        if importing.exists() or importing.is_symlink():
            raise FinalizeError("an earlier finalization workspace needs inspection")
        target_name = Path(plan["video"]).name
        update_job(checked, {
            "destination": str(destination),
            "artifacts": {
                "finalization_transaction": {
                    "phase": "building",
                    "destination": str(destination),
                    "importing": str(importing),
                    "source": str(source),
                    "target_name": target_name,
                },
            },
        })
        importing.mkdir(mode=0o700)
        moved_source = False
        try:
            target_video = importing / target_name
            info = ffprobe_data(probe)
            if source.suffix.lower() == ".mkv":
                os.replace(source, target_video)
                moved_source = True
            else:
                audio_map, subtitle_map = stream_maps(metadata, info)
                staged_mkv = work / target_video.name
                remux(source, staged_mkv, info, audio_map, subtitle_map, allow_unknown=True)
                os.replace(staged_mkv, target_video)
            nfo = importing / Path(plan["nfo"]).name
            nfo.write_bytes(render("movie", metadata))
            install_poster(poster, importing / Path(plan["poster"]).name)
            optional_fanart = artifact_root(job) / "fanart.jpg"
            if optional_fanart.is_file() and not optional_fanart.is_symlink():
                image_kind(optional_fanart)
                shutil.copy2(optional_fanart, importing / "fanart.jpg")
            optional_logo = artifact_root(job) / "clearlogo.png"
            if optional_logo.is_file() and not optional_logo.is_symlink():
                if image_kind(optional_logo) != "png":
                    raise FinalizeError("clear logo must be a PNG image")
                shutil.copy2(optional_logo, importing / "clearlogo.png")

            present = {item["language"] for item in embedded_languages(target_video)}
            for code, task, output_key in (
                ("eng", "subtitle-en", "english_subtitle"),
                ("fre", "subtitle-fr", "french_subtitle"),
            ):
                if code in present:
                    continue
                subtitle = required_artifact(job, task)
                validation = validate_bytes(
                    subtitle.read_bytes(), "en" if code == "eng" else "fr", duration,
                )
                if not validation.get("valid") or not validation.get("counts_as_full_coverage"):
                    raise FinalizeError(f"prepared {task} is not a valid full subtitle")
                shutil.copy2(subtitle, importing / Path(plan[output_key]).name)
            clean_appledouble_tree(importing)
            verify_movie_entry(importing)
            update_job(checked, {
                "artifacts": {
                    "finalization_transaction": {
                        "phase": "prepared",
                        "destination": str(destination),
                        "importing": str(importing),
                        "source": str(source),
                        "target_name": target_name,
                    },
                },
            })
            os.replace(importing, destination)
            update_job(checked, {
                "artifacts": {
                    "finalization_transaction": {
                        "phase": "installed",
                        "destination": str(destination),
                        "importing": str(importing),
                        "source": str(source),
                        "target_name": target_name,
                    },
                },
            })
        except Exception:
            if moved_source:
                staged_video = importing / Path(plan["video"]).name
                if staged_video.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged_video, source)
            shutil.rmtree(importing, ignore_errors=True)
            update_job(checked, {"artifacts": {"finalization_transaction": None}})
            raise

    if job.get("state") in {"downloaded", "finalizing"}:
        transition_job(checked, "verified")
    _, job = load_job(checked)
    transition_job(checked, "imported")
    _, job = load_job(checked)
    removed = remove_all_owned(client, job)
    staged = [path for path in (work, artifact_root(job), stage / "clean" / str(job["job_id"])) if path.exists()]
    cleanup = clean_completed_job(destination, staged, checked)
    return {
        "ready": True,
        "destination": str(destination),
        "quality": resolution_tag(probe),
        "removed_transfers": removed,
        "cleanup": cleanup,
        **handoff,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    if not args.commit:
        parser.error("finalization requires --commit for the requested download")
    try:
        print(json.dumps(finalize(args.job), ensure_ascii=False, indent=2))
        return 0
    except (FinalizeError, ManifestError, QbtError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc), "resumable": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
