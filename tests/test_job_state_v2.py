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


class JobStateV2Tests(unittest.TestCase):
    def test_prepare_job_searches_and_records_without_temporary_json(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            items = [candidate("a" * 40), candidate("b" * 40, "backup")]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(prepare_job, "dead_hashes", return_value=set()),
                patch.object(
                    prepare_job, "search_all",
                    return_value=(items, {"yts": {"ok": True, "results": 2, "latency_ms": 25}}, True),
                ),
                patch.object(
                    cinemeta, "metadata",
                    return_value={"name": "Example", "year": "2024"},
                ),
                patch.object(prepare_job, "record_provider"),
            ):
                result = prepare_job.prepare(
                    title="Example", year=2024, kind="movie",
                    runtime_minutes=100, imdb_id="tt1234567",
                    max_gib=15, timeout=5,
                )
                job_path = Path(result["job"])
                _, job = job_manifest.load_job(job_path)
            self.assertTrue(result["prepared"])
            self.assertTrue(result["authorized"])
            self.assertNotIn("confirmation", result)
            self.assertEqual(job["state"], "confirmed")
            self.assertEqual(job["identity"]["ids"]["imdb"], "tt1234567")
            self.assertEqual(len(job["candidate_pool"]), 2)
            self.assertFalse(any(job_path.parent.glob("*search*.json")))

    def test_prepare_job_resolves_missing_imdb_id_automatically(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            items = [candidate("a" * 40)]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(prepare_job, "dead_hashes", return_value=set()),
                patch.object(
                    prepare_job, "search_all",
                    return_value=(items, {"apibay": {"ok": True, "results": 1, "latency_ms": 25}}, True),
                ),
                patch.object(prepare_job, "record_provider"),
                patch.object(
                    cinemeta, "resolve_identity",
                    return_value={"imdb_id": "tt7654321", "canonical_title": "Example"},
                ) as resolve,
            ):
                result = prepare_job.prepare(
                    title="Example", year=2024, kind="movie",
                    runtime_minutes=100, imdb_id=None,
                    max_gib=15, timeout=5,
                )
                _, job = job_manifest.load_job(result["job"])
            resolve.assert_called_once_with("movie", "Example", 2024)
            self.assertEqual(job["identity"]["ids"]["imdb"], "tt7654321")

    def test_inline_json_and_authoritative_ids_are_supported(self):
        value = job_manifest.read_json('{"cache":{"runtime_minutes":121}}')
        self.assertEqual(value["cache"]["runtime_minutes"], 121)
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            path = job_manifest.create_job(
                "movie", "Example", 2024,
                {"identity": {"ids": {"imdb": "tt1234567"}}},
                environ=env,
            )
            _, job = job_manifest.load_job(path, env)
            self.assertEqual(job["identity"]["ids"]["imdb"], "tt1234567")

    def test_provider_health_is_private_and_dead_hash_expires(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            provider_health.record_provider(
                "movie", "YTS API", ok=True, latency_ms=200, results=4,
                environ=env,
            )
            provider_health.record_hash(
                "movie", "a" * 40, "dead", provider="YTS API", environ=env,
            )
            path = provider_health.cache_path("movie", env)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("a" * 40, provider_health.dead_hashes("movie", env))
            value = provider_health.load("movie", env)
            value["hashes"]["a" * 40]["updated_epoch"] = 1
            self.assertNotIn("a" * 40, provider_health.prune(value, now=provider_health.DEAD_TTL_SECONDS + 2)["hashes"])

    def test_job_manifest_lives_under_state_not_incoming(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            path = job_manifest.create_job("movie", "Example", 2024, environ=env)
            self.assertIn("/.movies-nerd/jobs/", str(path))
            self.assertNotIn("/.incoming/", str(path))
            _, value = job_manifest.load_job(path, env)
            self.assertEqual(value["version"], 2)

    def test_search_manifest_accepts_two_probe_waves(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = roots(base)
            job = job_manifest.create_job("movie", "Example", 2024, environ=env)
            items = [candidate(f"{index:x}" * 40, f"source-{index}", 100 - index) for index in range(1, 7)]
            selection = {
                "primary": items[0], "backup": items[1], "candidates": items,
                "eligible_count": 6,
                "confirmation_envelope": {
                    "quality": "1080p", "max_size_bytes": 2_000_000_000,
                },
            }
            result = base / "search.json"
            result.write_text(json.dumps({
                "request": {"title": "Example", "year": 2024, "kind": "movie"},
                "selection": selection,
            }), encoding="utf-8")
            recorded = job_manifest.record_search(job, result, env)
            self.assertEqual(len(recorded["candidate_pool"]), 6)

    def test_failed_manifest_survives_until_job_trash_is_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            path = job_manifest.create_job("movie", "Example", 2024, environ=env)
            job_manifest.transition_job(path, "failed", reason="test", environ=env)
            _, value = job_manifest.load_job(path, env)
            trash = path.parent.parent / "trash" / value["job_id"]
            trash.mkdir(parents=True)
            with self.assertRaisesRegex(job_manifest.ManifestError, "trash must be cleared"):
                job_manifest.remove_failed_job(path, environ=env)
            self.assertTrue(path.exists())
