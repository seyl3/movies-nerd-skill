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


class MediaProcessingTests(unittest.TestCase):
    def test_nfo_escapes_untrusted_text(self):
        payload = write_nfo.render("movie", {"title": "A & B <C>", "year": 2024})
        root = ET.fromstring(payload)
        self.assertEqual(root.findtext("title"), "A & B <C>")
        self.assertIn(b"&amp;", payload)
        self.assertIn(b"&lt;C&gt;", payload)

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
