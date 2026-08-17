from __future__ import annotations

import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common
import qbittorrent_api as qbt
import clean_clutter
import check_environment
import check_subtitles
import edit_mkv_headers
import finish_staging
import job_manifest
import media_probe
import monitor_download
import opensubtitles_api
import payload_safety
import rank_releases
import race_candidates
import remux_mkv
import search_releases
import select_payload
import skill_version
import stremio_subtitles
import subtitle_provider
import validate_subtitle
import write_nfo


class LibraryStateTests(unittest.TestCase):
    def test_library_roots_default_to_documents(self):
        movies, series = _common.library_roots({})
        self.assertEqual(movies, Path.home() / "Documents" / "Movies")
        self.assertEqual(series, Path.home() / "Documents" / "Series")

    def test_library_roots_accept_separate_custom_paths(self):
        env = {
            _common.MOVIES_ROOT_ENV: "/private/tmp/media/Movies",
            _common.SERIES_ROOT_ENV: "/private/tmp/media/Series",
        }
        movies, series = _common.library_roots(env)
        self.assertEqual(movies, Path("/private/tmp/media/Movies"))
        self.assertEqual(series, Path("/private/tmp/media/Series"))

    def test_movie_root_does_not_need_to_be_named_movies(self):
        env = {
            _common.MOVIES_ROOT_ENV: "/Volumes/ssd/Films",
            _common.SERIES_ROOT_ENV: "/Volumes/ssd/Series",
        }
        movies, series = _common.library_roots(env)
        self.assertEqual(movies, Path("/Volumes/ssd/Films"))
        self.assertEqual(series, Path("/Volumes/ssd/Series"))

    def test_library_roots_reject_nested_paths(self):
        env = {
            _common.MOVIES_ROOT_ENV: "/private/tmp/media",
            _common.SERIES_ROOT_ENV: "/private/tmp/media/Series",
        }
        with self.assertRaises(ValueError):
            _common.library_roots(env)

    def test_job_manifest_is_private_atomic_and_resumable(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = {
                _common.MOVIES_ROOT_ENV: str(base / "Movies"),
                _common.SERIES_ROOT_ENV: str(base / "Series"),
            }
            path = job_manifest.create_job(
                "movie", "Example", 2024,
                {"release": {"magnet": "magnet:?xt=urn:btih:" + "a" * 40}},
                env,
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            updated = job_manifest.update_job(
                path,
                {"state": "downloading", "steps": {"search": "complete", "transfer": "running"}},
                env,
            )
            self.assertEqual(updated["state"], "downloading")
            self.assertEqual(updated["steps"]["confirmation"], "pending")
            _, loaded = job_manifest.load_job(path, env)
            self.assertEqual(loaded["steps"]["search"], "complete")
            redacted = job_manifest.redacted(loaded)["release"]
            self.assertTrue(redacted["magnet_stored"])
            self.assertEqual(redacted["info_hash"], "a" * 40)

    def test_job_manifest_rejects_credentials_and_outside_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = {
                _common.MOVIES_ROOT_ENV: str(base / "Movies"),
                _common.SERIES_ROOT_ENV: str(base / "Series"),
            }
            with self.assertRaises(job_manifest.ManifestError):
                job_manifest.create_job("movie", "Example", 2024, {"api_key": "secret"}, env)
            outside = base / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaises(job_manifest.ManifestError):
                job_manifest.load_job(outside, env)

    def test_job_manifest_records_primary_and_backup_search_results(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = {
                _common.MOVIES_ROOT_ENV: str(base / "Movies"),
                _common.SERIES_ROOT_ENV: str(base / "Series"),
            }
            job = job_manifest.create_job("movie", "Example", 2024, environ=env)
            primary = {
                "title": "Example 2024 1080p x265", "source": "1337x",
                "provider": "Knaben API", "size_bytes": 2_000_000_000,
                "size": "1.86 GiB", "resolution": "1080p", "seeders": 30,
                "leechers": 2, "score": 350, "warnings": [],
                "magnet": search_releases.minimal_magnet("a" * 40, "Primary"),
            }
            backup = {
                **primary,
                "source": "The Pirate Bay",
                "provider": "APIBay",
                "magnet": search_releases.minimal_magnet("b" * 40, "Backup"),
            }
            result = base / "search.json"
            result.write_text(json.dumps({
                "query": "Example 2024",
                "request": {"title": "Example", "year": 2024, "kind": "movie"},
                "elapsed_ms": 800,
                "usable_results": 2,
                "selection": {"primary": primary, "backup": backup, "eligible_count": 2},
            }), encoding="utf-8")
            recorded = job_manifest.record_search(job, result, env)
            self.assertEqual(recorded["steps"]["search"], "complete")
            self.assertEqual(recorded["backup_release"]["source"], "The Pirate Bay")
            self.assertEqual(len(recorded["candidate_pool"]), 2)
            redacted = job_manifest.redacted(recorded)["backup_release"]
            self.assertTrue(redacted["magnet_stored"])
            self.assertEqual(redacted["info_hash"], "b" * 40)

    def test_job_transitions_keep_steps_consistent(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = {
                _common.MOVIES_ROOT_ENV: str(base / "Movies"),
                _common.SERIES_ROOT_ENV: str(base / "Series"),
            }
            job = job_manifest.create_job("movie", "Example", 2024, environ=env)
            confirmed = job_manifest.transition_job(job, "confirmed", environ=env)
            self.assertEqual(confirmed["steps"]["confirmation"], "complete")
            metadata = job_manifest.transition_job(job, "metadata-started", environ=env)
            self.assertEqual(metadata["steps"]["metadata_gate"], "running")
            failed = job_manifest.transition_job(
                job, "metadata-failed", reason="no metadata", environ=env,
            )
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["steps"]["transfer"], "skipped")

    def test_terminal_failed_job_state_is_removed(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = {
                _common.MOVIES_ROOT_ENV: str(base / "Movies"),
                _common.SERIES_ROOT_ENV: str(base / "Series"),
            }
            job = job_manifest.create_job("movie", "Example", 2024, environ=env)
            job_manifest.transition_job(job, "failed", reason="test", environ=env)
            result = job_manifest.remove_failed_job(job, environ=env)
            self.assertFalse(job.exists())
            self.assertTrue(result["removed"])
            self.assertTrue(result["state_clean"])
