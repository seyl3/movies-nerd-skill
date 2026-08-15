from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common
import cinemeta
import library_inventory
import movies_nerd


def roots(base: Path) -> dict[str, str]:
    return {
        _common.MOVIES_ROOT_ENV: str(base / "Films"),
        _common.SERIES_ROOT_ENV: str(base / "Series"),
    }


class ControllerTests(unittest.TestCase):
    def test_title_parenthesis_supplies_year(self):
        self.assertEqual(
            movies_nerd.split_title_year("Volver (2006)"),
            ("Volver", 2006),
        )

    def test_single_controller_bridges_identity_prepare_and_ready(self):
        identity = {
            "imdb_id": "tt0441909", "canonical_title": "Volver", "year": 2006,
        }
        resolver = Mock(return_value=identity)
        preparer = Mock(return_value={
            "prepared": True, "job": "/tmp/job.json", "title": "Volver (2006)",
        })
        runner = Mock(return_value={"ready": True, "destination": "/Films/Volver"})
        with tempfile.TemporaryDirectory() as raw:
            env = roots(Path(raw))
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(movies_nerd, "resumable_job", return_value=None),
            ):
                result = movies_nerd.download_one(
                    title="Volver (2006)", resolver=resolver,
                    preparer=preparer, runner=runner,
                )
        self.assertTrue(result["ready"])
        resolver.assert_called_once_with("movie", "Volver", 2006, None)
        self.assertEqual(preparer.call_args.kwargs["resolved_identity"], identity)
        runner.assert_called_once_with(
            Path("/tmp/job.json"), poll_seconds=5, artifact_wait_seconds=600,
        )

    def test_resumable_job_skips_new_preparation(self):
        identity = {
            "imdb_id": "tt0441909", "canonical_title": "Volver", "year": 2006,
        }
        preparer = Mock()
        runner = Mock(return_value={"ready": True})
        with (
            patch.object(movies_nerd, "resumable_job", return_value=Path("/tmp/existing.json")),
        ):
            result = movies_nerd.download_one(
                title="Volver", year=2006,
                resolver=Mock(return_value=identity), preparer=preparer, runner=runner,
            )
        self.assertTrue(result["resumed"])
        preparer.assert_not_called()

    def test_batch_has_no_default_title_limit_and_preserves_order(self):
        items = [{"title": f"Film {index}"} for index in range(6)]

        def worker(**item):
            return {"ready": True, "title": item["title"]}

        result = movies_nerd.download_many(items, worker=worker)
        self.assertEqual(result["requested"], 6)
        self.assertEqual(result["ready"], 6)
        self.assertEqual(result["concurrency"], 6)
        self.assertEqual([item["title"] for item in result["jobs"]], [
            "Film 0", "Film 1", "Film 2", "Film 3", "Film 4", "Film 5",
        ])

    def test_identity_can_resolve_a_unique_title_without_year(self):
        response = {"metas": [{
            "name": "Volver", "aliases": [], "releaseInfo": "2006",
            "imdb_id": "tt0441909",
        }]}
        with patch.object(cinemeta, "fetch_json", return_value=response):
            result = cinemeta.resolve_request("movie", "Volver")
        self.assertEqual(result, {
            "imdb_id": "tt0441909", "canonical_title": "Volver", "year": 2006,
        })

    def test_missing_year_remains_ambiguous_when_titles_collide(self):
        response = {"metas": [{
            "name": "Crash", "releaseInfo": "1996", "imdb_id": "tt0115964",
        }, {
            "name": "Crash", "releaseInfo": "2004", "imdb_id": "tt0375679",
        }]}
        with patch.object(cinemeta, "fetch_json", return_value=response):
            with self.assertRaisesRegex(cinemeta.CinemetaError, "did not resolve"):
                cinemeta.resolve_request("movie", "Crash")


class InventoryTests(unittest.TestCase):
    def test_shallow_scan_reads_directors_and_other_nfo(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "Films"
            kubrick = root / "Stanley Kubrick" / "2001 A Space Odyssey (1968)"
            volver = root / "Other" / "Volver (2006)"
            hidden = root / ".incoming" / "Ignored (2020)"
            kubrick.mkdir(parents=True)
            volver.mkdir(parents=True)
            hidden.mkdir(parents=True)
            (volver / "Volver.nfo").write_text(
                "<movie><director>Pedro Almodóvar</director></movie>", encoding="utf-8",
            )
            inventory = library_inventory.scan(root)
        self.assertEqual(inventory["owned_count"], 2)
        self.assertEqual(
            inventory["owned_by_director"]["Stanley Kubrick"],
            ["2001 A Space Odyssey (1968)"],
        )
        self.assertEqual(
            inventory["owned_by_director"]["Pedro Almodóvar"],
            ["Volver (2006)"],
        )
        self.assertNotIn(".incoming", json.dumps(inventory))

    def test_recommendation_context_is_compact_and_completed_director_first(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "Films"
            (root / "A Director" / "Existing (2000)").mkdir(parents=True)
            context = library_inventory.recommendation_context(
                root, title="New Film", year=2026, director="New Director",
            )
        self.assertEqual(context["owned_count"], 2)
        self.assertEqual(next(iter(context["owned_by_director"])), "New Director")
        self.assertEqual(context["owned_by_director"]["New Director"], ["New Film (2026)"])
        self.assertNotIn(str(root), json.dumps(context))

    def test_existing_title_membership_uses_title_and_year(self):
        inventory = {
            "owned_by_director": {"Director": ["The Great Escape (1963)"]},
        }
        self.assertTrue(library_inventory.contains(inventory, ["The Great Escape"], 1963))
        self.assertFalse(library_inventory.contains(inventory, ["The Great Escape"], 2020))


if __name__ == "__main__":
    unittest.main()
