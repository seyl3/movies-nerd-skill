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
import threading
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


class SearchV2Tests(unittest.TestCase):
    def test_yts_keeps_valid_direct_torrent_and_zero_peer_estimate(self):
        result = search_releases.normalize_yts({
            "status": "ok",
            "data": {"movies": [{
                "title": "Example", "year": 2024, "imdb_code": "tt1234567",
                "torrents": [{
                    "hash": "a" * 40, "quality": "1080p", "type": "bluray",
                    "video_codec": "x265", "size_bytes": 2_000_000_000,
                    "seeds": 0, "peers": 0,
                    "url": "https://yts.mx/torrent/download/" + "a" * 40,
                }],
            }]},
        })[0]
        self.assertEqual(result["torrent_url"], "https://yts.mx/torrent/download/" + "a" * 40)
        self.assertEqual(result["seeders"], 0)
        self.assertEqual(result["imdb_code"], "tt1234567")

    def test_zero_reported_seeders_do_not_disqualify_candidate(self):
        selection = search_releases.release_selection(
            [candidate("a" * 40, "YTS API")], "Example", 2024,
            15 * qbt.GIB, 90,
        )
        self.assertEqual(selection["eligible_count"], 1)
        self.assertEqual(selection["primary"]["seeders"], 0)

    def test_dead_hash_is_excluded_from_selection(self):
        selection = search_releases.release_selection(
            [candidate("a" * 40), candidate("b" * 40)], "Example", 2024,
            15 * qbt.GIB, 90, excluded_hashes={"a" * 40},
        )
        self.assertEqual(selection["primary"]["info_hash"], "b" * 40)

    def test_direct_metadata_wins_duplicate_hash(self):
        base = candidate("a" * 40)
        direct = {**base, "seeders": 1, "torrent_url": "https://yts.mx/torrent/download/" + "a" * 40}
        reported = {**base, "seeders": 100}
        result = search_releases.deduplicate([reported, direct])
        self.assertEqual(result[0]["torrent_url"], direct["torrent_url"])

    def test_provider_deadline_does_not_wait_for_slow_worker(self):
        release = threading.Event()

        def slow():
            release.wait(1)
            return []

        started = time.monotonic()
        try:
            _results, reports = search_releases.run_providers({"slow": slow}, 0.03)
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(reports["slow"]["ok"])
