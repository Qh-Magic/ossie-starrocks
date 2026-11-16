from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .compiler import CompilationError, Compiler
from .request import query_from_mapping


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ossie-starrocks")
    commands = parser.add_subparsers(dest="command", required=True)
    compile_parser = commands.add_parser("compile", help="compile a semantic request to StarRocks SQL")
    compile_parser.add_argument("--model", required=True)
    compile_parser.add_argument("--request", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        document = json.loads(Path(arguments.request).read_text(encoding="utf-8"))
        query = query_from_mapping(document)
        result = Compiler.from_file(arguments.model).compile(query)
    except (CompilationError, OSError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, CompilationError):
            payload = {"code": error.code, "location": error.location, "message": error.message}
        else:
            payload = {"code": "INVALID_INPUT", "location": "$", "message": str(error)}
        print(json.dumps(payload), file=sys.stderr)
        return 2
    print(result.sql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
