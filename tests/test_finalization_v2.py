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


class FinalizationV2Tests(unittest.TestCase):
    def test_finalization_requests_real_work_instead_of_claiming_it_started(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {"state": "downloading"})
                plan = finalization_queue.start_all(path)
                self.assertTrue(Path(plan["artifact_root"]).is_dir())
                self.assertTrue(all(item["status"] == "requested" for item in plan["tasks"]))

    def test_foreground_runner_reaches_finalizer_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "downloaded",
                    "enrichment_tasks": {
                        name: {"status": "complete"}
                        for name in finalization_queue.MOVIE_TASKS
                    },
                })
                _, prepared_job = job_manifest.load_job(path)
                context_root = finalization_queue.artifact_root(prepared_job)
                context_root.mkdir(parents=True)
                (context_root / "recommendation-context.json").write_text(json.dumps({
                    "owned_count": 2,
                    "completed_director": "Director",
                    "owned_by_director": {"Director": ["Example (2024)"]},
                }), encoding="utf-8")
                output = io.StringIO()
                with (
                    patch.object(acquire, "run", return_value={"downloaded": True}),
                    patch.object(
                        prepare_artifacts, "prepare_when_started",
                        return_value={"ready": True, "automated": True},
                    ) as artifacts,
                    patch.object(run_job, "finalize", return_value={"ready": True}) as finish,
                    redirect_stdout(output),
                ):
                    result = run_job.run(path, artifact_wait_seconds=0)
                _, job = job_manifest.load_job(path)
                lock = path.parent.parent / "locks" / f"{job['job_id']}.lock"
            self.assertTrue(result["ready"])
            self.assertEqual(result["recommendation_context"]["completed_director"], "Director")
            self.assertFalse(lock.exists())
            artifacts.assert_called_once()
            finish.assert_called_once()
            events = [json.loads(line)["event"] for line in output.getvalue().splitlines()]
            self.assertEqual(events, ["finalization-started", "ready"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_downloaded_movie_finalizes_and_cleans_end_to_end(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job(
                    "movie", "Example", 2024,
                    {"identity": {"ids": {"imdb": "tt1234567"}}},
                )
                job_manifest.update_job(path, {"state": "downloading"})
                _, job = job_manifest.load_job(path)
                finalization_queue.start_all(path)
                root = finalization_queue.artifact_root(job)
                metadata = root / "metadata.json"
                metadata.write_text(json.dumps({
                    "title": "Example", "year": 2024,
                    "directors": ["Director"], "plot": "Test movie.",
                    "uniqueids": {"imdb": "tt1234567"},
                    "default_uniqueid": "imdb",
                    "letterboxd_url": "https://letterboxd.com/film/example/",
                    "senscritique_url": "https://www.senscritique.com/film/example/123",
                    "recommendations": [],
                }), encoding="utf-8")
                poster = root / "poster.png"
                poster.write_bytes(base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z8l8AAAAASUVORK5CYII="
                ))
                subtitle_text = "\n\n".join([
                    "1\n00:00:00,000 --> 00:00:00,300\nOne",
                    "2\n00:00:00,350 --> 00:00:00,650\nTwo",
                    "3\n00:00:00,700 --> 00:00:01,000\nThree",
                    "4\n00:00:01,050 --> 00:00:01,350\nFour",
                    "5\n00:00:01,400 --> 00:00:01,900\nFive",
                ]) + "\n"
                en = root / "example.en.srt"
                fr = root / "example.fr.srt"
                en.write_text(subtitle_text, encoding="utf-8")
                fr.write_text(subtitle_text, encoding="utf-8")
                for task, artifact in (
                    ("metadata", metadata), ("artwork", poster),
                    ("subtitle-en", en), ("subtitle-fr", fr),
                ):
                    finalization_queue.mark(path, task, "complete", artifact=artifact)
                for task in ("destination", "film-links", "recommendations"):
                    finalization_queue.mark(path, task, "complete", note="prepared")

                info_hash = "a" * 40
                transfer = _common.stage_for_kind("movie") / "transfers" / info_hash
                transfer.mkdir(parents=True)
                media = transfer / "Example.mkv"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=size=320x180:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
                    "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(media),
                ], check=True)
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
                                "tags": "movies-nerd,movie",
                            }] if self.present else []
                        raise AssertionError(endpoint)

                    def request(self, endpoint, _fields=None):
                        if endpoint == "torrents/delete":
                            self.present = False
                        return b"Ok."

                client = Client()
                with (
                    patch.object(finalize_job, "connected_client", return_value=client),
                    patch.object(finish_staging, "connected_client", return_value=client),
                ):
                    result = finalize_job.finalize(path)
                destination = Path(result["destination"])
                files = {item.name for item in destination.iterdir()}
            self.assertTrue(result["ready"])
            self.assertTrue(result["cleanup"]["clean"])
            self.assertIn("Example (2024) [480p].mkv", files)
            self.assertIn("Example (2024) [480p].nfo", files)
            self.assertIn("Example (2024).png", files)
            self.assertIn("Example (2024) [480p].en.srt", files)
            self.assertIn("Example (2024) [480p].fr.srt", files)
            self.assertFalse(path.exists())

    def test_finalization_queue_marks_independent_tasks(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {"state": "downloading"})
                plan = finalization_queue.start_all(path)
                self.assertTrue(plan["parallel"])
                self.assertEqual(len(plan["tasks"]), 7)
                updated = finalization_queue.mark(path, "film-links", "complete", note="verified")
                links = next(item for item in updated["tasks"] if item["name"] == "film-links")
                self.assertEqual(links["status"], "complete")

    def test_movie_artifacts_are_prepared_without_per_file_orchestration(self):
        tiny_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z8l8AAAAASUVORK5CYII="
        )
        subtitle = "\n\n".join([
            "1\n00:00:00,000 --> 00:00:05,000\nOne",
            "2\n00:10:00,000 --> 00:10:05,000\nTwo",
            "3\n00:30:00,000 --> 00:30:05,000\nThree",
            "4\n00:50:00,000 --> 00:50:05,000\nFour",
            "5\n01:35:00,000 --> 01:35:05,000\nFive",
        ]).encode() + b"\n"
        meta = {
            "id": "tt1234567", "name": "Example", "year": "2024",
            "director": ["Director"], "description": "Plot", "runtime": "100 min",
            "genre": ["Drama"], "imdbRating": "7.5", "moviedb_id": 42,
            "released": "2024-01-02T00:00:00.000Z",
        }
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job(
                    "movie", "Example", 2024,
                    {"identity": {"ids": {"imdb": "tt1234567"}}},
                )
                job_manifest.update_job(path, {"state": "downloading"})
                with (
                    patch.object(prepare_artifacts.cinemeta, "metadata", return_value=meta),
                    patch.object(prepare_artifacts.cinemeta, "artwork", return_value=tiny_png),
                    patch.object(prepare_artifacts, "subtitle_bytes", return_value=subtitle),
                ):
                    result = prepare_artifacts.prepare(path)
                _, job = job_manifest.load_job(path)
                root = finalization_queue.artifact_root(job)
                metadata = json.loads((root / "metadata.json").read_text())
            self.assertTrue(result["automated"])
            self.assertTrue(result["ready"])
            self.assertTrue(all(
                item["status"] == "complete"
                for item in finalization_queue.task_state(job).values()
            ))
            self.assertEqual(metadata["directors"], ["Director"])
            self.assertEqual(metadata["uniqueids"]["imdb"], "tt1234567")
            self.assertTrue((root / "poster.png").is_file())
            self.assertTrue((root / "subtitle.en.srt").is_file())
            self.assertTrue((root / "subtitle.fr.srt").is_file())
