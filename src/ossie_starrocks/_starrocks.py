from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ._expressions import Between, Binary, Call, Case, Column, Expr, FilteredAggregate, InList, IsNull, Like, Literal as ExprLiteral, MeasureRef, Star, Unary
from ._model import BoundModel, Metric
from ._profile import SUPPORTED_AGGREGATES, SUPPORTED_SCALAR_FUNCTIONS
from .api import CompilationError, CompiledQuery, OutputColumn
from .planning import MultiStageQueryPlan, PlannedFilter, QueryPlan

def render_query(model: BoundModel, plan: QueryPlan | MultiStageQueryPlan) -> CompiledQuery:
    if isinstance(plan, MultiStageQueryPlan):
        return _render_multistage_query(model, plan)
    root = plan.joins.root
    aliases = {root: "t0"}
    for edge in plan.joins.edges:
        aliases.setdefault(edge.one, f"t{len(aliases)}")
    projections, columns = [], []
    for i, field in enumerate(plan.dimensions):
        alias = f"d{i}"
        projections.append(f"{_render_expr(field.expression, aliases)} AS `{alias}`")
        columns.append(OutputColumn(f"{field.dataset}.{field.name}", alias))
    for i, metric in enumerate(plan.metrics):
        alias = f"m{i}"
        projections.append(f"{_render_expr(metric.expression, aliases)} AS `{alias}`")
        columns.append(OutputColumn(metric.name, alias))
    lines = ["SELECT " + ", ".join(projections), f"FROM {_render_table(model, root)} AS {aliases[root]}"]
    for edge in plan.joins.edges:
        conditions = " AND ".join(f"{aliases[edge.many]}.`{left}` = {aliases[edge.one]}.`{right}`" for left, right in edge.keys)
        lines.append(f"INNER JOIN {_render_table(model, edge.one)} AS {aliases[edge.one]} ON {conditions}")
    if plan.where:
        lines.append("WHERE " + " AND ".join(_render_filter(item, aliases) for item in plan.where))
    if plan.dimensions:
        lines.append("GROUP BY " + ", ".join(_render_expr(field.expression, aliases) for field in plan.dimensions))
    if plan.having:
        lines.append("HAVING " + " AND ".join(_render_filter(item, aliases) for item in plan.having))
    if plan.limit is not None:
        lines.append(f"LIMIT {plan.limit}")
    dependencies = tuple(dict.fromkeys([root, *(edge.one for edge in plan.joins.edges)]))
    return CompiledQuery("\n".join(lines), tuple(columns), dependencies)

def _render_multistage_query(model: BoundModel, plan: MultiStageQueryPlan) -> CompiledQuery:
    stage_names = {
        (stage.reference.dataset, stage.reference.metric): f"s{index}"
        for index, stage in enumerate(plan.stages)
    }
    ctes = []
    for stage in plan.stages:
        reference = stage.reference
        stage_name = stage_names[(reference.dataset, reference.metric)]
        aliases = {stage.joins.root: "t0"}
        for edge in stage.joins.edges:
            aliases.setdefault(edge.one, f"t{len(aliases)}")
        projections = [
            *(
                f"{_render_expr(field.expression, aliases)} AS `k{index}`"
                for index, field in enumerate(plan.dimensions)
            ),
            f"{_render_expr(reference.expression, aliases)} AS `value`",
        ]
        lines = [
            f"{stage_name} AS (SELECT {', '.join(projections)}",
            f"FROM {_render_table(model, stage.joins.root)} AS t0",
        ]
        for edge in stage.joins.edges:
            conditions = " AND ".join(
                f"{aliases[edge.many]}.`{left}` = {aliases[edge.one]}.`{right}`"
                for left, right in edge.keys
            )
            lines.append(f"INNER JOIN {_render_table(model, edge.one)} AS {aliases[edge.one]} ON {conditions}")
        if plan.where:
            lines.append("WHERE " + " AND ".join(_render_filter(item, aliases) for item in plan.where))
        if plan.dimensions:
            lines.append("GROUP BY " + ", ".join(_render_expr(field.expression, aliases) for field in plan.dimensions))
        lines[-1] += ")"
        ctes.append("\n".join(lines))
    projections = []
    columns = []
    ordered_stage_names = [stage_names[(stage.reference.dataset, stage.reference.metric)] for stage in plan.stages]
    for index, field in enumerate(plan.dimensions):
        alias = f"d{index}"
        keys = ", ".join(f"{stage_name}.`k{index}`" for stage_name in ordered_stage_names)
        projections.append(f"COALESCE({keys}) AS `{alias}`")
        columns.append(OutputColumn(f"{field.dataset}.{field.name}", alias))
    for index, metric in enumerate(plan.metrics):
        alias = f"m{index}"
        projections.append(f"{_render_multistage_expr(metric.expression, stage_names)} AS `{alias}`")
        columns.append(OutputColumn(metric.name, alias))
    lines = ["WITH " + ",\n".join(ctes), "SELECT " + ", ".join(projections)]
    if not plan.dimensions:
        lines.append("FROM " + " CROSS JOIN ".join(ordered_stage_names))
    else:
        from_clause = f"FROM {ordered_stage_names[0]}"
        joined = [ordered_stage_names[0]]
        for stage_name in ordered_stage_names[1:]:
            conditions = []
            for index in range(len(plan.dimensions)):
                prior = f"COALESCE({', '.join(f'{name}.`k{index}`' for name in joined)})"
                current = f"{stage_name}.`k{index}`"
                conditions.append(f"({prior} = {current} OR ({prior} IS NULL AND {current} IS NULL))")
            from_clause += f"\nFULL OUTER JOIN {stage_name} ON " + " AND ".join(conditions)
            joined.append(stage_name)
        lines.append(from_clause)
    if plan.having:
        lines.append(
            "WHERE "
            + " AND ".join(_render_multistage_filter(item, stage_names) for item in plan.having)
        )
    if plan.limit is not None:
        lines.append(f"LIMIT {plan.limit}")
    dependencies = tuple(
        dict.fromkeys(
            dependency
            for stage in plan.stages
            for dependency in (stage.joins.root, *(edge.one for edge in stage.joins.edges))
        )
    )
    return CompiledQuery("\n".join(lines), tuple(columns), dependencies)

def _render_multistage_filter(item: PlannedFilter, stages: dict[tuple[str, str], str]) -> str:
    assert isinstance(item.expression_owner, Metric)
    left = _render_multistage_expr(item.expression_owner.expression, stages)
    if item.op == "is_null":
        return f"{left} IS NULL"
    if item.op == "is_not_null":
        return f"{left} IS NOT NULL"
    operator = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[item.op]
    return f"{left} {operator} {_literal(item.value)}"

def _render_multistage_expr(expression: Expr, stages: dict[tuple[str, str], str]) -> str:
    if isinstance(expression, MeasureRef):
        return f"{stages[(expression.dataset, expression.metric)]}.`value`"
    if isinstance(expression, ExprLiteral):
        return _literal(expression.value)
    if isinstance(expression, Binary):
        return f"({_render_multistage_expr(expression.left, stages)} {expression.op} {_render_multistage_expr(expression.right, stages)})"
    if isinstance(expression, Unary):
        separator = " " if expression.op == "NOT" else ""
        return f"({expression.op}{separator}{_render_multistage_expr(expression.operand, stages)})"
    if isinstance(expression, IsNull):
        suffix = "IS NOT NULL" if expression.negated else "IS NULL"
        return f"({_render_multistage_expr(expression.operand, stages)} {suffix})"
    if isinstance(expression, Case):
        branches = " ".join(
            f"WHEN {_render_multistage_expr(condition, stages)} THEN {_render_multistage_expr(value, stages)}"
            for condition, value in expression.branches
        )
        otherwise = f" ELSE {_render_multistage_expr(expression.otherwise, stages)}" if expression.otherwise is not None else ""
        return f"CASE {branches}{otherwise} END"
    if isinstance(expression, Between):
        operator = "NOT BETWEEN" if expression.negated else "BETWEEN"
        return f"({_render_multistage_expr(expression.operand, stages)} {operator} {_render_multistage_expr(expression.lower, stages)} AND {_render_multistage_expr(expression.upper, stages)})"
    if isinstance(expression, InList):
        operator = "NOT IN" if expression.negated else "IN"
        values = ", ".join(_render_multistage_expr(value, stages) for value in expression.values)
        return f"({_render_multistage_expr(expression.operand, stages)} {operator} ({values}))"
    if isinstance(expression, Like):
        operator = "NOT LIKE" if expression.negated else "LIKE"
        return f"({_render_multistage_expr(expression.operand, stages)} {operator} {_render_multistage_expr(expression.pattern, stages)})"
    if isinstance(expression, Call) and expression.name in SUPPORTED_SCALAR_FUNCTIONS:
        return f"{expression.name}({', '.join(_render_multistage_expr(argument, stages) for argument in expression.args)})"
    raise CompilationError("MULTISTAGE_UNSUPPORTED_EXPRESSION", "$.metrics", "unsupported node in cross-grain expression")

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
    if isinstance(expression, Unary):
        separator = " " if expression.op == "NOT" else ""
        return f"({expression.op}{separator}{_render_expr(expression.operand, aliases)})"
    if isinstance(expression, IsNull):
        suffix = "IS NOT NULL" if expression.negated else "IS NULL"
        return f"({_render_expr(expression.operand, aliases)} {suffix})"
    if isinstance(expression, Case):
        branches = " ".join(
            f"WHEN {_render_expr(condition, aliases)} THEN {_render_expr(value, aliases)}"
            for condition, value in expression.branches
        )
        otherwise = f" ELSE {_render_expr(expression.otherwise, aliases)}" if expression.otherwise is not None else ""
        return f"CASE {branches}{otherwise} END"
    if isinstance(expression, Between):
        operator = "NOT BETWEEN" if expression.negated else "BETWEEN"
        return f"({_render_expr(expression.operand, aliases)} {operator} {_render_expr(expression.lower, aliases)} AND {_render_expr(expression.upper, aliases)})"
    if isinstance(expression, InList):
        operator = "NOT IN" if expression.negated else "IN"
        values = ", ".join(_render_expr(value, aliases) for value in expression.values)
        return f"({_render_expr(expression.operand, aliases)} {operator} ({values}))"
    if isinstance(expression, Like):
        operator = "NOT LIKE" if expression.negated else "LIKE"
        return f"({_render_expr(expression.operand, aliases)} {operator} {_render_expr(expression.pattern, aliases)})"
    if isinstance(expression, Call):
        if expression.name not in SUPPORTED_AGGREGATES and expression.name not in SUPPORTED_SCALAR_FUNCTIONS:
            raise CompilationError("UNSUPPORTED_EXPRESSION", "$.expression", f"unsupported function {expression.name}")
        distinct = "DISTINCT " if expression.distinct else ""
        return f"{expression.name}({distinct}{', '.join(_render_expr(arg, aliases) for arg in expression.args)})"
    if isinstance(expression, FilteredAggregate):
        aggregate = expression.aggregate
        predicate = _render_expr(expression.predicate, aliases)
        argument = aggregate.args[0]
        if isinstance(argument, Star):
            conditional = f"CASE WHEN {predicate} THEN 1 END"
        else:
            conditional = f"CASE WHEN {predicate} THEN {_render_expr(argument, aliases)} END"
        distinct = "DISTINCT " if aggregate.distinct else ""
        return f"{aggregate.name}({distinct}{conditional})"
    if isinstance(expression, MeasureRef):
        return _render_expr(expression.expression, aliases)
    raise AssertionError(expression)

def _render_filter(item: PlannedFilter, aliases: dict[str, str]) -> str:
    left = _render_expr(item.expression_owner.expression, aliases)
    if item.op == "is_null":
        return f"{left} IS NULL"
    if item.op == "is_not_null":
        return f"{left} IS NOT NULL"
    operator = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[item.op]
    return f"{left} {operator} {_literal(item.value)}"

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
