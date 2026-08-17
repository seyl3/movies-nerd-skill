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


class SubtitleV2Tests(unittest.TestCase):
    def test_automated_subtitles_accept_verified_embedded_coverage(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with (
                patch.object(
                    prepare_artifacts, "subtitle_bytes",
                    side_effect=prepare_artifacts.ArtifactPreparationError("not found"),
                ),
                patch.object(prepare_artifacts, "active_movie_languages", return_value={"eng"}),
            ):
                result = prepare_artifacts.movie_subtitle(
                    root / "job.json", root, "tt1234567", "en", 6000, None,
                )
            self.assertIn("embedded", result)

            with patch.object(prepare_artifacts, "subtitle_bytes") as fetch:
                manifest = prepare_artifacts.series_subtitle(root, "tt1234567", "fr", [{
                    "source": "Show.S01E01.mkv", "season": 1, "episode": 1,
                    "duration": 2400, "languages": ["fre"],
                }])
            fetch.assert_not_called()
            self.assertEqual(json.loads(manifest.read_text())["subtitles"], [])
