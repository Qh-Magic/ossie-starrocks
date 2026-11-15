from __future__ import annotations

from dataclasses import dataclass
from ._model import BoundModel, Field, Metric, Relationship
from .api import CompilationError, Query, Scalar

@dataclass(frozen=True)
class PlannedFilter:
    expression_owner: Field | Metric
    op: str
    value: Scalar

@dataclass(frozen=True)
class SafeJoinTree:
    root: str
    edges: tuple[Relationship, ...]

@dataclass(frozen=True)
class QueryPlan:
    joins: SafeJoinTree
    metrics: tuple[Metric, ...]
    dimensions: tuple[Field, ...]
    where: tuple[PlannedFilter, ...]
    having: tuple[PlannedFilter, ...]
    limit: int | None

def plan_query(model: BoundModel, query: Query) -> QueryPlan:
    if not isinstance(query, Query):
        raise CompilationError("INVALID_REQUEST", "$", "query must be a Query")
    if not query.metrics:
        raise CompilationError("EMPTY_METRICS", "$.metrics", "at least one metric is required")
    metrics = tuple(_metric(model, name, f"$.metrics[{i}]") for i, name in enumerate(query.metrics))
    roots = {metric.root for metric in metrics}
    having_pairs = tuple((_metric(model, item.metric, f"$.having[{i}].metric"), item) for i, item in enumerate(query.having))
    roots.update(metric.root for metric, _ in having_pairs)
    if len(roots) != 1:
        raise CompilationError("MIXED_GRAIN", "$.metrics", "all selected and HAVING metrics must share one grain", tuple(sorted(roots)))
    root = next(iter(roots))
    dimensions = tuple(_field(model, name, f"$.dimensions[{i}]") for i, name in enumerate(query.dimensions))
    where_pairs = tuple((_field(model, item.field, f"$.where[{i}].field"), item) for i, item in enumerate(query.where))
    _validate_filters(query)
    if query.limit is not None and (type(query.limit) is not int or not 0 <= query.limit <= 2_147_483_647):
        raise CompilationError("INVALID_LIMIT", "$.limit", "limit must be a non-negative integer")
    required = {root, *(field.dataset for field in dimensions), *(field.dataset for field, _ in where_pairs)}
    edges: list[Relationship] = []
    seen: set[str] = set()
    for dataset in sorted(required):
        for edge in _unique_path(model, root, dataset):
            if edge.name not in seen:
                edges.append(edge)
                seen.add(edge.name)
    return QueryPlan(SafeJoinTree(root, tuple(edges)), metrics, dimensions,
                     tuple(PlannedFilter(field, item.op, item.value) for field, item in where_pairs),
                     tuple(PlannedFilter(metric, item.op, item.value) for metric, item in having_pairs), query.limit)

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
        raise CompilationError("AMBIGUOUS_JOIN", "$.dimensions", f"multiple safe paths from {root} to {target}", tuple(" -> ".join(e.name for e in path) for path in found))
    if found:
        return found[0]
    if _undirected_connected(model, root, target):
        raise CompilationError("FANOUT_JOIN", "$.dimensions", f"reaching {target} from {root} requires a one-to-many traversal")
    raise CompilationError("UNREACHABLE_DATASET", "$.dimensions", f"no relationship path from {root} to {target}")

def _undirected_connected(model: BoundModel, root: str, target: str) -> bool:
    neighbors = {name: set() for name in model.dataset_by_name}
    for edge in model.relationships:
        neighbors[edge.many].add(edge.one)
        neighbors[edge.one].add(edge.many)
    pending, seen = [root], {root}
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
    for collection, filters in (("where", query.where), ("having", query.having)):
        for i, item in enumerate(filters):
            if not isinstance(item.op, str) or item.op not in allowed:
                raise CompilationError("INVALID_FILTER", f"$.{collection}[{i}].op", f"unsupported operator {item.op!r}")
            if item.op in {"is_null", "is_not_null"} and item.value is not None:
                raise CompilationError("INVALID_FILTER", f"$.{collection}[{i}].value", "NULL predicates do not take a value")
            if item.op not in {"is_null", "is_not_null"} and item.value is None:
                raise CompilationError("INVALID_FILTER", f"$.{collection}[{i}].value", "use is_null or is_not_null for NULL")
