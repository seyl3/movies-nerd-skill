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


class MonitoringTests(unittest.TestCase):
    def test_monitor_requests_different_source_after_stall(self):
        now = int(__import__("time").time())
        report = monitor_download.assess({
            "hash": "0" * 40,
            "name": "Example",
            "state": "stalledDL",
            "progress": 0.4,
            "dlspeed": 0,
            "last_activity": now - 1800,
            "num_seeds": 0,
            "num_leechs": 0,
            "num_complete": 0,
            "num_incomplete": 0,
        }, threshold=1200, source="source.example")
        self.assertTrue(report["stalled"])
        self.assertEqual(report["failover"]["exclude_source"], "source.example")

    def test_monitor_marks_finished_transfer_complete(self):
        report = monitor_download.assess({
            "hash": "0" * 40,
            "name": "Example",
            "state": "uploading",
            "progress": 1.0,
            "dlspeed": 0,
        }, threshold=1200, source="source.example")
        self.assertTrue(report["complete"])
        self.assertFalse(report["stalled"])

    def test_monitor_window_exit_requires_continuation(self):
        current = {
            "hash": "0" * 40,
            "name": "Example",
            "state": "downloading",
            "progress": 0.5,
            "dlspeed": 1_000_000,
        }
        argv = [
            "monitor_download.py", "--hash", "0" * 40,
            "--source", "source.example", "--watch-minutes", "0",
        ]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.object(monitor_download, "connected_client", return_value=object()),
            patch.object(monitor_download, "sync_torrent", return_value=(1, current)),
            redirect_stdout(output),
        ):
            result = monitor_download.main()
        report = json.loads(output.getvalue())
        self.assertEqual(result, monitor_download.CONTINUE_MONITORING)
        self.assertEqual(report["monitoring"], "continue")
        self.assertIn("continue", report["next"])

    def test_monitor_merges_incremental_qbittorrent_updates(self):
        class Client:
            def __init__(self):
                self.responses = [
                    {
                        "rid": 1,
                        "full_update": True,
                        "torrents": {
                            "a" * 40: {
                                "hash": "a" * 40,
                                "name": "Example",
                                "state": "downloading",
                                "progress": 0.2,
                                "dlspeed": 1_000_000,
                                "num_seeds": 10,
                            },
                        },
                    },
                    {
                        "rid": 2,
                        "full_update": False,
                        "torrents": {"a" * 40: {"progress": 0.4, "dlspeed": 2_000_000}},
                    },
                ]

            def json(self, path):
                return self.responses.pop(0)

        client = Client()
        rid, current = monitor_download.sync_torrent(client, "a" * 40)
        rid, current = monitor_download.sync_torrent(client, "a" * 40, rid, current)
        self.assertEqual(rid, 2)
        self.assertEqual(current["progress"], 0.4)
        self.assertEqual(current["state"], "downloading")
        self.assertEqual(current["num_seeds"], 10)

    def test_monitor_uses_fast_polling_when_transfer_is_inactive(self):
        self.assertEqual(
            monitor_download.next_poll_interval({"download_speed": 0, "state": "stalledDL"}, 60),
            2,
        )
        self.assertEqual(
            monitor_download.next_poll_interval({"download_speed": 1, "state": "downloading"}, 60),
            60,
        )

    def test_monitor_uses_prepared_standby_for_slow_transfer(self):
        samples = [
            monitor_download.Sample(0, 0, 100_000, 0.1, "downloading", 1, 1.0),
            monitor_download.Sample(90, 1_000_000, 100_000, 0.11, "downloading", 1, 1.0),
        ]
        report = monitor_download.assess_samples(
            samples, "source", standby_ready=True,
            no_progress_seconds=60, low_speed_seconds=90,
            low_speed_bps=200_000,
        )
        self.assertTrue(report["stalled"])
        self.assertTrue(report["failover"]["standby_ready"])
