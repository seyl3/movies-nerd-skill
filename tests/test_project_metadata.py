from __future__ import annotations

from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import validate_project


class ProjectMetadataTests(unittest.TestCase):
    def test_repository_metadata_and_structure_are_consistent(self):
        self.assertEqual(validate_project.validate(), [])


if __name__ == "__main__":
    unittest.main()
