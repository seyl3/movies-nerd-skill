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
import finalization_queue
import finalize_job
import finish_staging
import job_manifest
import monitor_download
import provider_health
import prepare_job
import run_job
import qbittorrent_api as qbt
import race_candidates
import search_releases
import torrent_metadata


def bencode(value) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            bencode(key) + bencode(value[key]) for key in sorted(value)
        ) + b"e"
    raise TypeError(type(value))


def torrent_fixture(path: list[bytes] | None = None) -> tuple[bytes, str]:
    if path is None:
        info = {
            b"length": 2_000_000_000,
            b"name": b"Example.mkv",
            b"piece length": 262144,
            b"pieces": b"x" * 20,
        }
    else:
        info = {
            b"files": [{b"length": 2_000_000_000, b"path": path}],
            b"name": b"Example",
            b"piece length": 262144,
            b"pieces": b"x" * 20,
        }
    raw_info = bencode(info)
    raw = bencode({b"announce": b"udp://tracker.example/announce", b"info": info})
    return raw, hashlib.sha1(raw_info).hexdigest()


def roots(base: Path) -> dict[str, str]:
    return {
        _common.MOVIES_ROOT_ENV: str(base / "Films"),
        _common.SERIES_ROOT_ENV: str(base / "Series"),
    }


def candidate(info_hash: str, source: str = "source", score: float = 100) -> dict:
    return {
        "title": "Example (2024) 1080p x265",
        "source": source,
        "provider": source,
        "size_bytes": 2_000_000_000,
        "size": "1.86 GiB",
        "resolution": "1080p",
        "seeders": 0,
        "leechers": 0,
        "score": score,
        "warnings": [],
        "magnet": search_releases.minimal_magnet(info_hash, "Example"),
        "info_hash": info_hash,
    }


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
        def slow():
            time.sleep(0.2)
            return []

        started = time.monotonic()
        _results, reports = search_releases.run_providers({"slow": slow}, 0.03)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15)
        self.assertFalse(reports["slow"]["ok"])


class HealthAndStateTests(unittest.TestCase):
    def test_prepare_job_searches_and_records_without_temporary_json(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            items = [candidate("a" * 40), candidate("b" * 40, "backup")]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(prepare_job, "dead_hashes", return_value=set()),
                patch.object(
                    prepare_job, "search_all",
                    return_value=(items, {"yts": {"ok": True, "results": 2, "latency_ms": 25}}, True),
                ),
                patch.object(prepare_job, "record_provider"),
            ):
                result = prepare_job.prepare(
                    title="Example", year=2024, kind="movie",
                    runtime_minutes=100, imdb_id="tt1234567",
                    max_gib=15, timeout=5,
                )
                job_path = Path(result["job"])
                _, job = job_manifest.load_job(job_path)
            self.assertTrue(result["prepared"])
            self.assertEqual(job["identity"]["ids"]["imdb"], "tt1234567")
            self.assertEqual(len(job["candidate_pool"]), 2)
            self.assertFalse(any(job_path.parent.glob("*search*.json")))

    def test_inline_json_and_authoritative_ids_are_supported(self):
        value = job_manifest.read_json('{"cache":{"runtime_minutes":121}}')
        self.assertEqual(value["cache"]["runtime_minutes"], 121)
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            path = job_manifest.create_job(
                "movie", "Example", 2024,
                {"identity": {"ids": {"imdb": "tt1234567"}}},
                environ=env,
            )
            _, job = job_manifest.load_job(path, env)
            self.assertEqual(job["identity"]["ids"]["imdb"], "tt1234567")

    def test_provider_health_is_private_and_dead_hash_expires(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            provider_health.record_provider(
                "movie", "YTS API", ok=True, latency_ms=200, results=4,
                environ=env,
            )
            provider_health.record_hash(
                "movie", "a" * 40, "dead", provider="YTS API", environ=env,
            )
            path = provider_health.cache_path("movie", env)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("a" * 40, provider_health.dead_hashes("movie", env))
            value = provider_health.load("movie", env)
            value["hashes"]["a" * 40]["updated_epoch"] = 1
            self.assertNotIn("a" * 40, provider_health.prune(value, now=provider_health.DEAD_TTL_SECONDS + 2)["hashes"])

    def test_job_manifest_lives_under_state_not_incoming(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            path = job_manifest.create_job("movie", "Example", 2024, environ=env)
            self.assertIn("/.movies-nerd/jobs/", str(path))
            self.assertNotIn("/.incoming/", str(path))
            _, value = job_manifest.load_job(path, env)
            self.assertEqual(value["version"], 2)

    def test_search_manifest_accepts_two_probe_waves(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = roots(base)
            job = job_manifest.create_job("movie", "Example", 2024, environ=env)
            items = [candidate(f"{index:x}" * 40, f"source-{index}", 100 - index) for index in range(1, 7)]
            selection = {
                "primary": items[0], "backup": items[1], "candidates": items,
                "eligible_count": 6,
                "confirmation_envelope": {
                    "quality": "1080p", "max_size_bytes": 2_000_000_000,
                },
            }
            result = base / "search.json"
            result.write_text(json.dumps({
                "request": {"title": "Example", "year": 2024, "kind": "movie"},
                "selection": selection,
            }), encoding="utf-8")
            recorded = job_manifest.record_search(job, result, env)
            self.assertEqual(len(recorded["candidate_pool"]), 6)

    def test_failed_manifest_survives_until_job_trash_is_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            path = job_manifest.create_job("movie", "Example", 2024, environ=env)
            job_manifest.transition_job(path, "failed", reason="test", environ=env)
            _, value = job_manifest.load_job(path, env)
            trash = path.parent.parent / "trash" / value["job_id"]
            trash.mkdir(parents=True)
            with self.assertRaisesRegex(job_manifest.ManifestError, "trash must be cleared"):
                job_manifest.remove_failed_job(path, environ=env)
            self.assertTrue(path.exists())


class RaceAndMonitorV2Tests(unittest.TestCase):
    def test_race_uses_second_wave_after_dead_first_wave(self):
        candidates = [candidate(f"{index:x}" * 40, f"source-{index}") for index in range(1, 5)]
        dead = race_candidates.Probe(candidates[0], "1" * 40, 1.0, "magnet")
        live = race_candidates.Probe(candidates[3], "4" * 40, 1.0, "magnet")
        live.bytes_delta = 4_000_000
        live.speeds = [500_000, 600_000]
        live.availability = 1.0
        live.peers = 2
        with (
            patch.object(race_candidates, "prepare_wave", side_effect=[([dead], []), ([live], [])]) as prepare,
            patch.object(race_candidates, "probe_wave"),
            patch.object(race_candidates, "record_outcome"),
            patch.object(race_candidates, "remove_quietly"),
            patch.object(race_candidates, "command_start", return_value={"started": True}),
        ):
            winner, result = race_candidates.race(object(), candidates, "movie", "Example", 10, 5)
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(winner["source"], "source-4")
        self.assertEqual(result["wave"], 2)

    def test_monitor_waits_through_short_pause(self):
        samples = [
            monitor_download.Sample(0, 1_000, 0, 0.1, "stalledDL", 0, 0),
            monitor_download.Sample(30, 1_000, 0, 0.1, "stalledDL", 0, 0),
        ]
        report = monitor_download.assess_samples(samples, "source", no_progress_seconds=60)
        self.assertFalse(report["stalled"])

    def test_monitor_fails_over_after_sixty_seconds_without_bytes(self):
        samples = [
            monitor_download.Sample(0, 1_000, 0, 0.1, "stalledDL", 0, 0),
            monitor_download.Sample(60, 1_000, 0, 0.1, "stalledDL", 0, 0),
        ]
        report = monitor_download.assess_samples(samples, "source", no_progress_seconds=60)
        self.assertTrue(report["stalled"])
        self.assertTrue(any("downloaded bytes" in reason for reason in report["reasons"]))


class ControllerAndCleanupTests(unittest.TestCase):
    def test_appledouble_hygiene_removes_only_scoped_sidecars(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "job"
            nested = root / "nested"
            nested.mkdir(parents=True)
            keep = nested / "movie.mkv"
            keep.write_bytes(b"video")
            sidecar = nested / "._movie.mkv"
            sidecar.write_bytes(b"AppleDouble")
            removed = _common.clean_appledouble_tree(root)
            self.assertEqual(removed, [str(sidecar)])
            self.assertTrue(keep.exists())
            self.assertFalse(sidecar.exists())

    def test_acquire_help_is_compact(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "acquire.py"), "--help"],
            check=True, text=True, capture_output=True,
        )
        self.assertLess(len(completed.stdout), 2_000)
        self.assertIn("0..86400", completed.stdout)

    def test_slow_transfer_replaces_candidate_without_stopping_controller(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                first = candidate("a" * 40)
                second = candidate("b" * 40, "backup")
                job_manifest.update_job(path, {
                    "state": "downloading", "release": first,
                    "candidate_pool": [first, second],
                    "controller": {
                        "active_hash": "a" * 40,
                        "tried_hashes": ["a" * 40],
                    },
                    "artifacts": {"torrent_hash": "a" * 40},
                })

                class Client:
                    def request(self, *_args, **_kwargs):
                        return b"Ok."

                slow = {
                    "hash": "a" * 40, "state": "downloading", "progress": 0.1,
                    "downloaded": 100, "dlspeed": 1_000, "availability": 1.0,
                }
                complete = {
                    "hash": "b" * 40, "state": "uploading", "progress": 1.0,
                    "downloaded": 2_000_000_000, "dlspeed": 0, "availability": 1.0,
                }
                samples = iter([
                    monitor_download.to_sample(slow, 0),
                    monitor_download.to_sample(slow, 1),
                    monitor_download.to_sample(complete, 2),
                ])
                sync_values = iter([(1, slow), (2, slow), (3, complete)])

                def replace(job_path, _job, _client, _old):
                    job_manifest.transition_job(job_path, "replacement-started")
                    job_manifest.transition_job(
                        job_path, "downloading", torrent_hash="b" * 40,
                        release=second,
                    )
                    updated = job_manifest.update_job(job_path, {
                        "controller": {
                            "active_hash": "b" * 40,
                            "tried_hashes": ["a" * 40, "b" * 40],
                        },
                    })
                    return "b" * 40, updated

                with (
                    patch.object(acquire, "connected_client", return_value=Client()),
                    patch.object(acquire, "preflight", return_value={"ready": True}),
                    patch.object(acquire, "torrent_info", return_value=slow),
                    patch.object(acquire, "enforce_single_transfer", side_effect=lambda _p, j, _c, _h: j),
                    patch.object(acquire, "request_finalization"),
                    patch.object(acquire, "sync_torrent", side_effect=lambda *_args: next(sync_values)),
                    patch.object(acquire, "to_sample", side_effect=lambda _info: next(samples)),
                    patch.object(acquire, "replace_active", side_effect=replace) as failover,
                    patch.object(acquire, "DEFAULT_LOW_SPEED_SECONDS", 1),
                    patch.object(acquire, "DEFAULT_LOW_SPEED_BPS", 256 * 1024),
                    patch.object(acquire.time, "sleep"),
                    redirect_stdout(io.StringIO()),
                ):
                    result = acquire.run(path, poll_seconds=2)
            self.assertTrue(result["downloaded"])
            failover.assert_called_once()

    def test_finalization_requests_real_work_instead_of_claiming_it_started(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {"state": "downloading"})
                plan = finalization_queue.start_all(path)
                self.assertTrue(Path(plan["artifact_root"]).is_dir())
                self.assertTrue(all(item["status"] == "requested" for item in plan["tasks"]))

    def test_foreground_runner_reaches_finalizer_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "downloaded",
                    "enrichment_tasks": {
                        name: {"status": "complete"}
                        for name in finalization_queue.MOVIE_TASKS
                    },
                })
                with (
                    patch.object(acquire, "run", return_value={"downloaded": True}),
                    patch.object(run_job, "finalize", return_value={"ready": True}) as finish,
                ):
                    result = run_job.run(path, artifact_wait_seconds=0)
                _, job = job_manifest.load_job(path)
                lock = path.parent.parent / "locks" / f"{job['job_id']}.lock"
            self.assertTrue(result["ready"])
            self.assertFalse(lock.exists())
            finish.assert_called_once()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_downloaded_movie_finalizes_and_cleans_end_to_end(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job(
                    "movie", "Example", 2024,
                    {"identity": {"ids": {"imdb": "tt1234567"}}},
                )
                job_manifest.update_job(path, {"state": "downloading"})
                _, job = job_manifest.load_job(path)
                finalization_queue.start_all(path)
                root = finalization_queue.artifact_root(job)
                metadata = root / "metadata.json"
                metadata.write_text(json.dumps({
                    "title": "Example", "year": 2024,
                    "directors": ["Director"], "plot": "Test movie.",
                    "uniqueids": {"imdb": "tt1234567"},
                    "default_uniqueid": "imdb",
                    "letterboxd_url": "https://letterboxd.com/film/example/",
                    "senscritique_url": "https://www.senscritique.com/film/example/123",
                    "recommendations": [],
                }), encoding="utf-8")
                poster = root / "poster.png"
                poster.write_bytes(base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z8l8AAAAASUVORK5CYII="
                ))
                subtitle_text = "\n\n".join([
                    "1\n00:00:00,000 --> 00:00:00,300\nOne",
                    "2\n00:00:00,350 --> 00:00:00,650\nTwo",
                    "3\n00:00:00,700 --> 00:00:01,000\nThree",
                    "4\n00:00:01,050 --> 00:00:01,350\nFour",
                    "5\n00:00:01,400 --> 00:00:01,900\nFive",
                ]) + "\n"
                en = root / "example.en.srt"
                fr = root / "example.fr.srt"
                en.write_text(subtitle_text, encoding="utf-8")
                fr.write_text(subtitle_text, encoding="utf-8")
                for task, artifact in (
                    ("metadata", metadata), ("artwork", poster),
                    ("subtitle-en", en), ("subtitle-fr", fr),
                ):
                    finalization_queue.mark(path, task, "complete", artifact=artifact)
                for task in ("destination", "film-links", "recommendations"):
                    finalization_queue.mark(path, task, "complete", note="prepared")

                info_hash = "a" * 40
                transfer = _common.stage_for_kind("movie") / "transfers" / info_hash
                transfer.mkdir(parents=True)
                media = transfer / "Example.mkv"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=size=320x180:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
                    "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(media),
                ], check=True)
                job_manifest.update_job(path, {
                    "state": "downloaded",
                    "controller": {"active_hash": info_hash, "tried_hashes": [info_hash]},
                    "artifacts": {"torrent_hash": info_hash},
                })

                class Client:
                    present = True

                    def json(self, endpoint):
                        if endpoint.startswith("torrents/info"):
                            return [{
                                "hash": info_hash, "save_path": str(transfer),
                                "tags": "movies-nerd,movie",
                            }] if self.present else []
                        raise AssertionError(endpoint)

                    def request(self, endpoint, _fields=None):
                        if endpoint == "torrents/delete":
                            self.present = False
                        return b"Ok."

                client = Client()
                with (
                    patch.object(finalize_job, "connected_client", return_value=client),
                    patch.object(finish_staging, "connected_client", return_value=client),
                ):
                    result = finalize_job.finalize(path)
                destination = Path(result["destination"])
                files = {item.name for item in destination.iterdir()}
            self.assertTrue(result["ready"])
            self.assertTrue(result["cleanup"]["clean"])
            self.assertIn("Example (2024) [480p].mkv", files)
            self.assertIn("Example (2024) [480p].nfo", files)
            self.assertIn("Example (2024).png", files)
            self.assertIn("Example (2024) [480p].en.srt", files)
            self.assertIn("Example (2024) [480p].fr.srt", files)
            self.assertFalse(path.exists())
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

    def test_acquisition_resumes_existing_active_hash(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "downloading",
                    "controller": {"active_hash": "a" * 40},
                    "artifacts": {"torrent_hash": "a" * 40},
                })
                _, job = job_manifest.load_job(path)
                with patch.object(acquire, "torrent_info", return_value={"hash": "a" * 40}):
                    active, resumed = acquire.ensure_active(path, job, object())
        self.assertEqual(active, "a" * 40)
        self.assertEqual(resumed["state"], "downloading")

    def test_controller_runs_existing_transfer_to_downloaded_state(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "downloading",
                    "release": candidate("a" * 40),
                    "controller": {"active_hash": "a" * 40, "tried_hashes": ["a" * 40]},
                    "artifacts": {"torrent_hash": "a" * 40},
                })

                class Client:
                    def request(self, *_args, **_kwargs):
                        return b"Ok."

                complete = {
                    "hash": "a" * 40, "state": "uploading", "progress": 1.0,
                    "downloaded": 2_000_000_000, "dlspeed": 0,
                }
                with (
                    patch.object(acquire, "connected_client", return_value=Client()),
                    patch.object(acquire, "preflight", return_value={"ready": True}),
                    patch.object(acquire, "torrent_info", return_value=complete),
                    patch.object(acquire, "sync_torrent", return_value=(1, complete)),
                    redirect_stdout(io.StringIO()),
                ):
                    result = acquire.run(path, poll_seconds=2)
                _, final_job = job_manifest.load_job(path)
            self.assertTrue(result["downloaded"])
            self.assertEqual(final_job["state"], "downloaded")
            self.assertEqual(final_job["controller"]["phase"], "downloaded")

    def test_controller_activates_prevalidated_standby(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                backup = candidate("b" * 40, "backup")
                job_manifest.update_job(path, {
                    "state": "stalled", "release": candidate("a" * 40),
                    "backup_release": backup,
                    "controller": {
                        "active_hash": "a" * 40, "standby_hash": "b" * 40,
                        "tried_hashes": ["a" * 40, "b" * 40],
                    },
                    "artifacts": {"torrent_hash": "a" * 40},
                })
                _, job = job_manifest.load_job(path)
                with (
                    patch.object(acquire, "torrent_info", return_value={"hash": "b" * 40}),
                    patch.object(acquire, "remove_and_verify"),
                    patch.object(acquire, "command_start", return_value={"started": True}),
                    patch.object(acquire, "record_hash"),
                ):
                    active, updated = acquire.activate_standby(path, job, object(), "a" * 40)
            self.assertEqual(active, "b" * 40)
            self.assertEqual(updated["state"], "downloading")
            self.assertIsNone(updated["controller"]["standby_hash"])

    def test_stale_controller_lock_is_replaced_and_removed(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / ".movies-nerd"
            job_dir = state / "jobs"
            job_dir.mkdir(parents=True)
            job = job_dir / "job.json"
            job.write_text("{}", encoding="utf-8")
            lock = state / "locks" / "abc.lock"
            lock.parent.mkdir()
            lock.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
            with acquire.JobLock(job, "abc"):
                self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())

    def test_finalization_queue_marks_independent_tasks(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {"state": "downloading"})
                plan = finalization_queue.start_all(path)
                self.assertTrue(plan["parallel"])
                self.assertEqual(len(plan["tasks"]), 7)
                updated = finalization_queue.mark(path, "film-links", "complete", note="verified")
                links = next(item for item in updated["tasks"] if item["name"] == "film-links")
                self.assertEqual(links["status"], "complete")

    def test_success_cleanup_keeps_only_provider_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = roots(base)
            movies = Path(env[_common.MOVIES_ROOT_ENV])
            with patch.dict(os.environ, env, clear=False):
                provider_health.record_provider("movie", "YTS API", ok=True, latency_ms=100, results=2)
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {"state": "imported"})
                stage = movies / ".incoming" / "Movies Nerd"
                transfer = stage / "transfers" / ("a" * 40)
                transfer.mkdir(parents=True)
                (transfer / "junk.txt").write_text("junk", encoding="utf-8")
                final = movies / "Director" / "Example (2024)"
                final.mkdir(parents=True)
                (final / "Example (2024) [1080p].mkv").write_bytes(b"video")
                (final / "Example (2024) [1080p].nfo").write_text("<movie/>", encoding="utf-8")
                (final / "Example (2024).png").write_bytes(b"png")
                with patch.object(finish_staging, "verify_recorded_torrents_absent", return_value={"checked": 0, "all_absent": True}):
                    result = finish_staging.clean_completed_job(final, [transfer], path)
            state = movies / ".movies-nerd"
            self.assertTrue(result["clean"])
            self.assertTrue((state / "cache" / "provider-health.json").is_file())
            self.assertFalse((state / "jobs").exists())
            self.assertFalse((state / "trash").exists())
            self.assertFalse(stage.exists())

    def test_success_cleanup_refuses_when_recorded_torrent_remains(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            env = roots(base)
            movies = Path(env[_common.MOVIES_ROOT_ENV])
            with patch.dict(os.environ, env, clear=False):
                path = job_manifest.create_job("movie", "Example", 2024)
                job_manifest.update_job(path, {
                    "state": "imported",
                    "controller": {"active_hash": "a" * 40, "tried_hashes": ["a" * 40]},
                })
                transfer = movies / ".incoming" / "Movies Nerd" / "transfers" / ("a" * 40)
                transfer.mkdir(parents=True)
                final = movies / "Director" / "Example (2024)"
                final.mkdir(parents=True)
                (final / "Example.mkv").write_bytes(b"video")
                (final / "Example.nfo").write_text("<movie/>", encoding="utf-8")
                (final / "Example.png").write_bytes(b"png")
                with (
                    patch.object(finish_staging, "connected_client", return_value=object()),
                    patch.object(finish_staging, "torrent_info", return_value={"hash": "a" * 40}),
                ):
                    with self.assertRaisesRegex(ValueError, "remove the exact"):
                        finish_staging.clean_completed_job(final, [transfer], path)
                self.assertTrue(path.exists())
                self.assertTrue(transfer.exists())


if __name__ == "__main__":
    unittest.main()
