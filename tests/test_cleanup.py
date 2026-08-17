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


class CleanupTests(unittest.TestCase):
    def test_clutter_finder_detects_portuguese_and_apple_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".DS_Store").write_bytes(b"x")
            (root / "Film.pt.srt").write_text("subtitle", encoding="utf-8")
            (root / "Film.en.srt").write_text("subtitle", encoding="utf-8")
            names = [path.name for path in clean_clutter.targets(root)]
            self.assertEqual(names, [".DS_Store", "Film.pt.srt"])

    def test_clutter_cleanup_leaves_no_hidden_trash_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            movies = base / "Films"
            series = base / "Series"
            movies.mkdir()
            series.mkdir()
            clutter = movies / ".DS_Store"
            clutter.write_bytes(b"x")
            argv = ["clean_clutter.py", str(movies), "--commit"]
            output = io.StringIO()
            with (
                patch.dict(os.environ, {
                    _common.MOVIES_ROOT_ENV: str(movies),
                    _common.SERIES_ROOT_ENV: str(series),
                }, clear=False),
                patch.object(sys, "argv", argv),
                redirect_stdout(output),
            ):
                self.assertEqual(clean_clutter.main(), 0)
            report = json.loads(output.getvalue())
            self.assertFalse(clutter.exists())
            self.assertEqual(report["removed"], [str(clutter.resolve(strict=False))])
            self.assertFalse((movies / ".movies-nerd-trash").exists())

    def test_finished_job_leaves_no_incoming_or_job_trash(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            movies = base / "Movies"
            series = base / "Series"
            stage = movies / ".incoming" / "Movies Nerd"
            source = stage / "transfers" / ("a" * 40)
            source.mkdir(parents=True)
            (source / "junk.nfo").write_bytes(b"untrusted")
            sidecar = source.with_name("._" + source.name)
            sidecar.write_bytes(b"AppleDouble")
            final = movies / "Director" / "Example (2024)"
            final.mkdir(parents=True)
            (final / "Example (2024) [1080p].mkv").write_bytes(b"video")
            (final / "Example (2024) [1080p].nfo").write_text("<movie/>", encoding="utf-8")
            (final / "Example (2024).png").write_bytes(b"image")
            env = {
                _common.MOVIES_ROOT_ENV: str(movies),
                _common.SERIES_ROOT_ENV: str(series),
            }
            with patch.dict(os.environ, env, clear=False):
                job = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(job, {"state": "imported"})
                with patch.object(finish_staging, "verify_recorded_torrents_absent", return_value={"checked": 0, "all_absent": True}):
                    result = finish_staging.clean_completed_job(final, [source], job)
            self.assertFalse(source.exists())
            self.assertFalse(sidecar.exists())
            self.assertFalse(job.exists())
            self.assertTrue(result["clean"])
            self.assertFalse((movies / ".movies-nerd" / "trash").exists())
            self.assertFalse(stage.exists())

    def test_finished_job_verification_ignores_macos_sidecars(self):
        with tempfile.TemporaryDirectory() as raw:
            final = Path(raw) / "Example (2024)"
            final.mkdir()
            (final / "._Example (2024) [1080p].mkv").write_bytes(b"AppleDouble")
            (final / "._Example (2024) [1080p].nfo").write_bytes(b"AppleDouble")
            (final / "._Example (2024).png").write_bytes(b"AppleDouble")
            with self.assertRaisesRegex(ValueError, "not fully organized"):
                finish_staging.verify_final_destination(final)
