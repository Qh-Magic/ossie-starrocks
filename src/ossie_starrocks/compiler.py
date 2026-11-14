from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from ._expressions import Binary, Call, Column, Expr, Literal as ExprLiteral, Star
from ._model import BoundModel, Field, Metric, ModelError, Relationship, load_document
from ._starrocks import SUPPORTED_AGGREGATES


Scalar = str | int | Decimal | bool | date | datetime | None
Operator = Literal["eq", "ne", "lt", "le", "gt", "ge", "is_null", "is_not_null"]


class CompilationError(Exception):
    def __init__(self, code: str, location: str, message: str, related: tuple[str, ...] = ()):
        super().__init__(message)
        self.code = code
        self.location = location
        self.message = message
        self.related = related


@dataclass(frozen=True)
class FieldFilter:
    field: str
    op: Operator
    value: Scalar = None


@dataclass(frozen=True)
class MetricFilter:
    metric: str
    op: Operator
    value: Scalar = None


@dataclass(frozen=True)
class Query:
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...] = ()
    where: tuple[FieldFilter, ...] = ()
    having: tuple[MetricFilter, ...] = ()
    limit: int | None = None


@dataclass(frozen=True)
class OutputColumn:
    semantic_name: str
    sql_alias: str


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    columns: tuple[OutputColumn, ...]
    dependencies: tuple[str, ...]


class Compiler:
    def __init__(self, model: BoundModel):
        self._model = model

    @classmethod
    def from_file(cls, path: str | Path) -> "Compiler":
        source = Path(path)
        format = "yaml" if source.suffix.lower() in {".yaml", ".yml"} else "json"
        return cls.from_text(source.read_text(encoding="utf-8"), format=format)

    @classmethod
    def from_text(cls, document: str, *, format: Literal["json", "yaml"]) -> "Compiler":
        try:
            return cls(load_document(document, format))
        except ModelError as error:
            raise CompilationError(error.code, error.location, str(error)) from error

    def compile(self, query: Query) -> CompiledQuery:
        return _compile(self._model, query)


def _compile(model: BoundModel, query: Query) -> CompiledQuery:
    if not isinstance(query, Query):
        raise CompilationError("INVALID_REQUEST", "$", "query must be a Query")
    if not query.metrics:
        raise CompilationError("EMPTY_METRICS", "$.metrics", "at least one metric is required")
    metrics = tuple(_metric(model, name, f"$.metrics[{index}]") for index, name in enumerate(query.metrics))
    roots = {metric.root for metric in metrics}
    having = tuple((_metric(model, item.metric, f"$.having[{index}].metric"), item) for index, item in enumerate(query.having))
    roots.update(metric.root for metric, _ in having)
    if len(roots) != 1:
        raise CompilationError("MIXED_GRAIN", "$.metrics", "all selected and HAVING metrics must share one grain", tuple(sorted(roots)))
    root = next(iter(roots))
    dimensions = tuple(_field(model, name, f"$.dimensions[{index}]") for index, name in enumerate(query.dimensions))
    where = tuple((_field(model, item.field, f"$.where[{index}].field"), item) for index, item in enumerate(query.where))
    _validate_filters(query)
    if query.limit is not None and (type(query.limit) is not int or not 0 <= query.limit <= 2_147_483_647):
        raise CompilationError("INVALID_LIMIT", "$.limit", "limit must be a non-negative integer")

    required = {field.dataset for field in dimensions}
    required.update(field.dataset for field, _ in where)
    required.update(root for _ in [0])
    paths: dict[str, tuple[Relationship, ...]] = {}
    for dataset in sorted(required):
        paths[dataset] = _unique_path(model, root, dataset)
    ordered_edges: list[Relationship] = []
    seen_edges: set[str] = set()
    for dataset in sorted(paths):
        for edge in paths[dataset]:
            if edge.name not in seen_edges:
                ordered_edges.append(edge)
                seen_edges.add(edge.name)

    aliases: dict[str, str] = {root: "t0"}
    for edge in ordered_edges:
        aliases.setdefault(edge.one, f"t{len(aliases)}")
    lines: list[str] = []
    projections: list[str] = []
    columns: list[OutputColumn] = []
    for index, field in enumerate(dimensions):
        alias = f"d{index}"
        projections.append(f"{_render_expr(field.expression, aliases)} AS `{alias}`")
        columns.append(OutputColumn(f"{field.dataset}.{field.name}", alias))
    for index, metric in enumerate(metrics):
        alias = f"m{index}"
        projections.append(f"{_render_expr(metric.expression, aliases)} AS `{alias}`")
        columns.append(OutputColumn(metric.name, alias))
    lines.append("SELECT " + ", ".join(projections))
    lines.append(f"FROM {_render_table(model, root)} AS {aliases[root]}")
    for edge in ordered_edges:
        conditions = " AND ".join(
            f"{aliases[edge.many]}.`{left}` = {aliases[edge.one]}.`{right}`"
            for left, right in edge.keys
        )
        lines.append(f"INNER JOIN {_render_table(model, edge.one)} AS {aliases[edge.one]} ON {conditions}")
    if where:
        lines.append("WHERE " + " AND ".join(_render_filter(field.expression, item.op, item.value, aliases) for field, item in where))
    if dimensions:
        lines.append("GROUP BY " + ", ".join(_render_expr(field.expression, aliases) for field in dimensions))
    if having:
        lines.append("HAVING " + " AND ".join(_render_filter(metric.expression, item.op, item.value, aliases) for metric, item in having))
    if query.limit is not None:
        lines.append(f"LIMIT {query.limit}")
    dependencies = tuple(dict.fromkeys([root, *(edge.one for edge in ordered_edges)]))
    return CompiledQuery("\n".join(lines), tuple(columns), dependencies)


def _field(model: BoundModel, name: str, location: str) -> Field:
    field = model.field_by_name.get(name)
    if field is None:
        raise CompilationError("UNKNOWN_FIELD", location, f"unknown field {name!r}")
    return field


def _metric(model: BoundModel, name: str, location: str) -> Metric:
    metric = model.metric_by_name.get(name)
    if metric is None:
        raise CompilationError("UNKNOWN_METRIC", location, f"unknown or ambiguous metric {name!r}")
    return metric


def _unique_path(model: BoundModel, root: str, target: str) -> tuple[Relationship, ...]:
    if root == target:
        return ()
    found: list[tuple[Relationship, ...]] = []

    def visit(dataset: str, path: tuple[Relationship, ...]) -> None:
        if len(found) >= 2:
            return
        for edge in model.outgoing[dataset]:
            next_path = (*path, edge)
            if edge.one == target:
                found.append(next_path)
                if len(found) >= 2:
                    return
            else:
                visit(edge.one, next_path)

    visit(root, ())
    if len(found) > 1:
        raise CompilationError("AMBIGUOUS_JOIN", "$.dimensions", f"multiple safe paths from {root} to {target}", tuple(" -> ".join(edge.name for edge in path) for path in found))
    if found:
        return found[0]
    if _undirected_connected(model, root, target):
        raise CompilationError("FANOUT_JOIN", "$.dimensions", f"reaching {target} from {root} requires a one-to-many traversal")
    raise CompilationError("UNREACHABLE_DATASET", "$.dimensions", f"no relationship path from {root} to {target}")


def _undirected_connected(model: BoundModel, root: str, target: str) -> bool:
    neighbors: dict[str, set[str]] = {name: set() for name in model.dataset_by_name}
    for edge in model.relationships:
        neighbors[edge.many].add(edge.one)
        neighbors[edge.one].add(edge.many)
    pending = [root]
    seen = {root}
    while pending:
        current = pending.pop()
        if current == target:
            return True
        for neighbor in neighbors[current] - seen:
            seen.add(neighbor)
            pending.append(neighbor)
    return False


def _validate_filters(query: Query) -> None:
    allowed = {"eq", "ne", "lt", "le", "gt", "ge", "is_null", "is_not_null"}
    for collection_name, filters in (("where", query.where), ("having", query.having)):
        for index, item in enumerate(filters):
            if not isinstance(item.op, str) or item.op not in allowed:
                raise CompilationError("INVALID_FILTER", f"$.{collection_name}[{index}].op", f"unsupported operator {item.op!r}")
            if item.op in {"is_null", "is_not_null"}:
                if item.value is not None:
                    raise CompilationError("INVALID_FILTER", f"$.{collection_name}[{index}].value", "NULL predicates do not take a value")
            elif item.value is None:
                raise CompilationError("INVALID_FILTER", f"$.{collection_name}[{index}].value", "use is_null or is_not_null for NULL")


def _render_table(model: BoundModel, dataset_name: str) -> str:
    return ".".join(f"`{part}`" for part in model.dataset_by_name[dataset_name].source.parts)


def _render_expr(expression: Expr, aliases: dict[str, str]) -> str:
    if isinstance(expression, Column):
        if expression.dataset is None or expression.dataset not in aliases:
            raise CompilationError("INTERNAL_ERROR", "$.expression", "unplanned dataset in expression")
        return f"{aliases[expression.dataset]}.`{expression.name}`"
    if isinstance(expression, ExprLiteral):
        return _literal(expression.value)
    if isinstance(expression, Star):
        return "*"
    if isinstance(expression, Binary):
        return f"({_render_expr(expression.left, aliases)} {expression.op} {_render_expr(expression.right, aliases)})"
    if isinstance(expression, Call):
        if expression.name not in SUPPORTED_AGGREGATES:
            raise CompilationError("UNSUPPORTED_EXPRESSION", "$.expression", f"unsupported function {expression.name}")
        return f"{expression.name}({', '.join(_render_expr(arg, aliases) for arg in expression.args)})"
    raise AssertionError(expression)


def _render_filter(expression: Expr, op: str, value: Scalar, aliases: dict[str, str]) -> str:
    left = _render_expr(expression, aliases)
    if op == "is_null":
        return f"{left} IS NULL"
    if op == "is_not_null":
        return f"{left} IS NOT NULL"
    operator = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[op]
    return f"{left} {operator} {_literal(value)}"


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if type(value) is int:
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CompilationError("UNSAFE_LITERAL", "$.filter", "decimal must be finite")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise CompilationError("UNSAFE_LITERAL", "$.filter", "timezone-aware datetime is not supported")
        return f"CAST('{value.isoformat(sep=' ')}' AS DATETIME)"
    if isinstance(value, date):
        return f"CAST('{value.isoformat()}' AS DATE)"
    if isinstance(value, str):
        if "\x00" in value or "\\" in value:
            raise CompilationError("UNSAFE_LITERAL", "$.filter", "NUL and backslash string values are not supported")
        return "'" + value.replace("'", "''") + "'"
    raise CompilationError("UNSAFE_LITERAL", "$.filter", f"unsupported literal type {type(value).__name__}")
