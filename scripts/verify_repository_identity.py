from __future__ import annotations

import inspect
from pathlib import Path
import tomllib

from ossie_starrocks import Compiler


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
require(project["name"] == "ossie-starrocks", "distribution must be ossie-starrocks")
require(set(project["scripts"]) == {"ossie-starrocks"}, "only the ossie-starrocks command may be exported")
require("target" not in inspect.signature(Compiler.compile).parameters, "Compiler.compile must be single-target")
require((ROOT / "src/ossie_starrocks/_starrocks.py").is_file(), "StarRocks renderer is missing")

for forbidden in (
    "src/ossie_starrocks/_trino.py",
    "src/ossie_starrocks/trino.py",
    "src/ossie_starrocks/gateway.py",
    ".github/workflows/live-trino.yml",
    "scripts/live_trino.py",
):
    require(not (ROOT / forbidden).exists(), f"opposite-target artifact must be absent: {forbidden}")
