from __future__ import annotations

from dataclasses import dataclass
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from ._expressions import Binary, Call, Column, Expr, ExpressionError, Star, contains_call, parse, walk
from ._starrocks import SUPPORTED_AGGREGATES


class ModelError(ValueError):
    def __init__(self, code: str, location: str, message: str):
        super().__init__(message)
        self.code = code
        self.location = location


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_VERSIONS = frozenset({"0.2.0.dev0", "0.3.0.dev0"})


@dataclass(frozen=True)
class TableName:
    parts: tuple[str, ...]


@dataclass(frozen=True)
class Dataset:
    name: str
    source: TableName
    primary_key: tuple[str, ...]
    unique_keys: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Field:
    name: str
    dataset: str
    expression: Expr


@dataclass(frozen=True)
class Metric:
    name: str
    qualified_name: str
    root: str
    expression: Expr


@dataclass(frozen=True)
class Relationship:
    name: str
    many: str
    one: str
    keys: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BoundModel:
    name: str
    version: str
    datasets: tuple[Dataset, ...]
    fields: tuple[Field, ...]
    metrics: tuple[Metric, ...]
    relationships: tuple[Relationship, ...]
    dataset_by_name: Mapping[str, Dataset]
    field_by_name: Mapping[str, Field]
    metric_by_name: Mapping[str, Metric]
    outgoing: Mapping[str, tuple[Relationship, ...]]


def load_document(text: str, format: str) -> BoundModel:
    if format == "json":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise ModelError("INVALID_MODEL", "$", str(error)) from error
    elif format == "yaml":
        try:
            import yaml
        except ImportError as error:
            raise ModelError("YAML_UNAVAILABLE", "$", "install the yaml optional dependency") from error
        raw = yaml.safe_load(text)
    else:
        raise ModelError("INVALID_FORMAT", "$", f"unsupported format {format!r}")
    if not isinstance(raw, dict):
        raise ModelError("INVALID_MODEL", "$", "model must be an object")
    return bind_model(raw)


def bind_model(raw: dict[str, Any]) -> BoundModel:
    version = _required_string(raw, "version", "$.version")
    if version not in _SUPPORTED_VERSIONS:
        raise ModelError("UNSUPPORTED_MODEL_VERSION", "$.version", f"unsupported Ossie version {version!r}")
    name = _required_identifier(raw, "name", "$.name")
    raw_datasets = raw.get("datasets")
    if not isinstance(raw_datasets, list) or not raw_datasets:
        raise ModelError("INVALID_MODEL", "$.datasets", "datasets must be a non-empty array")

    datasets: list[Dataset] = []
    fields: list[Field] = []
    dataset_metrics: list[tuple[dict[str, Any], str, str]] = []
    dataset_names: set[str] = set()
    field_names: set[str] = set()
    for index, item in enumerate(raw_datasets):
        location = f"$.datasets[{index}]"
        if not isinstance(item, dict):
            raise ModelError("INVALID_MODEL", location, "dataset must be an object")
        dataset_name = _required_identifier(item, "name", f"{location}.name")
        if dataset_name in dataset_names:
            raise ModelError("DUPLICATE_NAME", f"{location}.name", f"duplicate dataset {dataset_name!r}")
        dataset_names.add(dataset_name)
        source = _table_name(_required_string(item, "source", f"{location}.source"), f"{location}.source")
        primary_key = _identifier_list(item.get("primary_key", []), f"{location}.primary_key")
        raw_unique = item.get("unique_keys", [])
        if not isinstance(raw_unique, list):
            raise ModelError("INVALID_MODEL", f"{location}.unique_keys", "unique_keys must be an array")
        unique_keys = tuple(_identifier_list(value, f"{location}.unique_keys") for value in raw_unique)
        datasets.append(Dataset(dataset_name, source, primary_key, unique_keys))

        raw_fields = item.get("fields", [])
        if not isinstance(raw_fields, list):
            raise ModelError("INVALID_MODEL", f"{location}.fields", "fields must be an array")
        for field_index, raw_field in enumerate(raw_fields):
            field_location = f"{location}.fields[{field_index}]"
            if not isinstance(raw_field, dict):
                raise ModelError("INVALID_MODEL", field_location, "field must be an object")
            field_name = _required_identifier(raw_field, "name", f"{field_location}.name")
            qualified = f"{dataset_name}.{field_name}"
            if qualified in field_names:
                raise ModelError("DUPLICATE_NAME", f"{field_location}.name", f"duplicate field {qualified!r}")
            field_names.add(qualified)
            expression = _parse_expression(raw_field.get("expression"), field_location)
            if contains_call(expression, SUPPORTED_AGGREGATES):
                raise ModelError("INVALID_FIELD_EXPRESSION", field_location, "field expression cannot aggregate")
            if any(isinstance(node, Star) for node in walk(expression)):
                raise ModelError("INVALID_FIELD_EXPRESSION", field_location, "field expression cannot contain *")
            expression = _scope_expression(expression, dataset_name, dataset_names | {dataset_name})
            for node in walk(expression):
                if isinstance(node, Column) and node.dataset != dataset_name:
                    raise ModelError(
                        "CROSS_DATASET_FIELD",
                        field_location,
                        "computed fields cannot reference another dataset in the supported profile",
                    )
            fields.append(Field(field_name, dataset_name, expression))
        raw_metrics = item.get("metrics", [])
        if not isinstance(raw_metrics, list):
            raise ModelError("INVALID_MODEL", f"{location}.metrics", "metrics must be an array")
        for metric_index, raw_metric in enumerate(raw_metrics):
            if not isinstance(raw_metric, dict):
                raise ModelError("INVALID_MODEL", f"{location}.metrics[{metric_index}]", "metric must be an object")
            dataset_metrics.append((raw_metric, dataset_name, f"{location}.metrics[{metric_index}]"))

    dataset_by_name = {dataset.name: dataset for dataset in datasets}
    field_by_name = {f"{field.dataset}.{field.name}": field for field in fields}
    fields, field_by_name = _expand_fields(fields, field_by_name)
    _validate_expression_columns(fields, dataset_by_name, field_by_name)

    metrics: list[Metric] = []
    for raw_metric, root, location in dataset_metrics:
        metrics.append(_bind_metric(raw_metric, location, root, dataset_by_name, field_by_name))
    raw_root_metrics = raw.get("metrics", [])
    if not isinstance(raw_root_metrics, list):
        raise ModelError("INVALID_MODEL", "$.metrics", "metrics must be an array")
    for metric_index, raw_metric in enumerate(raw_root_metrics):
        location = f"$.metrics[{metric_index}]"
        if not isinstance(raw_metric, dict):
            raise ModelError("INVALID_MODEL", location, "metric must be an object")
        metrics.append(_bind_metric(raw_metric, location, None, dataset_by_name, field_by_name))
    metric_by_name = _metric_index(metrics)

    relationships = _bind_relationships(raw.get("relationships", []), dataset_by_name, field_by_name)
    _reject_cycles(dataset_by_name, relationships)
    outgoing: dict[str, list[Relationship]] = {dataset: [] for dataset in dataset_by_name}
    for relationship in relationships:
        outgoing[relationship.many].append(relationship)
    frozen_outgoing = {name: tuple(sorted(edges, key=lambda edge: edge.name)) for name, edges in outgoing.items()}
    return BoundModel(
        name,
        version,
        tuple(datasets),
        tuple(fields),
        tuple(metrics),
        tuple(relationships),
        MappingProxyType(dataset_by_name),
        MappingProxyType(field_by_name),
        MappingProxyType(metric_by_name),
        MappingProxyType(frozen_outgoing),
    )


def _bind_metric(raw: dict[str, Any], location: str, declared_root: str | None, datasets: Mapping[str, Dataset], fields: Mapping[str, Field]) -> Metric:
    name = _required_identifier(raw, "name", f"{location}.name")
    expression = _parse_expression(raw.get("expression"), location)
    if not contains_call(expression, SUPPORTED_AGGREGATES):
        raise ModelError("INVALID_METRIC_EXPRESSION", location, "metric must contain an aggregate")
    expression = _scope_expression(expression, declared_root, set(datasets))
    expression = _expand_expression(expression, fields, (), None)
    _validate_aggregate_shape(expression, location)
    roots = {node.dataset for node in walk(expression) if isinstance(node, Column)}
    if None in roots:
        raise ModelError("UNKNOWN_GRAIN", location, "metric contains an unscoped column")
    if declared_root is not None:
        if roots and roots != {declared_root}:
            raise ModelError("MIXED_GRAIN", location, "dataset metric references another dataset")
        root = declared_root
    elif len(roots) == 1:
        root = next(iter(roots))
        assert root is not None
    else:
        raise ModelError("UNKNOWN_GRAIN" if not roots else "MIXED_GRAIN", location, "metric must have exactly one dataset grain")
    for node in walk(expression):
        if isinstance(node, Column) and f"{node.dataset}.{node.name}" not in fields:
            raise ModelError("UNKNOWN_FIELD", location, f"unknown field {node.dataset}.{node.name}")
    return Metric(name, f"{root}.{name}" if declared_root else name, root, expression)


def _validate_aggregate_shape(expression: Expr, location: str) -> None:
    def visit(node: Expr, inside_aggregate: bool) -> None:
        if isinstance(node, Call):
            if node.name not in SUPPORTED_AGGREGATES:
                raise ModelError("UNSUPPORTED_EXPRESSION", location, f"unsupported function {node.name}")
            if inside_aggregate:
                raise ModelError("INVALID_METRIC_EXPRESSION", location, "nested aggregate")
            if len(node.args) != 1:
                raise ModelError("INVALID_METRIC_EXPRESSION", location, f"{node.name} expects one argument")
            for argument in node.args:
                if isinstance(argument, Star):
                    if node.name != "COUNT":
                        raise ModelError("INVALID_METRIC_EXPRESSION", location, "only COUNT may use *")
                else:
                    if any(isinstance(child, Star) for child in walk(argument)):
                        raise ModelError("INVALID_METRIC_EXPRESSION", location, "* must be the direct argument of COUNT")
                    visit(argument, True)
        elif isinstance(node, Column) and not inside_aggregate:
            raise ModelError("INVALID_METRIC_EXPRESSION", location, "ungrouped field outside aggregate")
        elif isinstance(node, Star):
            raise ModelError("INVALID_METRIC_EXPRESSION", location, "* must be the direct argument of COUNT")
        elif hasattr(node, "left"):
            visit(node.left, inside_aggregate)  # type: ignore[attr-defined]
            visit(node.right, inside_aggregate)  # type: ignore[attr-defined]

    visit(expression, False)


def _expand_fields(fields: list[Field], field_by_name: Mapping[str, Field]) -> tuple[list[Field], dict[str, Field]]:
    expanded: dict[str, Field] = {}

    def expand(key: str, stack: tuple[str, ...]) -> Field:
        if key in expanded:
            return expanded[key]
        if key in stack:
            cycle = " -> ".join((*stack, key))
            raise ModelError("EXPRESSION_CYCLE", f"$.fields.{key}", f"computed field cycle: {cycle}")
        field = field_by_name[key]
        expression = expand_expression(field.expression, (*stack, key), key)
        result = Field(field.name, field.dataset, expression)
        expanded[key] = result
        return result

    def expand_expression(expression: Expr, stack: tuple[str, ...], owner: str) -> Expr:
        if isinstance(expression, Column) and expression.dataset is not None:
            key = f"{expression.dataset}.{expression.name}"
            if key == owner:
                return expression
            if key in field_by_name:
                return expand(key, stack).expression
            return expression
        if isinstance(expression, Binary):
            return Binary(expression.op, expand_expression(expression.left, stack, owner), expand_expression(expression.right, stack, owner))
        if isinstance(expression, Call):
            return Call(expression.name, tuple(expand_expression(argument, stack, owner) for argument in expression.args))
        return expression

    ordered = [expand(f"{field.dataset}.{field.name}", ()) for field in fields]
    return ordered, {f"{field.dataset}.{field.name}": field for field in ordered}


def _expand_expression(expression: Expr, field_by_name: Mapping[str, Field], stack: tuple[str, ...], owner: str | None) -> Expr:
    if isinstance(expression, Column) and expression.dataset is not None:
        key = f"{expression.dataset}.{expression.name}"
        if key == owner:
            return expression
        field = field_by_name.get(key)
        if field is None:
            return expression
        if key in stack:
            cycle = " -> ".join((*stack, key))
            raise ModelError("EXPRESSION_CYCLE", f"$.fields.{key}", f"computed field cycle: {cycle}")
        return _expand_expression(field.expression, field_by_name, (*stack, key), key)
    if isinstance(expression, Binary):
        return Binary(expression.op, _expand_expression(expression.left, field_by_name, stack, owner), _expand_expression(expression.right, field_by_name, stack, owner))
    if isinstance(expression, Call):
        return Call(expression.name, tuple(_expand_expression(argument, field_by_name, stack, owner) for argument in expression.args))
    return expression


def _bind_relationships(raw_relationships: Any, datasets: Mapping[str, Dataset], fields: Mapping[str, Field]) -> list[Relationship]:
    if not isinstance(raw_relationships, list):
        raise ModelError("INVALID_MODEL", "$.relationships", "relationships must be an array")
    relationships: list[Relationship] = []
    names: set[str] = set()
    for index, item in enumerate(raw_relationships):
        location = f"$.relationships[{index}]"
        if not isinstance(item, dict):
            raise ModelError("INVALID_MODEL", location, "relationship must be an object")
        name = _required_identifier(item, "name", f"{location}.name")
        if name in names:
            raise ModelError("DUPLICATE_NAME", f"{location}.name", f"duplicate relationship {name!r}")
        names.add(name)
        many = _required_identifier(item, "from", f"{location}.from")
        one = _required_identifier(item, "to", f"{location}.to")
        if many not in datasets or one not in datasets:
            raise ModelError("UNKNOWN_DATASET", location, "relationship references an unknown dataset")
        from_columns = _identifier_list(item.get("from_columns"), f"{location}.from_columns")
        to_columns = _identifier_list(item.get("to_columns"), f"{location}.to_columns")
        if not from_columns or len(from_columns) != len(to_columns):
            raise ModelError("INVALID_RELATIONSHIP", location, "relationship keys must be non-empty and paired")
        for column in from_columns:
            if f"{many}.{column}" not in fields:
                raise ModelError("UNKNOWN_FIELD", location, f"unknown field {many}.{column}")
            if not _is_direct_physical_field(fields[f"{many}.{column}"], many, column):
                raise ModelError("COMPUTED_RELATIONSHIP_KEY", location, "relationship keys must be direct physical fields")
        for column in to_columns:
            if f"{one}.{column}" not in fields:
                raise ModelError("UNKNOWN_FIELD", location, f"unknown field {one}.{column}")
            if not _is_direct_physical_field(fields[f"{one}.{column}"], one, column):
                raise ModelError("COMPUTED_RELATIONSHIP_KEY", location, "relationship keys must be direct physical fields")
        unique_keys = {datasets[one].primary_key, *datasets[one].unique_keys}
        if tuple(to_columns) not in unique_keys:
            raise ModelError("UNPROVEN_CARDINALITY", location, "target columns are not a declared primary or unique key")
        relationships.append(Relationship(name, many, one, tuple(zip(from_columns, to_columns))))
    return relationships


def _is_direct_physical_field(field: Field, dataset: str, column: str) -> bool:
    expression = field.expression
    return isinstance(expression, Column) and expression.dataset == dataset and expression.name == column


def _reject_cycles(datasets: Mapping[str, Dataset], relationships: list[Relationship]) -> None:
    outgoing: dict[str, list[str]] = {name: [] for name in datasets}
    for relationship in relationships:
        outgoing[relationship.many].append(relationship.one)
    state: dict[str, int] = {}

    def visit(name: str) -> None:
        if state.get(name) == 1:
            raise ModelError("RELATIONSHIP_CYCLE", "$.relationships", f"relationship cycle includes {name}")
        if state.get(name) == 2:
            return
        state[name] = 1
        for target in outgoing[name]:
            visit(target)
        state[name] = 2

    for dataset in datasets:
        visit(dataset)


def _parse_expression(raw: Any, location: str) -> Expr:
    sql: str | None = None
    if isinstance(raw, str):
        sql = raw
    elif isinstance(raw, dict):
        if isinstance(raw.get("sql"), str):
            sql = raw["sql"]
        else:
            dialects = raw.get("dialects")
            if isinstance(dialects, list):
                for alternative in dialects:
                    if isinstance(alternative, dict) and alternative.get("dialect") in {"ANSI_SQL", "OSSIE_SQL_2026"} and isinstance(alternative.get("expression"), str):
                        sql = alternative["expression"]
                        break
    if sql is None:
        raise ModelError("UNSUPPORTED_DIALECT", f"{location}.expression", "no supported SQL expression")
    try:
        return parse(sql)
    except ExpressionError as error:
        raise ModelError("UNSUPPORTED_EXPRESSION", f"{location}.expression", str(error)) from error


def _scope_expression(expression: Expr, default_dataset: str | None, datasets: set[str]) -> Expr:
    from ._expressions import Binary, Call, Literal, Star

    if isinstance(expression, Column):
        dataset = expression.dataset or default_dataset
        if dataset is not None and dataset not in datasets:
            raise ModelError("UNKNOWN_DATASET", "$.expression", f"unknown dataset {dataset!r}")
        return Column(dataset, expression.name)
    if isinstance(expression, Binary):
        return Binary(expression.op, _scope_expression(expression.left, default_dataset, datasets), _scope_expression(expression.right, default_dataset, datasets))
    if isinstance(expression, Call):
        return Call(expression.name, tuple(_scope_expression(argument, default_dataset, datasets) for argument in expression.args))
    if isinstance(expression, (Literal, Star)):
        return expression
    raise AssertionError(expression)


def _validate_expression_columns(fields: list[Field], datasets: Mapping[str, Dataset], field_by_name: Mapping[str, Field]) -> None:
    for field in fields:
        for node in walk(field.expression):
            if isinstance(node, Column):
                if node.dataset not in datasets or f"{node.dataset}.{node.name}" not in field_by_name:
                    raise ModelError("UNKNOWN_FIELD", f"$.datasets.{field.dataset}.{field.name}", f"unknown field {node.dataset}.{node.name}")


def _metric_index(metrics: list[Metric]) -> dict[str, Metric]:
    index: dict[str, Metric] = {}
    simple: dict[str, list[Metric]] = {}
    for metric in metrics:
        if metric.qualified_name in index:
            raise ModelError("DUPLICATE_NAME", "$.metrics", f"duplicate metric {metric.qualified_name!r}")
        index[metric.qualified_name] = metric
        simple.setdefault(metric.name, []).append(metric)
    for name, candidates in simple.items():
        if len(candidates) == 1:
            index.setdefault(name, candidates[0])
    return index


def _required_string(raw: Mapping[str, Any], key: str, location: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ModelError("INVALID_MODEL", location, f"{key} must be a non-empty string")
    return value


def _required_identifier(raw: Mapping[str, Any], key: str, location: str) -> str:
    return _identifier(_required_string(raw, key, location), location)


def _identifier(value: str, location: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ModelError("INVALID_IDENTIFIER", location, f"invalid identifier {value!r}")
    return value


def _identifier_list(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ModelError("INVALID_MODEL", location, "expected an array of identifiers")
    return tuple(_identifier(item, location) if isinstance(item, str) else _raise_identifier(location) for item in value)


def _raise_identifier(location: str) -> str:
    raise ModelError("INVALID_MODEL", location, "expected an identifier string")


def _table_name(value: str, location: str) -> TableName:
    parts = tuple(value.split("."))
    if not 1 <= len(parts) <= 3:
        raise ModelError("INVALID_SOURCE", location, "table source must contain one to three identifiers")
    return TableName(tuple(_identifier(part, location) for part in parts))
