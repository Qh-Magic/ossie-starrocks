# Ossie StarRocks compiler

`ossie-starrocks` compiles Apache Ossie semantic requests into StarRocks `SELECT` statements. It does not load DuckDB or a DuckDB extension.

The compiler fails closed. It returns no SQL when a request has mixed metric grains, a fan-out join, more than one valid join path, an unknown reference, or an expression outside the supported profile.

This project is an early implementation. The supported profile below passes the automated behavior suite, but it has not completed live StarRocks differential testing or full Apache Ossie conformance testing.

## Install the package

Use Python 3.11 or later.

```sh
python3 -m pip install -e .
```

To load YAML models, install the optional dependency.

```sh
python3 -m pip install -e '.[yaml]'
```

## Compile a request

```python
from ossie_starrocks import Compiler, FieldFilter, Query

compiler = Compiler.from_file("sales.json")
result = compiler.compile(Query(
    metrics=("revenue",),
    dimensions=("customers.country",),
    where=(FieldFilter("orders.status", "eq", "paid"),),
))

print(result.sql)
```

The result contains the SQL, ordered output-column metadata, and the dataset dependencies.

You can run the same operation through the command-line interface.

```sh
ossie-starrocks compile --model sales.json --request request.json
```

The request file uses this shape.

```json
{
  "metrics": ["revenue"],
  "dimensions": ["customers.country"],
  "where": [
    {"field": "orders.status", "op": "eq", "value": "paid"}
  ],
  "having": [
    {"metric": "revenue", "op": "gt", "value": 100}
  ],
  "limit": 100
}
```

The command writes SQL to stdout. It writes a JSON diagnostic to stderr and exits with code 2 when compilation fails.

## Supported profile

The current release accepts the following model and query features.

- Flat Ossie documents with version `0.2.0.dev0` or `0.3.0.dev0`.
- Full JSON Schema validation against the bundled Apache Ossie 0.3 draft core schema before semantic binding.
- Version-aware name resolution: 0.3 field expressions name source columns, while metric expressions name declared semantic fields.
- Ossie 0.3 dataset metrics may reference physical columns as `source.column`, including the specified `source` field-shadowing rule.
- JSON models and YAML models when PyYAML is installed.
- Table-backed sources with one to three identifier components.
- Dataset fields with `ANSI_SQL`, `OSSIE_SQL_2026`, or 0.3 `expression.sql` expressions.
- Root-level metrics and dataset-scoped metrics.
- Ossie 0.3 `AGG(dataset.metric)` evaluation when every referenced dataset metric has the same proven grain; cross-grain combinations fail closed pending multi-stage planning.
- `SUM`, `COUNT`, `MIN`, `MAX`, and `AVG` aggregates.
- `COUNT(DISTINCT ...)`, `SUM(DISTINCT ...)`, and aggregate `FILTER (WHERE ...)`; filters are lowered to NULL-preserving conditional aggregates for StarRocks.
- Direct StarRocks lowering for a verified OSSIE SQL scalar subset: case/trim/length and substring functions, basic numeric functions, date-part extraction, and `DATE_TRUNC`.
- Arithmetic above aggregate expressions.
- Structured predicates and conditional expressions: comparisons, `AND`, `OR`, `NOT`, `IS [NOT] NULL`, `IN`, `BETWEEN`, `LIKE` and their `NOT` forms, searched `CASE WHEN`, unary signs, and modulo.
- Relationships whose target columns match a declared primary key or unique key.
- Queries with one metric grain, dimensions, field filters, metric filters, and a limit.
- `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `is_null`, and `is_not_null` filters.

The compiler emits deterministic StarRocks SQL with backtick-quoted identifiers and typed literals. It uses `INNER JOIN` for relationships. Rows with a missing target dimension row do not contribute to the result.

## Refused constructs

The compiler rejects these constructs instead of guessing their semantics.

- Query-backed dataset sources.
- Window functions, nested aggregates, arbitrary SQL functions, and raw SQL fragments.
- Mixed-grain metrics.
- Reverse one-to-many traversal and many-to-many joins.
- Directed relationship cycles and ambiguous relationship paths.
- Relationship targets without a declared primary key or unique key.
- Timezone-aware datetime values, floating-point request values, backslashes, and NUL bytes in string values.

Static compilation trusts the model's key declarations. It cannot prove that StarRocks data satisfies those declarations. Validate key uniqueness against the target data before production use.

## Run the verification suite

```sh
./scripts/verify.sh
```

The script compiles the package and runs the library and CLI behavior tests. A live StarRocks differential suite remains a separate release requirement.

## Architecture

The compiler normalizes an Ossie document into immutable datasets, fields, metrics, and a directed many-to-one relationship graph. The planner proves one metric grain and one safe path to every required dataset. The renderer accepts only owned expression nodes and emits one StarRocks `SELECT`.

The 0.3 loader validates against the Apache Ossie draft core schema bundled from [`core-spec/ossie-schema.json`](https://github.com/apache/ossie/blob/main/core-spec/ossie-schema.json), then applies semantic-reference and StarRocks capability checks. The expression language is still a documented subset; unsupported constructs fail closed.

## License

Apache License 2.0. See [LICENSE](LICENSE).
