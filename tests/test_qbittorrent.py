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

    def test_skill_version_is_valid_semver(self):
        version = skill_version.read_version()
        self.assertIsNotNone(skill_version.SEMVER.fullmatch(version))
        self.assertEqual(skill_version.label(version), f"Movies Nerd v{version}")

    def test_payload_downloads_only_main_video_and_skips_companions(self):
        files = [
            {"index": 0, "name": "Movie/Movie.mkv", "size": 8_000_000_000, "priority": 1},
            {"index": 1, "name": "Movie/Featurettes/Interview.mkv", "size": 1_000_000_000, "priority": 1},
            {"index": 2, "name": "Movie/Movie.en.srt", "size": 50_000, "priority": 1},
            {"index": 3, "name": "Movie/setup.exe", "size": 40_000, "priority": 1},
        ]
        result = qbt.classify_files(files)
        self.assertEqual(result["main_feature"]["index"], 0)
        self.assertEqual(result["keep_indices"], [0])
        self.assertIn(1, result["skip_indices"])
        self.assertIn(2, result["skip_indices"])
        self.assertIn(3, result["skip_indices"])
        self.assertEqual(result["unsafe"], [])
        self.assertEqual(result["selected_size"], 8_000_000_000)

    def test_metadata_gate_skips_suspicious_files_but_rejects_spoofing(self):
        files = [
            {"index": 0, "name": "Movie/Movie.mkv", "size": 1_000_000_000},
            {"index": 1, "name": "Movie/setup.exe.mkv", "size": 1_000_000},
            {"index": 2, "name": "Movie/\u202espoof.mkv", "size": 1_000_000},
        ]
        result = qbt.classify_files(files)
        self.assertEqual(result["keep_indices"], [0])
        self.assertIn(1, result["skip_indices"])
        self.assertEqual([item["index"] for item in result["unsafe"]], [2])
        self.assertTrue(any("spoofing" in reason for reason in result["unsafe"][0]["unsafe_reasons"]))

    def test_metadata_gate_rejects_traversal_even_for_skipped_file(self):
        files = [
            {"index": 0, "name": "Movie/Movie.mkv", "size": 1_000_000_000},
            {"index": 1, "name": "../setup.exe", "size": 1_000_000},
        ]
        result = qbt.classify_files(files)
        self.assertEqual([item["index"] for item in result["unsafe"]], [1])
        self.assertTrue(any("traversing" in reason for reason in result["unsafe"][0]["hard_reasons"]))

    def test_metadata_gate_rejects_malformed_and_colliding_records(self):
        files = [
            {"index": 0, "name": "Movie/Film.mkv", "size": 1_000_000_000},
            {"index": 1, "name": "movie/FILM.MKV", "size": 2_000_000},
            {"index": "broken", "name": "Movie/Subs.en.srt", "size": 50_000, "priority": "bad"},
            "not-a-record",
        ]
        result = qbt.classify_files(files)
        self.assertEqual(len(result["unsafe"]), 3)
        self.assertTrue(any(
            "platform-colliding" in reason
            for reason in result["unsafe"][0]["unsafe_reasons"]
        ))
        self.assertTrue(any(
            "invalid payload file index" in reason
            for reason in result["unsafe"][1]["unsafe_reasons"]
        ))
        self.assertTrue(any(
            "invalid torrent metadata record" in reason
            for reason in result["unsafe"][2]["unsafe_reasons"]
        ))

    def test_series_keeps_all_episodes(self):
        files = [
            {"index": 0, "name": "Show.S01E01.mkv", "size": 1_000_000_000},
            {"index": 1, "name": "Show.S01E02.mkv", "size": 1_100_000_000},
            {"index": 2, "name": "extras/trailer.mkv", "size": 100_000_000},
        ]
        result = qbt.classify_files(files, series=True)
        self.assertEqual(result["keep_indices"], [0, 1])
        self.assertEqual(result["skip_indices"], [2])

    def test_closed_qbittorrent_is_opened_and_retried(self):
        class OfflineClient:
            def request(self, _endpoint):
                raise qbt.QbtUnavailable("not ready")

        class ReadyClient:
            def request(self, _endpoint):
                return b"5.0.0"

        with (
            patch.object(qbt, "client_from_env", side_effect=[OfflineClient(), ReadyClient()]),
            patch.object(qbt, "launch_qbittorrent", return_value=True) as launch,
        ):
            client = qbt.connected_client(wait_seconds=0)
        self.assertIsInstance(client, ReadyClient)
        launch.assert_called_once_with()

    def test_unavailable_qbittorrent_error_is_nontechnical(self):
        class OfflineClient:
            def request(self, _endpoint):
                raise qbt.QbtUnavailable("not ready")

        with (
            patch.object(qbt, "client_from_env", return_value=OfflineClient()),
            patch.object(qbt, "launch_qbittorrent", return_value=False),
        ):
            with self.assertRaisesRegex(qbt.QbtUnavailable, "app isn't open") as caught:
                qbt.connected_client(wait_seconds=0)
        message = str(caught.exception)
        self.assertNotIn("127.0.0.1", message)
        self.assertNotIn("port", message.lower())

    def test_local_access_denial_is_not_misreported_as_closed(self):
        denied = __import__("urllib.error").error.URLError(PermissionError(1, "Operation not permitted"))
        self.assertTrue(qbt.access_denied(denied))
        self.assertFalse(qbt.access_denied(ConnectionRefusedError(61, "Connection refused")))

    def test_macos_launcher_opens_qbittorrent_in_background(self):
        completed = __import__("subprocess").CompletedProcess([], 0)
        with (
            patch.object(qbt.platform, "system", return_value="Darwin"),
            patch.object(qbt.subprocess, "run", return_value=completed) as run,
        ):
            self.assertTrue(qbt.launch_qbittorrent())
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-g", "-a", "qBittorrent"])

    def test_torrent_isolated_in_its_own_transfer_directory(self):
        class Client:
            def __init__(self):
                self.fields = None

            def request(self, _endpoint, fields, multipart_body=False):
                self.fields = fields
                self.multipart_body = multipart_body
                return b"Ok."

        with tempfile.TemporaryDirectory() as raw:
            movie_stage = Path(raw) / "Movies" / ".incoming" / "Movies Nerd"
            series_stage = Path(raw) / "Series" / ".incoming" / "Movies Nerd"
            client = Client()
            args = __import__("argparse").Namespace(
                magnet="magnet:?xt=urn:btih:" + "a" * 40,
                kind="movie",
                rename="Example (2024)",
                commit=True,
            )
            with patch.object(qbt, "staging_roots", return_value=(movie_stage, series_stage)):
                result = qbt.command_add(client, args)
        self.assertTrue(result["staging"].endswith("/transfers/" + "a" * 40))
        self.assertEqual(client.fields["savepath"], result["staging"])

    def test_cleanup_refuses_unowned_or_nonisolated_torrent(self):
        with tempfile.TemporaryDirectory() as raw:
            stage = Path(raw) / "Movies" / ".incoming" / "Movies Nerd"
            info_hash = "a" * 40
            with patch.object(qbt, "staging_roots", return_value=(stage, Path(raw) / "Series")):
                with self.assertRaisesRegex(qbt.QbtError, "not owned"):
                    qbt.checked_movies_nerd_transfer({
                        "tags": "movie", "save_path": str(stage / "transfers" / info_hash),
                    }, info_hash)
                with self.assertRaisesRegex(qbt.QbtError, "exact"):
                    qbt.checked_movies_nerd_transfer({
                        "tags": "movies-nerd", "save_path": str(stage),
                    }, info_hash)

    def test_routine_environment_check_hides_connection_details(self):
        with patch.object(check_environment.socket, "create_connection", side_effect=OSError("refused")):
            status = check_environment.qbt_endpoint()
        self.assertEqual(status["message"], "qBittorrent app isn't open")
        self.assertNotIn("url", status)
        self.assertNotIn("error", status)
