from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import qbittorrent_api as qbt
import clean_clutter
import monitor_download
import rank_releases
import refresh_checksums
import write_nfo


class QbittorrentSafetyTests(unittest.TestCase):
    def test_rejects_non_loopback_api(self):
        with self.assertRaises(qbt.QbtError):
            qbt.checked_base_url("http://192.168.1.10:8080")

    def test_rejects_embedded_credentials(self):
        with self.assertRaises(qbt.QbtError):
            qbt.checked_base_url("http://name:secret@127.0.0.1:8080")

    def test_magnet_hex_and_base32(self):
        raw = bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
        encoded = base64.b32encode(raw).decode()
        expected = raw.hex()
        self.assertEqual(qbt.magnet_hash(f"magnet:?xt=urn:btih:{expected}"), expected)
        self.assertEqual(qbt.magnet_hash(f"magnet:?xt=urn:btih:{encoded}"), expected)

    def test_payload_omits_extras_and_rejects_executable(self):
        files = [
            {"index": 0, "name": "Movie/Movie.mkv", "size": 8_000_000_000, "priority": 1},
            {"index": 1, "name": "Movie/Featurettes/Interview.mkv", "size": 1_000_000_000, "priority": 1},
            {"index": 2, "name": "Movie/Movie.en.srt", "size": 50_000, "priority": 1},
            {"index": 3, "name": "Movie/setup.exe", "size": 40_000, "priority": 1},
        ]
        result = qbt.classify_files(files)
        self.assertEqual(result["main_feature"]["index"], 0)
        self.assertIn(1, result["skip_indices"])
        self.assertEqual(result["unsafe"][0]["index"], 3)

    def test_series_keeps_all_episodes(self):
        files = [
            {"index": 0, "name": "Show.S01E01.mkv", "size": 1_000_000_000},
            {"index": 1, "name": "Show.S01E02.mkv", "size": 1_100_000_000},
            {"index": 2, "name": "extras/trailer.mkv", "size": 100_000_000},
        ]
        result = qbt.classify_files(files, series=True)
        self.assertEqual(result["keep_indices"], [0, 1])
        self.assertEqual(result["skip_indices"], [2])


class PolicyTests(unittest.TestCase):
    def test_rank_prefers_eligible_4k_then_1080p(self):
        candidates = [
            {"title": "Film 1080p x264", "size": "7 GiB", "seeders": 100},
            {"title": "Film 2160p HEVC", "size": "14 GiB", "seeders": 20},
            {"title": "Film 2160p HEVC", "size": "16 GiB", "seeders": 300},
        ]
        ranked = [rank_releases.normalize(item, 15 * qbt.GIB) for item in candidates]
        ranked.sort(key=lambda item: (item["eligible"], item["score"]), reverse=True)
        self.assertEqual(ranked[0]["resolution"], "2160p")
        self.assertFalse(ranked[-1]["eligible"])

    def test_nfo_escapes_untrusted_text(self):
        payload = write_nfo.render("movie", {"title": "A & B <C>", "year": 2024})
        root = ET.fromstring(payload)
        self.assertEqual(root.findtext("title"), "A & B <C>")
        self.assertIn(b"&amp;", payload)
        self.assertIn(b"&lt;C&gt;", payload)

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

    def test_checksum_excludes_staging_and_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "Film.mkv").write_bytes(b"film")
            (root / refresh_checksums.MANIFEST).write_text("old", encoding="utf-8")
            (root / ".incoming").mkdir()
            (root / ".incoming" / "partial.mkv").write_bytes(b"partial")
            self.assertEqual([name for _digest, name in refresh_checksums.entries(root)], ["Film.mkv"])

    def test_clutter_finder_detects_portuguese_and_apple_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".DS_Store").write_bytes(b"x")
            (root / "Film.pt.srt").write_text("subtitle", encoding="utf-8")
            (root / "Film.en.srt").write_text("subtitle", encoding="utf-8")
            names = [path.name for path in clean_clutter.targets(root)]
            self.assertEqual(names, [".DS_Store", "Film.pt.srt"])


if __name__ == "__main__":
    unittest.main()
