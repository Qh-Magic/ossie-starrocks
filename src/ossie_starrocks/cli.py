from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from .compiler import CompilationError, Compiler, FieldFilter, MetricFilter, Query
from .trino import TrinoClient, TrinoError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ossie-starrocks")
    commands = parser.add_subparsers(dest="command", required=True)
    compile_parser = commands.add_parser("compile")
    _add_semantic_inputs(compile_parser)
    compile_parser.add_argument("--target", choices=("starrocks", "trino"), default="starrocks")
    query_parser = commands.add_parser("query", help="compile and execute a semantic request on Trino")
    _add_semantic_inputs(query_parser)
    query_parser.add_argument("--trino-url", required=True)
    query_parser.add_argument("--user", required=True)
    query_parser.add_argument("--catalog")
    query_parser.add_argument("--schema")
    query_parser.add_argument("--timeout", type=float, default=30.0)
    query_parser.add_argument("--query-timeout-seconds", type=float, default=300.0)
    query_parser.add_argument("--retries", type=int, default=3)
    query_parser.add_argument("--max-rows", type=int, default=10_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        query = _read_query(arguments.request)
        target = arguments.target if arguments.command == "compile" else "trino"
        compiler = Compiler.from_file(arguments.model)
        result = compiler.compile(query, target=target)
        if arguments.command == "query":
            executed = TrinoClient(
                arguments.trino_url,
                arguments.user,
                catalog=arguments.catalog,
                schema=arguments.schema,
                timeout=arguments.timeout,
                query_timeout=arguments.query_timeout_seconds,
                max_retries=arguments.retries,
                max_rows=arguments.max_rows,
            ).execute(result.sql)
    except (CompilationError, OSError, json.JSONDecodeError, ValueError, TrinoError) as error:
        if isinstance(error, CompilationError):
            print(json.dumps({"code": error.code, "location": error.location, "message": error.message}), file=sys.stderr)
            return 2
        if isinstance(error, TrinoError):
            print(
                json.dumps({"code": error.code, "queryId": error.query_id, "message": error.message}),
                file=sys.stderr,
            )
            return 3
        else:
            print(json.dumps({"code": "INVALID_INPUT", "location": "$", "message": str(error)}), file=sys.stderr)
        return 2
    if arguments.command == "compile":
        print(result.sql)
    else:
        identity = compiler.model_identity
        print(json.dumps({
            "queryId": executed.query_id,
            "columns": [{"name": column.name, "type": column.type} for column in executed.columns],
            "rows": executed.rows,
            "sql": result.sql,
            "dependencies": result.dependencies,
            "audit": {
                "modelName": identity.name,
                "modelVersion": identity.version,
                "modelSha256": identity.sha256,
                "requestSha256": _file_sha256(arguments.request),
                "target": "trino",
                "trinoUser": arguments.user,
                "queryId": executed.query_id,
                "dependencies": result.dependencies,
                "sql": result.sql,
            },
        }))
    return 0


def _add_semantic_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", required=True)
    parser.add_argument("--request", required=True)


def _file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_query(path: str) -> Query:
    request = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise CompilationError("INVALID_REQUEST", "$", "request must be an object")
    return Query(
        metrics=_strings(request.get("metrics", []), "$.metrics"),
        dimensions=_strings(request.get("dimensions", []), "$.dimensions"),
        filter=request.get("filter"),
        where=tuple(_field_filter(item, index) for index, item in enumerate(_objects(request.get("where", []), "$.where"))),
        having=tuple(_metric_filter(item, index) for index, item in enumerate(_objects(request.get("having", []), "$.having"))),
        limit=request.get("limit"),
    )


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
