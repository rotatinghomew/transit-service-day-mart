import json
from pathlib import Path
import unittest


class FixtureIntegrityTests(unittest.TestCase):
    def test_service_day_and_partial_confirmation_are_present(self):
        data = json.loads((Path(__file__).parents[1] / "data/facts.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["service_calendars"]), 2)
        self.assertEqual(len(data["instructions"]), 2)
        self.assertEqual({row["state"] for row in data["connections"]}, {"available", "pending"})


if __name__ == "__main__":
    unittest.main()
