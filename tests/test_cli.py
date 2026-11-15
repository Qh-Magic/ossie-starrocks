from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.fixtures import sales_model


class CliTests(unittest.TestCase):
    def test_cli_matches_library_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model.json"
            request_path = root / "request.json"
            model_path.write_text(json.dumps(sales_model()), encoding="utf-8")
            request_path.write_text(
                json.dumps({"metrics": ["revenue"], "dimensions": ["customers.country"]}),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ossie_starrocks.cli",
                    "compile",
                    "--model",
                    str(model_path),
                    "--request",
                    str(request_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("LEFT JOIN `default_catalog`.`sales`.`customers`", completed.stdout)
        self.assertEqual("", completed.stderr)

    def test_cli_returns_diagnostic_for_invalid_operator_type(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model.json"
            request_path = root / "request.json"
            model_path.write_text(json.dumps(sales_model()), encoding="utf-8")
            request_path.write_text(
                json.dumps({
                    "metrics": ["revenue"],
                    "where": [{"field": "orders.status", "op": ["eq"], "value": "paid"}],
                }),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            completed = subprocess.run(
                [sys.executable, "-m", "ossie_starrocks.cli", "compile", "--model", str(model_path), "--request", str(request_path)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn('"code": "INVALID_FILTER"', completed.stderr)


if __name__ == "__main__":
    unittest.main()
