from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common
import cinemeta
import job_manifest
import prepare_artifacts
import prepare_job
import search_releases
import title_policy
import wikidata_titles


def roots(base: Path) -> dict[str, str]:
    return {
        _common.MOVIES_ROOT_ENV: str(base / "Films"),
        _common.SERIES_ROOT_ENV: str(base / "Series"),
    }


def candidate(info_hash: str, source: str) -> dict:
    return {
        "title": "The Great Escape (1963) 1080p x265",
        "source": source,
        "provider": source,
        "size_bytes": 2_000_000_000,
        "size": "1.86 GiB",
        "resolution": "1080p",
        "seeders": 20,
        "leechers": 2,
        "score": 100,
        "warnings": [],
        "magnet": search_releases.minimal_magnet(info_hash, "The Great Escape"),
        "info_hash": info_hash,
    }


class TitlePolicyTests(unittest.TestCase):
    def test_english_original_keeps_original_and_searches_it_before_french(self):
        policy = title_policy.decide(
            requested_title="La Grande Évasion",
            canonical_title="The Great Escape",
            original_title="The Great Escape",
            french_title="La Grande Évasion",
            english_title="The Great Escape",
            original_languages=["en"],
        )
        self.assertEqual(policy["display_title"], "The Great Escape")
        self.assertEqual(policy["search_titles"], ["The Great Escape", "La Grande Évasion"])

    def test_french_original_keeps_original_title(self):
        policy = title_policy.decide(
            requested_title="La Collectionneuse",
            canonical_title="La Collectionneuse",
            original_title="La Collectionneuse",
            french_title="La Collectionneuse",
            english_title="The Collector",
            original_languages=["fr"],
        )
        self.assertEqual(policy["display_title"], "La Collectionneuse")
        self.assertEqual(policy["search_titles"][0], "La Collectionneuse")

    def test_other_language_uses_french_then_original_and_searches_english_first(self):
        policy = title_policy.decide(
            requested_title="Tout sur ma mère",
            canonical_title="All About My Mother",
            original_title="Todo sobre mi madre",
            french_title="Tout sur ma mère",
            english_title="All About My Mother",
            original_languages=["es"],
        )
        self.assertEqual(
            policy["display_title"],
            "Tout sur ma mère (Todo sobre mi madre)",
        )
        self.assertEqual(
            policy["search_titles"],
            ["All About My Mother", "Todo sobre mi madre", "Tout sur ma mère"],
        )

    def test_prepare_job_resolves_french_request_before_torrent_search(self):
        captured = {}

        def fake_search(query, timeout, series, use_qbt, **kwargs):
            captured.update({"query": query, **kwargs})
            return (
                [candidate("a" * 40, "YTS"), candidate("b" * 40, "APIBay")],
                {"yts": {"ok": True, "results": 2, "latency_ms": 20}},
                True,
            )

        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(prepare_job, "dead_hashes", return_value=set()),
                patch.object(prepare_job, "record_provider"),
                patch.object(
                    cinemeta, "resolve_identity",
                    return_value={
                        "imdb_id": "tt0057115",
                        "canonical_title": "The Great Escape",
                    },
                ),
                patch.object(prepare_job, "search_all", side_effect=fake_search),
            ):
                result = prepare_job.prepare(
                    title="La Grande Évasion", year=1963, kind="movie",
                    runtime_minutes=172, imdb_id=None, max_gib=15, timeout=5,
                )
                _, job = job_manifest.load_job(result["job"])
        self.assertEqual(captured["query"], "The Great Escape 1963")
        self.assertEqual(
            captured["search_titles"],
            ["The Great Escape", "La Grande Évasion"],
        )
        self.assertEqual(job["identity"]["title"], "The Great Escape")
        self.assertEqual(
            job["cache"]["title_policy"]["requested_title"],
            "La Grande Évasion",
        )

    def test_provider_uses_french_alias_only_when_canonical_has_no_exact_match(self):
        calls = []

        def provider(query, timeout):
            calls.append(query)
            if query.startswith("The Great Escape"):
                return []
            item = candidate("c" * 40, "alias-provider")
            item["title"] = "La Grande Évasion 1963 1080p x265"
            return [item]

        results = search_releases.search_title_aliases(
            provider, ["The Great Escape", "La Grande Évasion"], 1963, 3,
        )
        self.assertEqual(
            calls,
            ["The Great Escape 1963", "La Grande Évasion 1963"],
        )
        self.assertEqual(len(results), 1)

    def test_background_enrichment_sets_non_english_library_title(self):
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with patch.dict(os.environ, env, clear=False):
                initial = title_policy.decide(
                    requested_title="Tout sur ma mère",
                    canonical_title="All About My Mother",
                )
                path = job_manifest.create_job(
                    "movie", "All About My Mother", 1999,
                    {
                        "identity": {"ids": {"imdb": "tt0185125"}},
                        "cache": {"title_policy": initial},
                    },
                    environ=env,
                )
                with patch.object(
                    wikidata_titles, "title_facts",
                    return_value={
                        "original_title": "Todo sobre mi madre",
                        "original_languages": ["es"],
                        "french_title": "Tout sur ma mère",
                        "english_title": "All About My Mother",
                    },
                ):
                    _, job = job_manifest.load_job(path, env)
                    enriched = prepare_artifacts.enrich_title_policy(
                        path, job, {"name": "All About My Mother"}, "tt0185125",
                    )
        self.assertEqual(
            title_policy.library_title(enriched),
            "Tout sur ma mère (Todo sobre mi madre)",
        )


class WikidataTitleTests(unittest.TestCase):
    def test_original_title_language_wins_over_multilingual_film_languages(self):
        search = {"query": {"search": [{"title": "Q329805"}]}}
        entity = {
            "entities": {
                "Q329805": {
                    "id": "Q329805",
                    "labels": {
                        "en": {"value": "All About My Mother"},
                        "fr": {"value": "Tout sur ma mère"},
                    },
                    "claims": {
                        "P345": [{
                            "rank": "normal",
                            "mainsnak": {"datavalue": {"value": "tt0185125"}},
                        }],
                        "P364": [{
                            "rank": "normal",
                            "mainsnak": {"datavalue": {"value": {"id": "Q1860"}}},
                        }, {
                            "rank": "normal",
                            "mainsnak": {"datavalue": {"value": {"id": "Q1321"}}},
                        }],
                        "P1476": [{
                            "rank": "normal",
                            "mainsnak": {
                                "datavalue": {
                                    "value": {
                                        "text": "Todo sobre mi madre",
                                        "language": "es",
                                    },
                                },
                            },
                        }],
                    },
                },
            },
        }
        with patch.object(wikidata_titles, "fetch_json", side_effect=[search, entity]):
            facts = wikidata_titles.title_facts("tt0185125")
        self.assertEqual(facts["original_title"], "Todo sobre mi madre")
        self.assertEqual(facts["original_languages"], ["es"])
        self.assertEqual(facts["film_languages"], ["en", "es"])
        self.assertEqual(facts["french_title"], "Tout sur ma mère")


if __name__ == "__main__":
    unittest.main()
