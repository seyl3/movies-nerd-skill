from __future__ import annotations

import hashlib
import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import ssl
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common
import acquire
import batch_jobs
import cinemeta
import finalization_queue
import finalize_job
import finalize_series
import finish_staging
import job_manifest
import media_probe
import monitor_download
import provider_health
import prepare_job
import prepare_artifacts
import run_job
import qbittorrent_api as qbt
import race_candidates
import search_releases
import torrent_metadata
import title_policy
import wikidata_titles

from helpers import bencode, candidate, roots, torrent_fixture


class SeriesV2Tests(unittest.TestCase):
    def test_foreground_runner_dispatches_series_finalizer(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("series", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "downloaded",
                    "enrichment_tasks": {
                        name: {"status": "complete"}
                        for name in finalization_queue.SERIES_TASKS
                    },
                })
                output = io.StringIO()
                with (
                    patch.object(acquire, "run", return_value={"downloaded": True}),
                    patch.object(run_job, "finalize_series", return_value={"ready": True}) as finish,
                    redirect_stdout(output),
                ):
                    result = run_job.run(path, artifact_wait_seconds=0)
            self.assertTrue(result["ready"])
            finish.assert_called_once()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_downloaded_series_finalizes_special_multi_episode_and_cleans(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job(
                    "series", "Example Show", 2024,
                    {"identity": {"ids": {"imdb": "tt1234567"}}},
                )
                job_manifest.update_job(path, {"state": "downloading"})
                _, job = job_manifest.load_job(path)
                finalization_queue.start_all(path)
                root = finalization_queue.artifact_root(job)
                metadata = root / "metadata.json"
                metadata.write_text(json.dumps({
                    "show": {
                        "title": "Example Show", "year": 2024,
                        "plot": "Test show.", "uniqueids": {"imdb": "tt1234567"},
                        "default_uniqueid": "imdb",
                    },
                    "episodes": [{
                        "source": "Example.Show.S00E01E02.mkv",
                        "season": 0, "episode": 1, "episode_end": 2,
                        "title": "The Special", "plot": "A combined special.",
                    }],
                }), encoding="utf-8")
                poster = root / "poster.jpg"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=size=32x32:duration=0.1", "-frames:v", "1", str(poster),
                ], check=True)
                shutil.copy2(poster, root / "fanart.jpg")
                shutil.copy2(poster, root / "season00-poster.jpg")
                subtitle_text = "\n\n".join([
                    "1\n00:00:00,000 --> 00:00:00,300\nOne",
                    "2\n00:00:00,350 --> 00:00:00,650\nTwo",
                    "3\n00:00:00,700 --> 00:00:01,000\nThree",
                    "4\n00:00:01,050 --> 00:00:01,350\nFour",
                    "5\n00:00:01,400 --> 00:00:01,900\nFive",
                ]) + "\n"
                en = root / "special.en.srt"
                fr = root / "special.fr.srt"
                en.write_text(subtitle_text, encoding="utf-8")
                fr.write_text(subtitle_text, encoding="utf-8")
                for task, artifact in (
                    ("metadata", metadata), ("artwork", poster),
                    ("subtitle-en", en), ("subtitle-fr", fr),
                ):
                    finalization_queue.mark(path, task, "complete", artifact=artifact)
                finalization_queue.mark(path, "destination", "complete", note="prepared")

                info_hash = "d" * 40
                transfer = _common.stage_for_kind("series") / "transfers" / info_hash
                transfer.mkdir(parents=True)
                media = transfer / "Example.Show.S00E01E02.mkv"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=size=320x180:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
                    "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(media),
                ], check=True)
                probe = media_probe.probe_media(media)
                report = {
                    "safe_to_extract_selected": True,
                    "cleanup_required": False,
                    "selected": [{
                        "path": media.name, "probe": probe,
                        "duration_seconds": probe["summary"]["duration_seconds"],
                    }],
                }
                job_manifest.update_job(path, {
                    "state": "downloaded",
                    "controller": {"active_hash": info_hash, "tried_hashes": [info_hash]},
                    "artifacts": {"torrent_hash": info_hash},
                })

                class Client:
                    present = True

                    def json(self, endpoint):
                        if endpoint.startswith("torrents/info"):
                            return [{
                                "hash": info_hash, "save_path": str(transfer),
                                "tags": "movies-nerd,series",
                            }] if self.present else []
                        raise AssertionError(endpoint)

                    def request(self, endpoint, _fields=None):
                        if endpoint == "torrents/delete":
                            self.present = False
                        return b"Ok."

                client = Client()
                with (
                    patch.object(finalize_series, "connected_client", return_value=client),
                    patch.object(finalize_series, "scan_payload", return_value=report),
                    patch.object(finish_staging, "connected_client", return_value=client),
                ):
                    result = finalize_series.finalize(path)
                destination = Path(result["destination"])
                episode_files = {item.name for item in (destination / "Season 00").iterdir()}
            self.assertTrue(result["ready"])
            self.assertTrue(result["cleanup"]["clean"])
            self.assertEqual(result["episodes"], 1)
            self.assertTrue((destination / "tvshow.nfo").is_file())
            self.assertTrue((destination / "poster.jpg").is_file())
            self.assertTrue((destination / "fanart.jpg").is_file())
            self.assertTrue((destination / "season00-poster.jpg").is_file())
            self.assertIn("Example Show (2024) - S00E01-E02 - The Special [480p].mkv", episode_files)
            self.assertIn("Example Show (2024) - S00E01-E02 - The Special [480p].en.srt", episode_files)
            self.assertIn("Example Show (2024) - S00E01-E02 - The Special [480p].fr.srt", episode_files)
            self.assertFalse(path.exists())

    def test_series_artifacts_include_episode_manifests_automatically(self):
        tiny_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z8l8AAAAASUVORK5CYII="
        )
        subtitle = b"1\n00:00:00,000 --> 00:00:01,000\nOne\n\n2\n00:00:02,000 --> 00:00:03,000\nTwo\n\n3\n00:00:04,000 --> 00:00:05,000\nThree\n\n4\n00:00:06,000 --> 00:00:07,000\nFour\n\n5\n00:00:08,000 --> 00:00:09,000\nFive\n"
        meta = {
            "id": "tt1234567", "name": "Example Show", "year": "2024",
            "description": "Plot", "runtime": "45 min", "genre": ["Drama"],
            "videos": [{
                "id": "tt1234567:1:2", "name": "Second", "season": 1,
                "episode": 2, "released": "2024-01-02T00:00:00.000Z",
                "description": "Episode plot", "tvdb_id": 22,
            }],
        }
        sources = [{
            "source": "Example.Show.S01E02.mkv", "season": 1,
            "episode": 2, "episode_end": None, "duration": None,
        }]
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job(
                    "series", "Example Show", 2024,
                    {"identity": {"ids": {"imdb": "tt1234567"}}},
                )
                job_manifest.update_job(path, {"state": "downloading"})
                with (
                    patch.object(prepare_artifacts.cinemeta, "metadata", return_value=meta),
                    patch.object(prepare_artifacts.cinemeta, "artwork", return_value=tiny_png),
                    patch.object(prepare_artifacts, "active_episode_sources", return_value=sources),
                    patch.object(prepare_artifacts, "subtitle_bytes", return_value=subtitle),
                ):
                    result = prepare_artifacts.prepare(path)
                _, job = job_manifest.load_job(path)
                root = finalization_queue.artifact_root(job)
                metadata = json.loads((root / "metadata.json").read_text())
                english = json.loads((root / "subtitle-en.json").read_text())
                artwork = json.loads((root / "artwork.json").read_text())
            self.assertTrue(result["ready"])
            self.assertEqual(metadata["episodes"][0]["title"], "Second")
            self.assertEqual(english["subtitles"][0]["source"], "Example.Show.S01E02.mkv")
            self.assertIn("1", artwork["season_posters"])
