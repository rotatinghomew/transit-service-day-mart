import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class CommandLineTests(unittest.TestCase):
    def test_build_day_outputs_json(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "serviceday.day",
                "--day",
                "2026-11-29",
                "--facts",
                str(ROOT / "data" / "facts.json"),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["service_day"], "2026-11-29")
        self.assertTrue(payload["version"])
        self.assertTrue(payload["stops"])

    def test_audit_outputs_delivered_and_current(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "serviceday.day",
                "--day",
                "2026-11-29",
                "--facts",
                str(ROOT / "data" / "facts.json"),
                "--audit",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertIn("delivered_version", payload)
        self.assertIn("current", payload)
        self.assertIn("changes", payload)

    def test_output_can_be_written_to_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "payload.json"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "serviceday.day",
                    "--day",
                    "2026-11-29",
                    "--facts",
                    str(ROOT / "data" / "facts.json"),
                    "--out",
                    str(target),
                ],
                capture_output=True,
                text=True,
                cwd=ROOT,
                check=True,
            )
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["service_day"], "2026-11-29")


if __name__ == "__main__":
    unittest.main()
