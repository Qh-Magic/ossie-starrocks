# Apache Ossie semantic compiler for StarRocks

`ossie-starrocks` compiles Apache Ossie semantic requests into StarRocks `SELECT` statements. It is a StarRocks-only project and does not load DuckDB, Trino, or database extensions.

The compiler fails closed. It emits no SQL when a request contains an unknown reference, unsafe fan-out traversal, ambiguous join path, mixed metric grain, or unsupported expression.

This is an early implementation of the evolving Apache Ossie specification. The supported profile is verified by unit tests and a live StarRocks differential workflow, but it is not a claim of full Ossie conformance.

## Install

Python 3.11 or later is required.

```sh
python3 -m pip install -e .
```

Install the optional YAML dependency when models use YAML.

```sh
python3 -m pip install -e '.[yaml]'
```

## Compile a semantic request

```python
from ossie_starrocks import Compiler, Query

compiler = Compiler.from_file("sales.json")
result = compiler.compile(Query(
    metrics=("revenue",),
    dimensions=("customers.country",),
    filter="orders.status = 'paid' OR orders.status = 'settled'",
))

print(result.sql)
```

The public compiler has no target selector. Every result is StarRocks SQL.

The command-line interface accepts the same Layer 3 request shape.

```sh
ossie-starrocks compile --model sales.json --request request.json
```

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

Compilation errors are written to stderr as structured JSON and return exit code 2.

## Safety model

- Models are validated against the bundled Apache Ossie 0.3 draft schema before semantic binding.
- The planner follows declared many-to-one relationships and rejects unsafe reverse fan-out.
- Metrics from different source grains are aggregated independently and stitched with null-safe full outer joins.
- Layer 3 filters are parsed into an owned AST. Raw query SQL is not accepted.
- Only the documented portable expression subset is lowered to StarRocks functions and types.
- Integer aggregate division uses StarRocks `DIVIDE` to preserve fractional semantics.

Static compilation relies on model key declarations. Validate that target data satisfies declared primary and unique keys before production use.

## Verification

Run the local suite and wheel check.

```sh
./scripts/verify.sh
python3 scripts/verify_wheel.py
```

The `test` workflow runs the suite on Python 3.11, 3.12, and 3.13. The manually triggered `live-starrocks` workflow starts the official StarRocks 3.5 container and compares generated queries with hand-written baseline SQL.

## Related project

The Trino compiler, protocol client, semantic gateway, TPCH example, and live Trino verification live in the independent [`Qh-Magic/ossie-trino`](https://github.com/Qh-Magic/ossie-trino) repository.

## License

Apache License 2.0. See [LICENSE](LICENSE).
