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


class MonitoringV2Tests(unittest.TestCase):
    def test_race_uses_second_wave_after_dead_first_wave(self):
        candidates = [candidate(f"{index:x}" * 40, f"source-{index}") for index in range(1, 5)]
        dead = race_candidates.Probe(candidates[0], "1" * 40, 1.0, "magnet")
        live = race_candidates.Probe(candidates[3], "4" * 40, 1.0, "magnet")
        live.bytes_delta = 4_000_000
        live.speeds = [500_000, 600_000]
        live.availability = 1.0
        live.peers = 2
        with (
            patch.object(race_candidates, "prepare_wave", side_effect=[([dead], []), ([live], [])]) as prepare,
            patch.object(race_candidates, "probe_wave"),
            patch.object(race_candidates, "record_outcome"),
            patch.object(race_candidates, "remove_quietly"),
            patch.object(race_candidates, "command_start", return_value={"started": True}),
        ):
            winner, result = race_candidates.race(object(), candidates, "movie", "Example", 10, 5)
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(winner["source"], "source-4")
        self.assertEqual(result["wave"], 2)

    def test_monitor_waits_through_short_pause(self):
        samples = [
            monitor_download.Sample(0, 1_000, 0, 0.1, "stalledDL", 0, 0),
            monitor_download.Sample(30, 1_000, 0, 0.1, "stalledDL", 0, 0),
        ]
        report = monitor_download.assess_samples(samples, "source", no_progress_seconds=60)
        self.assertFalse(report["stalled"])

    def test_monitor_fails_over_after_sixty_seconds_without_bytes(self):
        samples = [
            monitor_download.Sample(0, 1_000, 0, 0.1, "stalledDL", 0, 0),
            monitor_download.Sample(60, 1_000, 0, 0.1, "stalledDL", 0, 0),
        ]
        report = monitor_download.assess_samples(samples, "source", no_progress_seconds=60)
        self.assertTrue(report["stalled"])
        self.assertTrue(any("downloaded bytes" in reason for reason in report["reasons"]))
