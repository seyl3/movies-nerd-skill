from __future__ import annotations

import base64
import json
import os
from pathlib import Path
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
import check_subtitles
import edit_mkv_headers
import job_manifest
import media_probe
import monitor_download
import opensubtitles_api
import payload_safety
import rank_releases
import remux_mkv
import search_releases
import select_payload
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

    def test_metadata_gate_rejects_double_extensions_and_bidi_spoofing(self):
        files = [
            {"index": 0, "name": "Movie/Movie.mkv", "size": 1_000_000_000},
            {"index": 1, "name": "Movie/setup.exe.mkv", "size": 1_000_000},
            {"index": 2, "name": "Movie/\u202espoof.mkv", "size": 1_000_000},
        ]
        result = qbt.classify_files(files)
        self.assertEqual([item["index"] for item in result["unsafe"]], [1, 2])
        self.assertIn("inner extension", result["unsafe"][0]["unsafe_reasons"][0])
        self.assertTrue(any("spoofing" in reason for reason in result["unsafe"][1]["unsafe_reasons"]))

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
            self.assertEqual(job_manifest.redacted(loaded)["release"]["magnet"], "<stored>")

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

    def test_apibay_results_are_normalized_without_extra_trackers(self):
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
        self.assertNotIn("&tr=", result["magnet"])

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
        self.assertEqual(reports["qbt_torznab"]["skipped"], "enough exact healthy API results")
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

    def test_clutter_finder_detects_portuguese_and_apple_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".DS_Store").write_bytes(b"x")
            (root / "Film.pt.srt").write_text("subtitle", encoding="utf-8")
            (root / "Film.en.srt").write_text("subtitle", encoding="utf-8")
            names = [path.name for path in clean_clutter.targets(root)]
            self.assertEqual(names, [".DS_Store", "Film.pt.srt"])

    def test_subtitle_provider_asks_once_then_falls_back(self):
        ask = subtitle_provider.plan("Example", 2024, "Example.2024.1080p", ["en", "fr"], False, {})
        fallback = subtitle_provider.plan("Example", 2024, "Example.2024.1080p", ["en", "fr"], True, {})
        self.assertEqual(ask["action"], "ask-user-once")
        self.assertEqual(fallback["action"], "browser-fallback")
        self.assertEqual(fallback["provider"], "Subtitle Cat")

    def test_subtitle_provider_uses_but_never_outputs_key(self):
        secret = "not-for-output"
        result = subtitle_provider.plan("Example", 2024, None, ["en"], False, {"OPENSUBTITLES_API_KEY": secret})
        self.assertEqual(result["action"], "use-opensubtitles-api")
        self.assertNotIn(secret, json.dumps(result))

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
