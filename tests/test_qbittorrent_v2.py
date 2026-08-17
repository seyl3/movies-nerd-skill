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


class QbittorrentV2Tests(unittest.TestCase):
    def test_resumed_job_removes_every_duplicate_candidate(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "downloading",
                    "release": candidate("a" * 40),
                    "backup_release": candidate("b" * 40, "backup"),
                    "candidate_pool": [candidate("a" * 40), candidate("b" * 40), candidate("c" * 40)],
                    "controller": {
                        "active_hash": "a" * 40,
                        "standby_hash": "b" * 40,
                        "tried_hashes": ["a" * 40, "b" * 40, "c" * 40],
                    },
                })
                _, job = job_manifest.load_job(path)
                present = {"a" * 40, "b" * 40, "c" * 40}

                def info(_client, value):
                    if value not in present:
                        raise qbt.QbtError("torrent is not present in qBittorrent")
                    return {"hash": value}

                def remove(_client, value):
                    present.discard(value)

                with (
                    patch.object(acquire, "torrent_info", side_effect=info),
                    patch.object(acquire, "remove_and_verify", side_effect=remove),
                ):
                    updated = acquire.enforce_single_transfer(path, job, object(), "a" * 40)
            self.assertEqual(present, {"a" * 40})
            self.assertIsNone(updated["controller"]["standby_hash"])
            self.assertEqual(updated["controller"]["duplicate_hashes_removed"], ["b" * 40, "c" * 40])

    def test_exact_qbittorrent_removal_deletes_partial_candidate_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                info_hash = "d" * 40
                transfer = _common.stage_for_kind("movie") / "transfers" / info_hash
                transfer.mkdir(parents=True)
                (transfer / "partial.mkv").write_bytes(b"partial")
                info = {
                    "hash": info_hash,
                    "save_path": str(transfer),
                    "tags": "movies-nerd, movie",
                }

                class Client:
                    def request(self, *_args, **_kwargs):
                        return b"Ok."

                with patch.object(qbt, "torrent_info", side_effect=[info, qbt.QbtError("torrent is not present in qBittorrent")]):
                    result = qbt.remove_movies_nerd_torrent(Client(), info_hash)
            self.assertTrue(result["staged_payload_removed"])
            self.assertFalse(transfer.exists())

    def test_qbittorrent_preflight_reads_connection_once(self):
        class Client:
            def request(self, endpoint, *_args, **_kwargs):
                self.endpoint = endpoint
                return b"5.2.1"

            def json(self, endpoint):
                if endpoint == "transfer/info":
                    return {"connection_status": "connected"}
                return {"server_state": {"dht_nodes": 42, "use_alt_speed_limits": False}}

        report = qbt.preflight(Client())
        self.assertTrue(report["ready"])
        self.assertEqual(report["connection"], "connected")
        self.assertEqual(report["dht_nodes"], 42)

    def test_add_candidate_can_send_validated_torrent_bytes(self):
        raw_torrent, info_hash = torrent_fixture()
        calls = []

        class Client:
            def request(self, endpoint, fields=None, multipart_body=False):
                calls.append((endpoint, fields, multipart_body))
                return b"Ok."

        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                result = qbt.add_candidate(
                    Client(), info_hash=info_hash, kind="movie", rename="Example (2024)",
                    torrent_data=raw_torrent,
                )
        self.assertEqual(result["source_type"], "torrent")
        self.assertIn("torrents", calls[0][1])
        self.assertNotIn("urls", calls[0][1])
