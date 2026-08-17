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


class SearchTests(unittest.TestCase):
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
            patch.object(search_releases, "search_magnetz", return_value=[]),
            patch.object(search_releases, "search_yts", return_value=[]),
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
