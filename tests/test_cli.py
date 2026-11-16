from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from ossie_starrocks.cli import main
from tests.fixtures import sales_model


class CliTests(unittest.TestCase):
    def test_compiles_starrocks_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.json"
            request = root / "request.json"
            model.write_text(json.dumps(sales_model()), encoding="utf-8")
            request.write_text(json.dumps({"metrics": ["revenue"]}), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                status = main(["compile", "--model", str(model), "--request", str(request)])

        self.assertEqual(status, 0)
        self.assertIn("SUM(t0.`amount`)", output.getvalue())

    def test_returns_structured_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.json"
            request = root / "request.json"
            model.write_text(json.dumps(sales_model()), encoding="utf-8")
            request.write_text(json.dumps({"metrics": ["missing"]}), encoding="utf-8")
            error = StringIO()
            with redirect_stderr(error):
                status = main(["compile", "--model", str(model), "--request", str(request)])

        self.assertEqual(status, 2)
        self.assertEqual(json.loads(error.getvalue())["code"], "UNKNOWN_METRIC")


if __name__ == "__main__":
    unittest.main()
