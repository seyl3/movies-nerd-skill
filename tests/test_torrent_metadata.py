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


class TorrentMetadataTests(unittest.TestCase):
    def test_valid_torrent_hash_and_shape(self):
        raw, info_hash = torrent_fixture()
        report = torrent_metadata.inspect_torrent(raw, info_hash)
        self.assertEqual(report["info_hash"], info_hash)
        self.assertEqual(report["file_count"], 1)
        self.assertEqual(report["total_size"], 2_000_000_000)

    def test_torrent_hash_mismatch_is_rejected(self):
        raw, _ = torrent_fixture()
        with self.assertRaisesRegex(torrent_metadata.TorrentMetadataError, "does not match"):
            torrent_metadata.inspect_torrent(raw, "0" * 40)

    def test_torrent_traversal_path_is_rejected(self):
        raw, _ = torrent_fixture([b"..", b"escape.mkv"])
        with self.assertRaisesRegex(torrent_metadata.TorrentMetadataError, "unsafe"):
            torrent_metadata.inspect_torrent(raw)

    def test_binary_multipart_uses_torrent_file_part(self):
        body, content_type = qbt.multipart({"paused": "true", "torrents": b"d4:infode"})
        self.assertIn(b'filename="candidate.torrent"', body)
        self.assertIn(b"application/x-bittorrent", body)
        self.assertIn("multipart/form-data", content_type)

    def test_fixed_tracker_magnet_keeps_canonical_xt(self):
        magnet = qbt.safe_magnet("a" * 40, "Example (2024)")
        self.assertTrue(magnet.startswith("magnet:?xt=urn:btih:" + "a" * 40))
        self.assertIn("&tr=udp%3A", magnet)
        self.assertEqual(qbt.magnet_hash(magnet), "a" * 40)

    def test_torrent_fetch_uses_system_trust_fallback(self):
        raw, info_hash = torrent_fixture([b"movie.mkv"])
        opener = type("BrokenOpener", (), {
            "open": lambda *args, **kwargs: (_ for _ in ()).throw(
                URLError(ssl.SSLCertVerificationError("missing local CA bundle"))
            )
        })()
        with patch.object(torrent_metadata, "build_opener", return_value=opener), patch.object(
            torrent_metadata, "fetch_with_system_curl", return_value=raw,
        ) as fallback:
            fetched = torrent_metadata.fetch_torrent(
                "https://yts.gg/torrent/download/" + info_hash.upper()
            )
        self.assertEqual(fetched, raw)
        fallback.assert_called_once()
