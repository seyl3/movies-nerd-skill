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


class PolicyTests(unittest.TestCase):
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

    def test_rank_penalizes_bloated_encode_for_same_quality(self):
        candidates = [
            {"title": "Film 2024 1080p x265 WEB-DL", "size": "5 GiB", "seeders": 100},
            {"title": "Film 2024 1080p x265 WEB-DL", "size": "2.5 GiB", "seeders": 60},
        ]
        ranked = [rank_releases.normalize(item, 15 * qbt.GIB, 90) for item in candidates]
        ranked.sort(key=lambda item: (item["eligible"], item["score"]), reverse=True)
        self.assertEqual(ranked[0]["size"], "2.50 GiB")
        self.assertEqual(ranked[0]["size_efficiency"]["rating"], "balanced")
        self.assertEqual(ranked[1]["size_efficiency"]["rating"], "bloated")
        self.assertTrue(any("large for its runtime" in warning for warning in ranked[1]["warnings"]))

    def test_knaben_results_are_normalized_and_magnets_sanitized(self):
        payload = {
            "hits": [{
                "title": "Example 2024 1080p x265",
                "bytes": 2_500_000_000,
                "seeders": 40,
                "peers": 5,
                "tracker": "1337x",
                "hash": "a" * 40,
                "magnetUrl": "magnet:?xt=urn:btih:" + "a" * 40 + "&ws=file:///tmp/bad",
            }]
        }
        result = search_releases.normalize_knaben(payload)[0]
        self.assertEqual(result["source"], "1337x")
        self.assertEqual(result["leechers"], 5)
        self.assertNotIn("ws=", result["magnet"])
        self.assertEqual(search_releases.magnet_hash(result["magnet"]), "a" * 40)

    def test_apibay_results_receive_only_fixed_trackers(self):
        payload = [{
            "name": "Example 2024 2160p HEVC",
            "size": "12000000000",
            "seeders": "25",
            "leechers": "3",
            "info_hash": "b" * 40,
        }]
        result = search_releases.normalize_apibay(payload)[0]
        self.assertEqual(result["provider"], "APIBay")
        self.assertEqual(result["seeders"], 25)
        self.assertIn("&tr=", result["magnet"])
        self.assertNotIn("file%3A", result["magnet"])

    def test_yts_and_magnetz_api_results_are_normalized(self):
        yts = search_releases.normalize_yts({
            "status": "ok",
            "data": {"movies": [{
                "title": "Example", "year": 2024,
                "torrents": [{
                    "hash": "d" * 40, "quality": "1080p", "type": "bluray",
                    "video_codec": "x265", "size_bytes": 2_000_000_000,
                    "seeds": 9, "peers": 2,
                }],
            }]},
        })[0]
        magnetz = search_releases.normalize_magnetz({
            "data": [{
                "name": "Example 2024 1080p x265", "info_hash": "e" * 40,
                "size": 1_900_000_000, "seeders": 7, "leechers": 1,
            }],
        })[0]
        self.assertEqual(yts["provider"], "YTS API")
        self.assertEqual(yts["seeders"], 9)
        self.assertEqual(magnetz["provider"], "Magnetz API")
        self.assertEqual(magnetz["seeders"], 7)

    def test_candidate_race_keeps_same_quality_and_size_ceiling(self):
        candidates = [
            {
                "title": "Example 2024 1080p x265 primary", "size": 2_000_000_000,
                "seeders": 30, "source": "one",
                "magnet": search_releases.minimal_magnet("1" * 40, "Primary"),
            },
            {
                "title": "Example 2024 1080p x265 smaller", "size": 1_800_000_000,
                "seeders": 20, "source": "two",
                "magnet": search_releases.minimal_magnet("2" * 40, "Smaller"),
            },
            {
                "title": "Example 2024 720p x264", "size": 1_000_000_000,
                "seeders": 50, "source": "three",
                "magnet": search_releases.minimal_magnet("3" * 40, "Lower quality"),
            },
            {
                "title": "Example 2024 1080p x265 larger", "size": 3_000_000_000,
                "seeders": 10, "source": "four",
                "magnet": search_releases.minimal_magnet("4" * 40, "Larger"),
            },
        ]
        selection = search_releases.release_selection(
            candidates, "Example", 2024, 15 * qbt.GIB, 90,
        )
        self.assertEqual(len(selection["candidates"]), 2)
        self.assertTrue(all(item["resolution"] == "1080p" for item in selection["candidates"]))
        self.assertTrue(all(
            item["size_bytes"] <= selection["primary"]["size_bytes"]
            for item in selection["candidates"]
        ))

    def test_hidden_race_starts_one_winner_and_removes_loser(self):
        candidates = [
            {
                "title": "Example 2024 1080p x265", "source": "one",
                "provider": "one", "size_bytes": 2_000_000_000,
                "size": "1.86 GiB", "resolution": "1080p", "seeders": 10,
                "leechers": 1, "score": 100, "warnings": [],
                "magnet": search_releases.minimal_magnet("a" * 40, "One"),
            },
            {
                "title": "Example 2024 1080p x265", "source": "two",
                "provider": "two", "size_bytes": 1_900_000_000,
                "size": "1.77 GiB", "resolution": "1080p", "seeders": 8,
                "leechers": 1, "score": 200, "warnings": [],
                "magnet": search_releases.minimal_magnet("b" * 40, "Two"),
            },
        ]

        first = race_candidates.Probe(candidates[0], "a" * 40, 1.0, "magnet")
        first.bytes_delta = 1_000_000
        first.speeds = [100_000, 120_000]
        first.availability = 1.0
        first.peers = 2
        second = race_candidates.Probe(candidates[1], "b" * 40, 0.5, "torrent")
        second.bytes_delta = 8_000_000
        second.speeds = [800_000, 900_000]
        second.availability = 2.0
        second.peers = 8
        removed = []
        with (
            patch.object(race_candidates, "prepare_wave", return_value=([first, second], [])),
            patch.object(race_candidates, "probe_wave"),
            patch.object(race_candidates, "record_outcome"),
            patch.object(race_candidates, "remove_and_verify", side_effect=lambda _client, value: removed.append(value)),
            patch.object(race_candidates, "command_start", return_value={"started": True, "hash": "b" * 40}),
        ):
            winner, result = race_candidates.race(object(), candidates, "movie", "Example (2024)", 15, 5)
        self.assertEqual(winner["source"], "two")
        self.assertEqual(removed, ["a" * 40])
        self.assertIsNone(result["standby_hash"])
        self.assertEqual(result["comparison"]["kept_hash"], "b" * 40)
        self.assertEqual(result["comparison"]["healthy_candidates"], 2)
        self.assertTrue(result["started"])

    def test_api_search_deduplicates_by_info_hash(self):
        first = {
            "title": "Example 1080p x265", "size": 2_000_000_000,
            "seeders": 10, "magnet": search_releases.minimal_magnet("c" * 40, "Example"),
        }
        second = {**first, "seeders": 50, "source": "faster"}
        result = search_releases.deduplicate([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["seeders"], 50)

    def test_exact_title_match_requires_requested_year(self):
        self.assertTrue(search_releases.matches_requested_title(
            "Amelie 2001 1080p BluRay x265", "Amélie", 2001,
        ))
        self.assertFalse(search_releases.matches_requested_title(
            "Amelie 2014 1080p BluRay x265", "Amélie", 2001,
        ))

    def test_fast_api_success_skips_qbittorrent_search(self):
        candidates = [
            {
                "title": f"Example 2024 1080p x265 release {index}",
                "size": 2_000_000_000,
                "seeders": 20,
                "source": f"source-{index}",
                "magnet": search_releases.minimal_magnet(str(index) * 40, "Example"),
            }
            for index in range(1, 4)
        ]
        with (
            patch.object(search_releases, "search_knaben", return_value=candidates),
            patch.object(search_releases, "search_apibay", return_value=[]),
            patch.object(search_releases, "search_qbt") as qbt_search,
        ):
            results, reports, early = search_releases.search_all(
                "Example 2024", 5, False, True,
                title="Example", year=2024, max_bytes=15 * qbt.GIB,
                runtime_minutes=90,
            )
        self.assertEqual(len(results), 3)
        self.assertTrue(early)
        self.assertEqual(
            reports["qbt_torznab"]["skipped"],
            "enough exact healthy API results with a different-source backup",
        )
        qbt_search.assert_not_called()

    def test_qbittorrent_search_runs_when_fast_results_are_insufficient(self):
        one = {
            "title": "Example 2024 1080p x265",
            "size": 2_000_000_000,
            "seeders": 20,
            "magnet": search_releases.minimal_magnet("1" * 40, "Example"),
        }
        with (
            patch.object(search_releases, "search_knaben", return_value=[one]),
            patch.object(search_releases, "search_apibay", return_value=[]),
            patch.object(search_releases, "search_qbt", return_value=[]) as qbt_search,
        ):
            _, _, early = search_releases.search_all(
                "Example 2024", 5, False, True,
                title="Example", year=2024, max_bytes=15 * qbt.GIB,
                runtime_minutes=90,
            )
        self.assertFalse(early)
        qbt_search.assert_called_once()

    def test_search_prepares_backup_from_a_different_source(self):
        candidates = [
            {
                "title": "Example 2024 2160p HEVC",
                "size": 10_000_000_000,
                "seeders": 30,
                "source": "1337x",
                "provider": "Knaben API",
                "magnet": search_releases.minimal_magnet("a" * 40, "Primary"),
            },
            {
                "title": "Example 2024 2160p HEVC alternate",
                "size": 11_000_000_000,
                "seeders": 20,
                "source": "1337x",
                "provider": "Knaben API",
                "magnet": search_releases.minimal_magnet("b" * 40, "Same source"),
            },
            {
                "title": "Example 2024 1080p x265",
                "size": 2_500_000_000,
                "seeders": 15,
                "source": "The Pirate Bay",
                "provider": "APIBay",
                "magnet": search_releases.minimal_magnet("c" * 40, "Backup"),
            },
        ]
        selection = search_releases.release_selection(
            candidates, "Example", 2024, 15 * qbt.GIB, 90,
        )
        self.assertEqual(
            {selection["primary"]["source"], selection["backup"]["source"]},
            {"1337x", "The Pirate Bay"},
        )
        self.assertNotEqual(selection["primary"]["source"], selection["backup"]["source"])
        self.assertNotEqual(
            search_releases.magnet_hash(selection["primary"]["magnet"]),
            search_releases.magnet_hash(selection["backup"]["magnet"]),
        )

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

    def test_subtitle_provider_uses_no_key_service_without_prompting(self):
        result = subtitle_provider.plan(
            "Example", 2024, "Example.2024.1080p", ["en", "fr"], {},
        )
        self.assertEqual(result["action"], "use-stremio-opensubtitles")
        self.assertEqual(result["provider"], "OpenSubtitles v3 for Stremio")
        self.assertFalse(result["requires_api_key"])
        self.assertNotIn("question", result)

    def test_subtitle_provider_uses_but_never_outputs_key(self):
        secret = "not-for-output"
        result = subtitle_provider.plan(
            "Example", 2024, None, ["en"], {"OPENSUBTITLES_API_KEY": secret},
        )
        self.assertEqual(result["action"], "use-opensubtitles-api")
        self.assertNotIn(secret, json.dumps(result))

    def test_no_key_subtitle_candidates_filter_language_and_hosts(self):
        result = {
            "subtitles": [
                {
                    "id": "101", "lang": "eng", "SubEncoding": "UTF-8",
                    "url": "https://subs5.strem.io/en/download/subencoding-stremio-utf8/src-api/file/501",
                },
                {
                    "id": "102", "lang": "fre", "SubEncoding": "CP1252",
                    "url": "https://subs2.strem.io/en/download/subencoding-stremio-utf8/src-api/file/502?senc=cp1252",
                },
                {
                    "id": "103", "lang": "eng", "SubEncoding": "UTF-8",
                    "url": "https://ads.example/en/download/subencoding-stremio-utf8/src-api/file/503",
                },
                {
                    "id": "104", "lang": "por", "SubEncoding": "UTF-8",
                    "url": "https://subs5.strem.io/en/download/subencoding-stremio-utf8/src-api/file/504",
                },
            ],
        }
        found = stremio_subtitles.candidates(result, ["en", "fr"], 10)
        self.assertEqual([item["subtitle_id"] for item in found], ["101", "102"])
        self.assertEqual([item["language"] for item in found], ["en", "fr"])
        self.assertTrue(all("url" not in item for item in found))

    def test_no_key_subtitle_service_rejects_unapproved_download_urls(self):
        for url in (
            "http://subs5.strem.io/en/download/subencoding-stremio-utf8/src-api/file/1",
            "https://strem.io/en/download/subencoding-stremio-utf8/src-api/file/1",
            "https://subs5.strem.io/other/file/1",
            "https://subs5.strem.io/en/download/subencoding-stremio-utf8/src-api/file/1?next=https://bad.example",
        ):
            with self.assertRaises(stremio_subtitles.StremioSubtitleError):
                stremio_subtitles.checked_download_url(url)

    def test_no_key_subtitle_series_content_id_is_episode_specific(self):
        self.assertEqual(
            stremio_subtitles.content_id("series", "tt1234567", 2, 4),
            "tt1234567:2:4",
        )
        with self.assertRaises(stremio_subtitles.StremioSubtitleError):
            stremio_subtitles.content_id("series", "tt1234567", None, None)

    def test_no_key_subtitle_service_uses_system_trust_fallback(self):
        payload = json.dumps({"subtitles": []}).encode("utf-8")
        verification_error = ssl.SSLCertVerificationError(1, "untrusted local CA")
        url_error = __import__("urllib.error").error.URLError(verification_error)
        with (
            patch.object(stremio_subtitles, "build_opener") as opener,
            patch.object(stremio_subtitles, "system_curl", return_value=payload) as curl,
        ):
            opener.return_value.open.side_effect = url_error
            result = stremio_subtitles.service_json("movie", "tt1234567")
        self.assertEqual(result, {"subtitles": []})
        self.assertEqual(curl.call_args.args[1:], (stremio_subtitles.MAX_JSON, 20, "application/json"))

    def test_content_gate_rejects_renamed_executable_and_archive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            disguised_video = root / "Movie.mkv"
            disguised_video.write_bytes(b"MZ" + b"x" * 128)
            disguised_image = root / "poster.png"
            disguised_image.write_bytes(b"PK\x03\x04" + b"x" * 128)
            video_reasons = payload_safety.content_reasons(disguised_video, ".mkv")
            image_reasons = payload_safety.content_reasons(disguised_image, ".png")
            report = select_payload.scan_payload(root)
            self.assertTrue(any("Windows executable" in reason for reason in video_reasons))
            self.assertTrue(any("ZIP" in reason for reason in image_reasons))
            self.assertFalse(report["safe_to_continue"])
            self.assertEqual(len(report["hazards"]), 2)

    def test_content_gate_salvages_verified_video_and_leaves_bad_companions(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            movie_stage = base / "Movies" / ".incoming" / "Movies Nerd"
            series_stage = base / "Series" / ".incoming" / "Movies Nerd"
            source = movie_stage / "transfers" / ("a" * 40)
            source.mkdir(parents=True)
            video = source / "Example.mkv"
            video.write_bytes(b"verified fixture")
            (source / "bad.nfo").write_bytes(b"bad\x00data")

            def fake_probe(path):
                return {
                    "schema": media_probe.SCHEMA,
                    "media": str(path.resolve()),
                    "snapshot": media_probe.snapshot(path),
                    "ffprobe": {"streams": [], "format": {}},
                    "summary": {
                        "valid_media": True, "duration_seconds": 5400.0,
                        "width": 1920, "height": 1080, "video_codec": "h264",
                        "stream_count": 2, "chapter_count": 0,
                    },
                }

            clean = movie_stage / "clean" / ("a" * 40)
            with (
                patch.object(select_payload, "probe_media", side_effect=fake_probe),
                patch.object(select_payload, "staging_roots", return_value=(movie_stage, series_stage)),
            ):
                report = select_payload.scan_payload(source)
                result = select_payload.extract_selected(source.resolve(), clean, report)
            self.assertTrue(report["safe_to_extract_selected"])
            self.assertFalse(report["safe_to_continue"])
            self.assertTrue(report["cleanup_required"])
            self.assertTrue((clean / "Example.mkv").is_file())
            self.assertTrue((source / "bad.nfo").is_file())
            self.assertFalse((source / "Example.mkv").exists())
            self.assertTrue(result["verification"]["safe_to_extract_selected"])

    def test_saved_media_probe_is_reused_for_subtitle_coverage(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            media = root / "Example.mkv"
            media.write_bytes(b"safe fixture")
            info = media_probe.clean_probe({
                "streams": [
                    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
                    {"index": 1, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng", "title": "English"}},
                ],
                "chapters": [],
                "format": {"format_name": "matroska,webm", "duration": "5400.0", "size": "12"},
            })
            report = {
                "schema": media_probe.SCHEMA,
                "media": str(media.resolve()),
                "snapshot": media_probe.snapshot(media),
                "ffprobe": info,
                "summary": media_probe.summary(info),
            }
            gate = root / "gate.json"
            gate.write_text(json.dumps({"selected": [{"path": media.name, "probe": report}]}), encoding="utf-8")
            loaded = media_probe.load_report(gate, media)
            embedded = check_subtitles.embedded_languages(media, loaded)
            self.assertEqual(embedded[0]["language"], "eng")
            self.assertEqual(loaded["summary"]["duration_seconds"], 5400.0)

    def test_saved_media_probe_rejects_changed_media(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            media = root / "Example.mkv"
            media.write_bytes(b"first")
            info = media_probe.clean_probe({
                "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}],
                "format": {"format_name": "matroska,webm", "duration": "90"},
            })
            report = {
                "schema": media_probe.SCHEMA,
                "media": str(media.resolve()),
                "snapshot": media_probe.snapshot(media),
                "ffprobe": info,
                "summary": media_probe.summary(info),
            }
            saved = root / "probe.json"
            saved.write_text(json.dumps(report), encoding="utf-8")
            media.write_bytes(b"changed")
            with self.assertRaises(media_probe.ProbeError):
                media_probe.load_report(saved, media)

    def test_valid_srt_and_html_rejection(self):
        cues = "\n\n".join(
            f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},800\nLine {index}."
            for index in range(1, 7)
        ).encode()
        valid = validate_subtitle.validate_bytes(cues, "en", media_duration=7)
        invalid = validate_subtitle.validate_bytes(b"<!doctype html><html>Cloudflare</html>", "fr")
        self.assertTrue(valid["valid"])
        self.assertTrue(valid["counts_as_full_coverage"])
        self.assertFalse(invalid["valid"])

    def test_remux_validation_uses_stream_metadata_and_duration(self):
        info = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
                {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
            ],
            "format": {"duration": "5400.125"},
        }
        self.assertEqual(remux_mkv.stream_signature(info), remux_mkv.stream_signature(info.copy()))
        self.assertEqual(remux_mkv.duration(info), 5400.125)

    def test_mkv_header_plan_avoids_full_remux_for_track_metadata(self):
        info = {
            "streams": [
                {
                    "index": 1, "codec_type": "audio", "codec_name": "aac",
                    "tags": {"language": "en", "title": "Track 1"},
                    "disposition": {"default": 0},
                },
                {
                    "index": 2, "codec_type": "subtitle", "codec_name": "subrip",
                    "tags": {"language": "fr", "title": "French"},
                    "disposition": {"default": 1, "forced": 0, "hearing_impaired": 0},
                },
            ],
            "chapters": [],
            "format": {"format_name": "matroska,webm", "duration": "5400"},
        }
        changes = edit_mkv_headers.change_plan(info, {}, {})
        self.assertEqual([item["selector"] for item in changes], ["track:a1", "track:s1"])
        self.assertEqual(changes[0]["properties"]["language"], "eng")
        self.assertEqual(changes[0]["properties"]["name"], "English")
        self.assertEqual(changes[0]["properties"]["flag-default"], 1)
        self.assertEqual(changes[1]["properties"]["flag-default"], 0)

    def test_compliant_mkv_headers_need_no_edit(self):
        info = {
            "streams": [
                {
                    "index": 1, "codec_type": "audio", "codec_name": "aac",
                    "tags": {"language": "eng", "title": "English"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 2, "codec_type": "subtitle", "codec_name": "subrip",
                    "tags": {"language": "fre", "title": "French"},
                    "disposition": {"default": 0, "forced": 0, "hearing_impaired": 0},
                },
            ],
            "chapters": [],
            "format": {"format_name": "matroska,webm", "duration": "5400"},
        }
        self.assertEqual(edit_mkv_headers.change_plan(info, {}, {}), [])

    def test_opensubtitles_requires_environment_key(self):
        with self.assertRaises(opensubtitles_api.SubtitleApiError):
            opensubtitles_api.api_key({})

    def test_opensubtitles_destination_is_staging_only(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = {
                _common.MOVIES_ROOT_ENV: str(base / "Movies"),
                _common.SERIES_ROOT_ENV: str(base / "Series"),
            }
            output = base / "Movies" / ".incoming" / "Movies Nerd" / "Example.en.srt"
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(opensubtitles_api.checked_destination(str(output), "en"), output.resolve(strict=False))
                with self.assertRaises(opensubtitles_api.SubtitleApiError):
                    opensubtitles_api.checked_destination(str(base / "outside.en.srt"), "en")


if __name__ == "__main__":
    unittest.main()
