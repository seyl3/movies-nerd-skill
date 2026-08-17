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


class ControllerV2Tests(unittest.TestCase):
    def test_batch_runner_starts_all_requested_jobs_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                paths = [job_manifest.create_job("movie", f"Example {index}", 2024) for index in range(6)]
                active = 0
                maximum = 0

                def runner(path, **_kwargs):
                    nonlocal active, maximum
                    active += 1
                    maximum = max(maximum, active)
                    time.sleep(0.02)
                    active -= 1
                    return {"ready": True, "title": path.stem}

                result = batch_jobs.run_many(paths, runner=runner)
            self.assertEqual(maximum, 6)
            self.assertEqual(result["concurrency"], 6)
            self.assertEqual(result["ready"], 6)
            self.assertEqual([item["job"] for item in result["jobs"]], [str(path) for path in paths])

    def test_batch_input_is_not_capped_at_twenty_titles(self):
        items = [
            {"title": f"Example {index}", "year": 2024, "kind": "movie"}
            for index in range(25)
        ]
        self.assertEqual(len(batch_jobs.checked_items({"items": items})), 25)

    def test_acquire_help_is_compact(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "acquire.py"), "--help"],
            check=True, text=True, capture_output=True,
        )
        self.assertLess(len(completed.stdout), 2_000)
        self.assertIn("0..86400", completed.stdout)

    def test_slow_transfer_replaces_candidate_without_stopping_controller(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                first = candidate("a" * 40)
                second = candidate("b" * 40, "backup")
                job_manifest.update_job(path, {
                    "state": "downloading", "release": first,
                    "candidate_pool": [first, second],
                    "controller": {
                        "active_hash": "a" * 40,
                        "tried_hashes": ["a" * 40],
                    },
                    "artifacts": {"torrent_hash": "a" * 40},
                })

                class Client:
                    def request(self, *_args, **_kwargs):
                        return b"Ok."

                slow = {
                    "hash": "a" * 40, "state": "downloading", "progress": 0.1,
                    "downloaded": 100, "dlspeed": 1_000, "availability": 1.0,
                }
                complete = {
                    "hash": "b" * 40, "state": "uploading", "progress": 1.0,
                    "downloaded": 2_000_000_000, "dlspeed": 0, "availability": 1.0,
                }
                samples = iter([
                    monitor_download.to_sample(slow, 0),
                    monitor_download.to_sample(slow, 1),
                    monitor_download.to_sample(complete, 2),
                ])
                sync_values = iter([(1, slow), (2, slow), (3, complete)])

                def replace(job_path, _job, _client, _old):
                    job_manifest.transition_job(job_path, "replacement-started")
                    job_manifest.transition_job(
                        job_path, "downloading", torrent_hash="b" * 40,
                        release=second,
                    )
                    updated = job_manifest.update_job(job_path, {
                        "controller": {
                            "active_hash": "b" * 40,
                            "tried_hashes": ["a" * 40, "b" * 40],
                        },
                    })
                    return "b" * 40, updated

                output = io.StringIO()
                with (
                    patch.object(acquire, "connected_client", return_value=Client()),
                    patch.object(acquire, "preflight", return_value={"ready": True}),
                    patch.object(acquire, "torrent_info", return_value=slow),
                    patch.object(acquire, "enforce_single_transfer", side_effect=lambda _p, j, _c, _h: j),
                    patch.object(acquire, "request_finalization"),
                    patch.object(acquire, "sync_torrent", side_effect=lambda *_args: next(sync_values)),
                    patch.object(acquire, "to_sample", side_effect=lambda _info: next(samples)),
                    patch.object(acquire, "replace_active", side_effect=replace) as failover,
                    patch.object(acquire, "DEFAULT_LOW_SPEED_SECONDS", 1),
                    patch.object(acquire, "DEFAULT_LOW_SPEED_BPS", 256 * 1024),
                    patch.object(acquire.time, "sleep"),
                    redirect_stdout(output),
                ):
                    result = acquire.run(path, poll_seconds=2)
            self.assertTrue(result["downloaded"])
            failover.assert_called_once()
            events = [json.loads(line)["event"] for line in output.getvalue().splitlines()]
            self.assertEqual(events, [
                "download-started", "download-stalled",
                "source-replaced", "download-completed",
            ])
            self.assertNotIn("progress", events)

    def test_acquisition_resumes_existing_active_hash(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "downloading",
                    "controller": {"active_hash": "a" * 40},
                    "artifacts": {"torrent_hash": "a" * 40},
                })
                _, job = job_manifest.load_job(path)
                with patch.object(acquire, "torrent_info", return_value={"hash": "a" * 40}):
                    active, resumed = acquire.ensure_active(path, job, object())
        self.assertEqual(active, "a" * 40)
        self.assertEqual(resumed["state"], "downloading")

    def test_controller_runs_existing_transfer_to_downloaded_state(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "downloading",
                    "release": candidate("a" * 40),
                    "controller": {"active_hash": "a" * 40, "tried_hashes": ["a" * 40]},
                    "artifacts": {"torrent_hash": "a" * 40},
                })

                class Client:
                    def request(self, *_args, **_kwargs):
                        return b"Ok."

                complete = {
                    "hash": "a" * 40, "state": "uploading", "progress": 1.0,
                    "downloaded": 2_000_000_000, "dlspeed": 0,
                }
                with (
                    patch.object(acquire, "connected_client", return_value=Client()),
                    patch.object(acquire, "preflight", return_value={"ready": True}),
                    patch.object(acquire, "torrent_info", return_value=complete),
                    patch.object(acquire, "sync_torrent", return_value=(1, complete)),
                    redirect_stdout(io.StringIO()),
                ):
                    result = acquire.run(path, poll_seconds=2)
                _, final_job = job_manifest.load_job(path)
            self.assertTrue(result["downloaded"])
            self.assertEqual(final_job["state"], "downloaded")
            self.assertEqual(final_job["controller"]["phase"], "downloaded")

    def test_controller_activates_prevalidated_standby(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                backup = candidate("b" * 40, "backup")
                job_manifest.update_job(path, {
                    "state": "stalled", "release": candidate("a" * 40),
                    "backup_release": backup,
                    "controller": {
                        "active_hash": "a" * 40, "standby_hash": "b" * 40,
                        "tried_hashes": ["a" * 40, "b" * 40],
                    },
                    "artifacts": {"torrent_hash": "a" * 40},
                })
                _, job = job_manifest.load_job(path)
                with (
                    patch.object(acquire, "torrent_info", return_value={"hash": "b" * 40}),
                    patch.object(acquire, "remove_and_verify"),
                    patch.object(acquire, "command_start", return_value={"started": True}),
                    patch.object(acquire, "record_hash"),
                ):
                    active, updated = acquire.activate_standby(path, job, object(), "a" * 40)
            self.assertEqual(active, "b" * 40)
            self.assertEqual(updated["state"], "downloading")
            self.assertIsNone(updated["controller"]["standby_hash"])

    def test_stale_controller_lock_is_replaced_and_removed(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / ".movies-nerd"
            job_dir = state / "jobs"
            job_dir.mkdir(parents=True)
            job = job_dir / "job.json"
            job.write_text("{}", encoding="utf-8")
            lock = state / "locks" / "abc.lock"
            lock.parent.mkdir()
            lock.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
            with acquire.JobLock(job, "abc"):
                self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())
