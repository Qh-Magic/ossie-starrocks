from __future__ import annotations

import re

from ._model import BoundModel
from ._starrocks import render_query as render_starrocks_query
from .api import CompilationError, CompiledQuery
from .planning import MultiStageQueryPlan, QueryPlan


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN_REWRITES = {
    "DATETIME": "TIMESTAMP",
    "DAYOFYEAR": "DAY_OF_YEAR",
    "IFNULL": "COALESCE",
}
_UNSUPPORTED_FUNCTIONS = frozenset({"LEFT", "RIGHT"})


def render_query(
    model: BoundModel,
    plan: QueryPlan | MultiStageQueryPlan,
) -> CompiledQuery:
    """Render a proven semantic plan as ordinary Trino SQL.

    Planning is dialect-independent. The StarRocks renderer first produces the
    compiler-owned SQL subset, then this tokenizer rewrites only identifiers
    and known dialect tokens. String literals are never rewritten.
    """

    compiled = render_starrocks_query(model, plan)
    return CompiledQuery(
        sql=_to_trino_sql(compiled.sql),
        columns=compiled.columns,
        dependencies=compiled.dependencies,
    )


def _to_trino_sql(sql: str) -> str:
    output: list[str] = []
    position = 0
    while position < len(sql):
        character = sql[position]
        if character == "'":
            end = _quoted_end(sql, position, "'")
            output.append(sql[position:end])
            position = end
            continue
        if character == "`":
            end = _quoted_end(sql, position, "`")
            identifier = sql[position + 1 : end - 1].replace("``", "`")
            output.append('"' + identifier.replace('"', '""') + '"')
            position = end
            continue
        match = _TOKEN.match(sql, position)
        if match is None:
            output.append(character)
            position += 1
            continue
        token = match.group(0)
        upper = token.upper()
        next_position = match.end()
        if upper in _UNSUPPORTED_FUNCTIONS and _next_non_space(sql, next_position) == "(":
            raise CompilationError(
                "UNSUPPORTED_TRINO_FUNCTION",
                "$.expression",
                f"function {upper} has no verified Trino lowering",
            )
        output.append(_TOKEN_REWRITES.get(upper, token))
        position = next_position
    return "".join(output)


def _quoted_end(sql: str, start: int, quote: str) -> int:
    position = start + 1
    while position < len(sql):
        if sql[position] != quote:
            position += 1
            continue
        if position + 1 < len(sql) and sql[position + 1] == quote:
            position += 2
            continue
        return position + 1
    raise CompilationError("INTERNAL_ERROR", "$.sql", "renderer emitted an unterminated quote")


def _next_non_space(sql: str, position: int) -> str | None:
    while position < len(sql) and sql[position].isspace():
        position += 1
    return sql[position] if position < len(sql) else None
