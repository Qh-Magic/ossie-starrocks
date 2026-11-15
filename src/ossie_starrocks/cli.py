from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .compiler import CompilationError, Compiler, FieldFilter, MetricFilter, Query


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ossie-starrocks")
    commands = parser.add_subparsers(dest="command", required=True)
    compile_parser = commands.add_parser("compile")
    compile_parser.add_argument("--model", required=True)
    compile_parser.add_argument("--request", required=True)
    compile_parser.add_argument("--target", choices=("starrocks", "trino"), default="starrocks")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        request = json.loads(Path(arguments.request).read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise CompilationError("INVALID_REQUEST", "$", "request must be an object")
        query = Query(
            metrics=_strings(request.get("metrics", []), "$.metrics"),
            dimensions=_strings(request.get("dimensions", []), "$.dimensions"),
            filter=request.get("filter"),
            where=tuple(_field_filter(item, index) for index, item in enumerate(_objects(request.get("where", []), "$.where"))),
            having=tuple(_metric_filter(item, index) for index, item in enumerate(_objects(request.get("having", []), "$.having"))),
            limit=request.get("limit"),
        )
        result = Compiler.from_file(arguments.model).compile(query, target=arguments.target)
    except (CompilationError, OSError, json.JSONDecodeError) as error:
        if isinstance(error, CompilationError):
            print(json.dumps({"code": error.code, "location": error.location, "message": error.message}), file=sys.stderr)
        else:
            print(json.dumps({"code": "INVALID_INPUT", "location": "$", "message": str(error)}), file=sys.stderr)
        return 2
    print(result.sql)
    return 0


def _strings(value: object, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CompilationError("INVALID_REQUEST", location, "expected an array of strings")
    return tuple(value)


def _objects(value: object, location: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise CompilationError("INVALID_REQUEST", location, "expected an array of objects")
    return value


def _field_filter(item: dict, index: int) -> FieldFilter:
    try:
        return FieldFilter(item["field"], item["op"], item.get("value"))
    except KeyError as error:
        raise CompilationError("INVALID_REQUEST", f"$.where[{index}]", f"missing {error.args[0]}") from error


def _metric_filter(item: dict, index: int) -> MetricFilter:
    try:
        return MetricFilter(item["metric"], item["op"], item.get("value"))
    except KeyError as error:
        raise CompilationError("INVALID_REQUEST", f"$.having[{index}]", f"missing {error.args[0]}") from error


if __name__ == "__main__":
    raise SystemExit(main())
