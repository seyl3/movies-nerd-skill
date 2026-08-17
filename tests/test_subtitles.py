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


class SubtitleTests(unittest.TestCase):
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
