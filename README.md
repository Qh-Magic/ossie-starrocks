# Ossie semantic compiler for StarRocks and Trino

`ossie-starrocks` compiles Apache Ossie semantic requests into StarRocks or Trino `SELECT` statements. It does not load DuckDB or a DuckDB extension.

The compiler fails closed. It returns no SQL when a request has mixed metric grains, a fan-out join, more than one valid join path, an unknown reference, or an expression outside the supported profile.

This project is an early implementation. The supported profile below passes the automated behavior suite and live StarRocks 3.5 and Trino differential workflows, but it has not completed full Apache Ossie conformance testing.

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
    filter="orders.status = 'paid' OR orders.status = 'settled'",
), target="trino")  # omit target to emit StarRocks SQL

print(result.sql)
```

The result contains the SQL, ordered output-column metadata, and the dataset dependencies.

You can run the same operation through the command-line interface.

```sh
ossie-starrocks compile --model sales.json --request request.json
# Trino SQL:
ossie-starrocks compile --model sales.json --request request.json --target trino
```

The request file uses this shape.

```json
{
  "metrics": ["revenue"],
  "dimensions": ["customers.country"],
  "filter": "orders.status = 'paid' OR orders.status = 'settled'",
  "having": [
    {"metric": "revenue", "op": "gt", "value": 100}
  ],
  "limit": 100
}
```

`filter` is the Ossie Layer 3 expression form. It is parsed into a controlled AST, bound to declared semantic fields, and may reference fields from one dataset. The legacy structured `where` array remains available for API compatibility, but a request cannot use both forms.

The command writes SQL to stdout. It writes a JSON diagnostic to stderr and exits with code 2 when compilation fails.

To compile the semantic request and execute it through Trino's official client protocol, use `query`. The JSON result includes the generated SQL, Trino query ID, column metadata, rows, and model dependencies so the execution can be audited.

```sh
ossie-starrocks query \
  --model sales.json \
  --request request.json \
  --trino-url http://localhost:8080 \
  --user analyst
```

The library exposes the same protocol client directly:

```python
from ossie_starrocks import TrinoClient

executed = TrinoClient("http://localhost:8080", "analyst").execute(result.sql)
print(executed.query_id, executed.rows)
```

The client accepts only compiler-style `SELECT` or `WITH` statements and follows Trino `nextUri` pages only on the configured Trino origin. It intentionally does not implement the mutable session state needed by a general-purpose Trino client. Trino query failures are returned as structured diagnostics by the CLI with exit code 3. Authentication and TLS termination remain deployment concerns; do not expose an unauthenticated Trino coordinator publicly.

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
- Dimension-only queries. The planner infers the one requested dimension dataset that can safely reach every other dimension and returns distinct tuples; it rejects requests with no safe anchor.
- Ossie 0.3 `AGG(dataset.metric)` evaluation through independent aggregate CTE stages, including cross-grain grouping when every stage has a safe many-to-one path to the requested dimensions.
- Cross-grain field filters are pushed into every aggregate stage only when each stage can reach the filtered field through a safe many-to-one path. Cross-grain metric filters are evaluated after the stages are joined. Unsafe fan-out traversal fails closed.
- Metrics from different home datasets are evaluated in independent branches and stitched with null-safe full outer joins. A branch-presence marker supplies each metric's true empty-input value, so a missing `COUNT` becomes `0` while a missing or all-NULL `SUM` remains `NULL`.
- Model-scoped metrics may combine aggregate components from different datasets directly, such as `SUM(orders.amount) / COUNT(customers.customer_id)`. Each aggregate call must read exactly one home dataset; the planner stages the components independently and evaluates the scalar expression after stitching.
- `SUM`, `COUNT`, `MIN`, `MAX`, `AVG`, required sample/population standard deviation, and required sample/population variance aggregates. Portable `STDDEV` and `VARIANCE` use sample semantics on both targets, including StarRocks where the same native names otherwise mean population statistics.
- `COUNT(DISTINCT ...)`, `SUM(DISTINCT ...)`, and aggregate `FILTER (WHERE ...)`; filters are lowered to NULL-preserving conditional aggregates for StarRocks.
- Direct lowering for a verified OSSIE SQL scalar subset: required conditional functions (`COALESCE`, `NULLIF`, `IF`, `IFNULL`) plus the Ossie aliases `IFF`, `NVL`, `NVL2`, `ZEROIFNULL`, and `NULLIFZERO`; string composition, splitting, searching, case/trim/length and substring functions; basic and advanced math functions including portable truncation toward zero; comparison functions; date-part extraction; and `DATE_TRUNC`.
- Arithmetic above aggregate expressions. Division is lowered as true division even when both operands are integers: StarRocks uses its `DIVIDE` function and Trino casts the dividend to `DOUBLE`. This matches Ossie fractional semantics instead of Trino integer truncation; as documented by Ossie, the `DOUBLE` route has the usual binary floating-point and 2^53 integer-precision boundary.
- Structured predicates and conditional expressions: comparisons, `AND`, `OR`, `NOT`, `IS [NOT] NULL`, `IN`, `BETWEEN`, `LIKE` and their `NOT` forms, searched `CASE WHEN`, unary signs, and modulo.
- Relationships whose target columns match a declared primary key or unique key.
- Queries with one metric grain, dimensions, field filters, metric filters, and a limit.
- `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `is_null`, and `is_not_null` filters.

The compiler emits deterministic StarRocks SQL with backtick-quoted identifiers and typed literals. It follows safe many-to-one paths with `LEFT JOIN`, as required by the Ossie Layer 3 lowering, so an unmatched home row contributes to the `NULL` dimension group instead of being discarded.

The same proven semantic plan can be rendered as Trino SQL by passing `target="trino"` to `Compiler.compile`, or `--target trino` to the CLI. The Trino target uses double-quoted identifiers and verified dialect rewrites, including `LEFT`/`RIGHT` to `SUBSTRING`, while rejecting functions without a proven lowering. StarRocks remains the default target for backward compatibility.

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

The script compiles the package and runs the library and CLI behavior tests. Live execution remains a separate workflow because it requires Docker and a running StarRocks container.

The manually triggered `live-starrocks` GitHub workflow starts the official `starrocks/allin1-ubuntu:3.5-latest` image, loads deterministic fixture data, and compares generated queries with hand-written StarRocks baseline queries. Its source is [`scripts/live_starrocks.py`](scripts/live_starrocks.py).

The manually triggered `live-trino` workflow runs generated and hand-written baseline queries against the official `trinodb/trino:476` image and its TPCH connector. Its source is [`scripts/live_trino.py`](scripts/live_trino.py).

Research on using the same semantic model through Trino is available in [`docs/research/trino-ossie.md`](docs/research/trino-ossie.md). The recommended route is an external Ossie semantic compiler that emits Trino SQL; neither Apache Ossie nor Trino currently ships a direct Ossie runtime integration.

## Architecture

The compiler normalizes an Ossie document into immutable datasets, fields, metrics, and a directed many-to-one relationship graph. The planner proves one metric grain and one safe path to every required dataset. The renderer accepts only owned expression nodes and emits one StarRocks `SELECT`.

The 0.3 loader validates against the Apache Ossie draft core schema bundled from [`core-spec/ossie-schema.json`](https://github.com/apache/ossie/blob/main/core-spec/ossie-schema.json), then applies semantic-reference and StarRocks capability checks. The expression language is still a documented subset; unsupported constructs fail closed.

The automatic join and branch strategy follows the shared-dimension scope of the draft [`Wide Table Query Interface`](https://github.com/apache/ossie/blob/main/core-spec/wide_table_semantics.md): many-to-one paths use left joins, while independently aggregated metric stages are combined with null-safe full outer joins.

## License

Apache License 2.0. See [LICENSE](LICENSE).
