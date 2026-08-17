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


class MediaSecurityTests(unittest.TestCase):
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
