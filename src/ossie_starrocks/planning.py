from __future__ import annotations

from dataclasses import dataclass
from ._expressions import Between, Binary, Call, Case, Column, Expr, FilteredAggregate, InList, IsNull, Like, Literal, MeasureRef, Star, Unary
from ._model import BoundModel, Field, Metric, Relationship
from ._profile import SUPPORTED_AGGREGATES
from .api import CompilationError, FieldFilter, Query, Scalar

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

@dataclass(frozen=True)
class MultiStageStage:
    key: str
    root: str
    expression: Expr
    joins: SafeJoinTree

@dataclass(frozen=True)
class MultiStageQueryPlan:
    metrics: tuple[Metric, ...]
    metric_stage_keys: tuple[str | None, ...]
    direct_stage_keys: tuple[tuple[str, str], ...]
    dimensions: tuple[Field, ...]
    stages: tuple[MultiStageStage, ...]
    where: tuple[PlannedFilter, ...]
    having: tuple[PlannedFilter, ...]
    limit: int | None

def plan_query(model: BoundModel, query: Query) -> QueryPlan | MultiStageQueryPlan:
    if not isinstance(query, Query):
        raise CompilationError("INVALID_REQUEST", "$", "query must be a Query")
    if not query.metrics and not query.dimensions:
        raise CompilationError("EMPTY_QUERY", "$", "at least one metric or dimension is required")
    dimensions = tuple(_field(model, name, f"$.dimensions[{i}]") for i, name in enumerate(query.dimensions))
    where_pairs = tuple((_field(model, item.field, f"$.where[{i}].field"), item) for i, item in enumerate(query.where))
    _validate_filters(query)
    if query.limit is not None and (type(query.limit) is not int or not 0 <= query.limit <= 2_147_483_647):
        raise CompilationError("INVALID_LIMIT", "$.limit", "limit must be a non-negative integer")
    if not query.metrics:
        if query.having:
            raise CompilationError("INVALID_REQUEST", "$.having", "dimension-only queries cannot have metric filters")
        return _plan_dimension_only(model, dimensions, where_pairs, query.limit)
    metrics = tuple(_metric(model, name, f"$.metrics[{i}]") for i, name in enumerate(query.metrics))
    multistage = any(len(metric.roots) > 1 for metric in metrics)
    roots = {metric.root for metric in metrics}
    having_pairs = tuple((_metric(model, item.metric, f"$.having[{i}].metric"), item) for i, item in enumerate(query.having))
    multistage = multistage or any(len(metric.roots) > 1 for metric, _ in having_pairs)
    roots.update(metric.root for metric, _ in having_pairs)
    multistage = multistage or len(roots) > 1
    if multistage:
        stage_inputs: list[tuple[str, str, Expr]] = []
        seen_stages: set[str] = set()
        direct_stage_keys: dict[str, str] = {}
        all_metrics = tuple(dict.fromkeys((*metrics, *(metric for metric, _ in having_pairs))))
        for metric in all_metrics:
            if len(metric.roots) == 1:
                assert metric.root is not None
                key = _direct_stage_key(metric)
                direct_stage_keys[metric.qualified_name] = key
                if key not in seen_stages:
                    stage_inputs.append((key, metric.root, metric.expression))
                    seen_stages.add(key)
                continue
            for reference in _multistage_refs(metric.expression):
                key = _measure_stage_key(reference)
                if key not in seen_stages:
                    stage_inputs.append((key, reference.dataset, reference.expression))
                    seen_stages.add(key)
        stages: list[MultiStageStage] = []
        for key, root, expression in stage_inputs:
            edges: list[Relationship] = []
            seen_edges: set[str] = set()
            required_datasets = {
                *(field.dataset for field in dimensions),
                *(field.dataset for field, _ in where_pairs),
            }
            for dataset in sorted(required_datasets):
                for edge in _unique_path(model, root, dataset):
                    if edge.name not in seen_edges:
                        edges.append(edge)
                        seen_edges.add(edge.name)
            stages.append(MultiStageStage(key, root, expression, SafeJoinTree(root, tuple(edges))))
        return MultiStageQueryPlan(
            metrics,
            tuple(direct_stage_keys.get(metric.qualified_name) for metric in metrics),
            tuple(direct_stage_keys.items()),
            dimensions,
            tuple(stages),
            tuple(PlannedFilter(field, item.op, item.value) for field, item in where_pairs),
            tuple(PlannedFilter(metric, item.op, item.value) for metric, item in having_pairs),
            query.limit,
        )
    root = next(iter(roots))
    assert root is not None
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

def _plan_dimension_only(
    model: BoundModel,
    dimensions: tuple[Field, ...],
    where_pairs: tuple[tuple[Field, FieldFilter], ...],
    limit: int | None,
) -> QueryPlan:
    dimension_datasets = tuple(dict.fromkeys(field.dataset for field in dimensions))
    candidates: list[tuple[str, tuple[Relationship, ...]]] = []
    ambiguous: list[CompilationError] = []
    for anchor in dimension_datasets:
        edges: list[Relationship] = []
        seen: set[str] = set()
        for target in dimension_datasets:
            try:
                path = _unique_path(model, anchor, target)
            except CompilationError as error:
                if error.code == "AMBIGUOUS_JOIN":
                    ambiguous.append(error)
                break
            for edge in path:
                if edge.name not in seen:
                    edges.append(edge)
                    seen.add(edge.name)
        else:
            candidates.append((anchor, tuple(edges)))
    if len(candidates) > 1:
        raise CompilationError(
            "AMBIGUOUS_ANCHOR",
            "$.dimensions",
            "multiple dimension datasets can reach every requested dimension",
            tuple(anchor for anchor, _ in candidates),
        )
    if not candidates:
        if ambiguous:
            raise ambiguous[0]
        raise CompilationError(
            "NO_ANCHOR",
            "$.dimensions",
            "no dimension dataset can safely reach every requested dimension",
            dimension_datasets,
        )
    anchor, initial_edges = candidates[0]
    edges = list(initial_edges)
    seen = {edge.name for edge in edges}
    for field, _ in where_pairs:
        for edge in _unique_path(model, anchor, field.dataset):
            if edge.name not in seen:
                edges.append(edge)
                seen.add(edge.name)
    return QueryPlan(
        SafeJoinTree(anchor, tuple(edges)),
        (),
        dimensions,
        tuple(PlannedFilter(field, item.op, item.value) for field, item in where_pairs),
        (),
        limit,
    )

def _multistage_refs(expression: Expr) -> tuple[MeasureRef, ...]:
    found: list[MeasureRef] = []

    def visit(node: Expr) -> None:
        if isinstance(node, MeasureRef):
            found.append(node)
            return
        if isinstance(node, (Column, Star, FilteredAggregate)):
            raise CompilationError("MULTISTAGE_UNSUPPORTED_EXPRESSION", "$.metrics", "cross-grain expression must be composed from AGG(dataset.metric) values")
        if isinstance(node, Binary):
            visit(node.left); visit(node.right)
        elif isinstance(node, (Unary, IsNull)):
            visit(node.operand)
        elif isinstance(node, Case):
            for condition, value in node.branches:
                visit(condition); visit(value)
            if node.otherwise is not None:
                visit(node.otherwise)
        elif isinstance(node, Between):
            visit(node.operand); visit(node.lower); visit(node.upper)
        elif isinstance(node, InList):
            visit(node.operand)
            for value in node.values:
                visit(value)
        elif isinstance(node, Like):
            visit(node.operand); visit(node.pattern)
        elif isinstance(node, Call):
            if node.name in SUPPORTED_AGGREGATES:
                raise CompilationError("MULTISTAGE_UNSUPPORTED_EXPRESSION", "$.metrics", "raw aggregates cannot be mixed with cross-grain AGG values")
            for argument in node.args:
                visit(argument)
        elif not isinstance(node, Literal):
            raise AssertionError(node)

    visit(expression)
    if not found:
        raise CompilationError("MULTISTAGE_UNSUPPORTED_EXPRESSION", "$.metrics", "cross-grain expression has no AGG metric references")
    return tuple(found)

def _measure_stage_key(reference: MeasureRef) -> str:
    return f"measure:{reference.dataset}.{reference.metric}"

def _direct_stage_key(metric: Metric) -> str:
    if metric.qualified_name != metric.name:
        return f"measure:{metric.qualified_name}"
    return f"metric:{metric.qualified_name}"

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
