from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import job_manifest
import qbittorrent_api as qbt
import search_releases
import torrentio_provider


class TorrentioProviderTests(unittest.TestCase):
    def test_builds_fixed_movie_and_exact_episode_urls(self):
        movie = torrentio_provider.stream_url("tt1234567")
        episode = torrentio_provider.stream_url(
            "tt1234567", series=True, season=2, episode=4,
        )
        self.assertTrue(movie.startswith("https://torrentio.strem.fun/"))
        self.assertTrue(movie.endswith("/stream/movie/tt1234567.json"))
        self.assertTrue(episode.endswith("/stream/series/tt1234567:2:4.json"))
        with self.assertRaises(torrentio_provider.TorrentioError):
            torrentio_provider.stream_url("tt1234567", series=True)
        with self.assertRaises(torrentio_provider.TorrentioError):
            torrentio_provider.stream_url("https://evil.example/tt1234567")

    def test_normalizes_hash_only_stream_and_ignores_provider_trackers(self):
        result = torrentio_provider.normalize_streams({"streams": [{
            "name": "Torrentio\n1080p",
            "title": "Pack.Name\nExample.2024.1080p.x265.mkv\n👤 42 💾 2.35 GB ⚙️ 1337x",
            "infoHash": "a" * 40,
            "fileIdx": 4,
            "sources": ["tracker:https://evil.example/announce"],
            "behaviorHints": {"filename": "../../Example.mkv"},
        }]}, title="Example", year=2024, max_bytes=15 * qbt.GIB)[0]
        self.assertEqual(result["provider"], "Torrentio")
        self.assertEqual(result["source"], "1337x")
        self.assertEqual(result["seeders"], 42)
        self.assertEqual(result["size"], int(2.35 * qbt.GIB))
        self.assertEqual(result["file_index"], 4)
        self.assertEqual(result["info_hash"], "a" * 40)
        self.assertNotIn("evil.example", result["magnet"])
        self.assertNotIn("..", result["title"])

    def test_skips_direct_debrid_invalid_oversize_and_malformed_streams(self):
        payload = {"streams": [
            {"url": "https://debrid.example/file", "infoHash": "a" * 40, "title": "💾 1 GB"},
            {"externalUrl": "https://example.test", "infoHash": "b" * 40, "title": "💾 1 GB"},
            {"infoHash": "invalid", "title": "💾 1 GB"},
            {"infoHash": "c" * 40, "title": "Example 1080p 💾 20 GB"},
            {"infoHash": "d" * 40, "title": "Example 1080p without size"},
            {"infoHash": "e" * 40, "fileIdx": True, "title": "Example 1080p 💾 1 GB"},
            {"infoHash": "f" * 40, "title": "Example \u202e1080p 💾 1 GB"},
        ]}
        self.assertEqual(torrentio_provider.normalize_streams(
            payload, title="Example", year=2024, max_bytes=15 * qbt.GIB,
        ), [])
        with self.assertRaises(torrentio_provider.TorrentioError):
            torrentio_provider.normalize_streams([], title="Example", year=2024, max_bytes=qbt.GIB)

    def test_search_all_queries_torrentio_only_for_exact_movie_identity(self):
        item = {
            "title": "Example (2024) 1080p x265", "size": 2_000_000_000,
            "seeders": 4, "source": "Torrentio", "provider": "Torrentio",
            "magnet": search_releases.minimal_magnet("f" * 40, "Example"),
            "info_hash": "f" * 40, "file_index": 3,
        }
        with (
            patch.object(search_releases, "search_knaben", return_value=[]),
            patch.object(search_releases, "search_apibay", return_value=[]),
            patch.object(search_releases, "search_magnetz", return_value=[]),
            patch.object(search_releases, "search_yts", return_value=[]),
            patch.object(search_releases, "search_torrentio", return_value=[item]) as torrentio,
        ):
            results, reports, _early = search_releases.search_all(
                "Example 2024", 2, False, False, title="Example", year=2024,
                max_bytes=15 * qbt.GIB, runtime_minutes=90, imdb_id="tt1234567",
            )
        torrentio.assert_called_once()
        self.assertEqual(results[0]["file_index"], 3)
        self.assertTrue(reports["torrentio"]["ok"])

        with (
            patch.object(search_releases, "search_knaben", return_value=[]),
            patch.object(search_releases, "search_apibay", return_value=[]),
            patch.object(search_releases, "search_magnetz", return_value=[]),
            patch.object(search_releases, "search_yts", return_value=[]),
            patch.object(search_releases, "search_torrentio") as torrentio,
        ):
            search_releases.search_all(
                "Example 2024", 2, True, False, title="Example", year=2024,
                max_bytes=15 * qbt.GIB, runtime_minutes=90, imdb_id="tt1234567",
            )
        torrentio.assert_not_called()

    def test_dedup_keeps_direct_metadata_and_torrentio_file_index(self):
        base = {
            "title": "Example 2024 1080p", "size": 2_000_000_000,
            "seeders": 5, "source": "source",
            "magnet": search_releases.minimal_magnet("a" * 40, "Example"),
        }
        torrentio = {**base, "provider": "Torrentio", "file_index": 7, "seeders": 50}
        direct = {
            **base, "provider": "YTS API", "torrent_url": "https://yts.mx/torrent/download/" + "a" * 40,
        }
        result = search_releases.deduplicate([torrentio, direct])[0]
        self.assertEqual(result["provider"], "YTS API")
        self.assertEqual(result["file_index"], 7)


class TorrentioFileSelectionTests(unittest.TestCase):
    def test_preferred_safe_movie_file_overrides_largest_pack_video(self):
        files = [
            {"index": 0, "name": "Other.Movie.2023.mkv", "size": 5_000_000_000},
            {"index": 4, "name": "Example.2024.mkv", "size": 2_000_000_000},
            {"index": 5, "name": "Featurettes/Interview.mkv", "size": 500_000_000},
        ]
        result = qbt.classify_files(files, preferred_file_index=4)
        self.assertEqual(result["main_feature"]["index"], 4)
        self.assertEqual(result["keep_indices"], [4])
        self.assertEqual(result["selected_size"], 2_000_000_000)
        self.assertIsNone(result["preferred_file_error"])

    def test_preferred_file_must_be_safe_video_and_manifest_value_is_bounded(self):
        files = [
            {"index": 0, "name": "Example.mkv", "size": 2_000_000_000},
            {"index": 1, "name": "Featurettes/Interview.mkv", "size": 500_000_000},
        ]
        result = qbt.classify_files(files, preferred_file_index=1)
        self.assertIn("missing, unsafe, or an extra", result["preferred_file_error"])
        release = {
            "title": "Example 2024 1080p", "source": "Torrentio", "provider": "Torrentio",
            "size_bytes": 2_000_000_000, "size": "1.86 GiB", "resolution": "1080p",
            "seeders": 4, "leechers": 0, "score": 100, "warnings": [],
            "magnet": search_releases.minimal_magnet("a" * 40, "Example"),
            "file_index": 4,
        }
        self.assertEqual(job_manifest.checked_release(release)["file_index"], 4)
        with self.assertRaises(job_manifest.ManifestError):
            job_manifest.checked_release({**release, "file_index": True})

    def test_start_passes_preferred_index_to_selection(self):
        class Client:
            def request(self, _endpoint, _payload):
                return b""

        args = argparse.Namespace(
            hash="a" * 40, commit=True, include_extras=False,
            allow_oversize=False, series=False, preferred_file_index=6,
        )
        with patch.object(
            qbt, "configure_selection", return_value=({"name": "Example"}, {}, 2_000_000_000),
        ) as configure:
            qbt.command_start(Client(), args)
        self.assertEqual(configure.call_args.kwargs["preferred_file_index"], 6)


if __name__ == "__main__":
    unittest.main()
