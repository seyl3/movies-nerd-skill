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


class CleanupV2Tests(unittest.TestCase):
    def test_appledouble_hygiene_removes_only_scoped_sidecars(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "job"
            nested = root / "nested"
            nested.mkdir(parents=True)
            keep = nested / "movie.mkv"
            keep.write_bytes(b"video")
            sidecar = nested / "._movie.mkv"
            sidecar.write_bytes(b"AppleDouble")
            removed = _common.clean_appledouble_tree(root)
            self.assertEqual(removed, [str(sidecar)])
            self.assertTrue(keep.exists())
            self.assertFalse(sidecar.exists())

    def test_success_cleanup_keeps_only_provider_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = roots(base)
            movies = Path(env[_common.MOVIES_ROOT_ENV])
            with patch.dict(os.environ, env, clear=False):
                provider_health.record_provider("movie", "YTS API", ok=True, latency_ms=100, results=2)
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {"state": "imported"})
                stage = movies / ".incoming" / "Movies Nerd"
                transfer = stage / "transfers" / ("a" * 40)
                transfer.mkdir(parents=True)
                (transfer / "junk.txt").write_text("junk", encoding="utf-8")
                final = movies / "Director" / "Example (2024)"
                final.mkdir(parents=True)
                (final / "Example (2024) [1080p].mkv").write_bytes(b"video")
                (final / "Example (2024) [1080p].nfo").write_text("<movie/>", encoding="utf-8")
                (final / "Example (2024).png").write_bytes(b"png")
                with patch.object(finish_staging, "verify_recorded_torrents_absent", return_value={"checked": 0, "all_absent": True}):
                    result = finish_staging.clean_completed_job(final, [transfer], path)
            state = movies / ".movies-nerd"
            self.assertTrue(result["clean"])
            self.assertTrue((state / "cache" / "provider-health.json").is_file())
            self.assertFalse((state / "jobs").exists())
            self.assertFalse((state / "trash").exists())
            self.assertFalse(stage.exists())

    def test_success_cleanup_refuses_when_recorded_torrent_remains(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = roots(base)
            movies = Path(env[_common.MOVIES_ROOT_ENV])
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "imported",
                    "controller": {"active_hash": "a" * 40, "tried_hashes": ["a" * 40]},
                })
                transfer = movies / ".incoming" / "Movies Nerd" / "transfers" / ("a" * 40)
                transfer.mkdir(parents=True)
                final = movies / "Director" / "Example (2024)"
                final.mkdir(parents=True)
                (final / "Example.mkv").write_bytes(b"video")
                (final / "Example.nfo").write_text("<movie/>", encoding="utf-8")
                (final / "Example.png").write_bytes(b"png")
                with (
                    patch.object(finish_staging, "connected_client", return_value=object()),
                    patch.object(finish_staging, "torrent_info", return_value={"hash": "a" * 40}),
                ):
                    with self.assertRaisesRegex(ValueError, "remove the exact"):
                        finish_staging.clean_completed_job(final, [transfer], path)
                self.assertTrue(path.exists())
                self.assertTrue(transfer.exists())
