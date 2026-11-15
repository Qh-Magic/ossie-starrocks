from __future__ import annotations

import json
import os
import subprocess

from ossie_starrocks import Compiler, Query


CONTAINER = os.environ.get("TRINO_CONTAINER", "ossie-trino-live")


def sql(statement: str) -> tuple[tuple[str, ...], ...]:
    completed = subprocess.run(
        [
            "docker", "exec", CONTAINER, "trino",
            "--output-format", "TSV", "--execute", statement,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Trino command failed ({completed.returncode})\n"
            f"SQL:\n{statement}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return tuple(tuple(line.split("\t")) for line in completed.stdout.splitlines())


def assert_same(name: str, generated: str, baseline: str) -> None:
    actual = sql(generated)
    expected = sql(baseline)
    if actual != expected:
        raise AssertionError(
            f"{name} mismatch\ngenerated SQL:\n{generated}\n"
            f"expected rows: {expected!r}\nactual rows: {actual!r}"
        )
    print(f"PASS {name}: {actual!r}")


model = {
    "version": "0.3.0.dev0",
    "name": "trino_tpch",
    "datasets": [
        {
            "name": "orders",
            "source": "tpch.tiny.orders",
            "fields": [
                {"name": "custkey", "expression": {"sql": "custkey"}},
                {"name": "orderstatus", "expression": {"sql": "orderstatus"}},
                {"name": "totalprice", "expression": {"sql": "totalprice"}},
            ],
        },
        {
            "name": "customers",
            "source": "tpch.tiny.customer",
            "primary_key": ["custkey"],
            "fields": [
                {"name": "custkey", "expression": {"sql": "custkey"}},
                {"name": "mktsegment", "expression": {"sql": "mktsegment"}},
            ],
        },
    ],
    "relationships": [
        {
            "name": "orders_customer",
            "from": "orders",
            "to": "customers",
            "from_columns": ["custkey"],
            "to_columns": ["custkey"],
        }
    ],
    "metrics": [
        {"name": "revenue", "expression": {"sql": "SUM(orders.totalprice)"}},
        {
            "name": "revenue_per_customer",
            "expression": {
                "sql": "NVL(SUM(orders.totalprice), 0) / NULLIFZERO(COUNT(customers.custkey))"
            },
        },
    ],
}

compiler = Compiler.from_text(json.dumps(model), format="json")

joined = compiler.compile(
    Query(metrics=("revenue",), dimensions=("customers.mktsegment",)),
    target="trino",
)
assert_same(
    "joined aggregate",
    joined.sql + " ORDER BY d0",
    """
    SELECT c.mktsegment, SUM(o.totalprice)
    FROM tpch.tiny.orders o
    LEFT JOIN tpch.tiny.customer c ON o.custkey = c.custkey
    GROUP BY c.mktsegment
    ORDER BY c.mktsegment
    """,
)

cross_grain = compiler.compile(
    Query(metrics=("revenue_per_customer",), dimensions=("customers.mktsegment",)),
    target="trino",
)
assert_same(
    "cross-grain conditional metric",
    cross_grain.sql + " ORDER BY d0",
    """
    WITH order_stage AS (
        SELECT c.mktsegment, SUM(o.totalprice) AS value, 1 AS present
        FROM tpch.tiny.orders o
        LEFT JOIN tpch.tiny.customer c ON o.custkey = c.custkey
        GROUP BY c.mktsegment
    ), customer_stage AS (
        SELECT mktsegment, COUNT(custkey) AS value, 1 AS present
        FROM tpch.tiny.customer
        GROUP BY mktsegment
    )
    SELECT COALESCE(order_stage.mktsegment, customer_stage.mktsegment),
           COALESCE(
               CASE WHEN order_stage.present IS NULL THEN NULL ELSE order_stage.value END,
               0
           ) / NULLIF(
               CASE WHEN customer_stage.present IS NULL THEN 0 ELSE customer_stage.value END,
               0
           )
    FROM order_stage
    FULL OUTER JOIN customer_stage
      ON order_stage.mktsegment IS NOT DISTINCT FROM customer_stage.mktsegment
    ORDER BY COALESCE(order_stage.mktsegment, customer_stage.mktsegment)
    """,
)

filtered = compiler.compile(
    Query(
        metrics=("revenue",),
        dimensions=("customers.mktsegment",),
        filter="orders.orderstatus = 'F'",
    ),
    target="trino",
)
assert_same(
    "bound Layer 3 filter",
    filtered.sql + " ORDER BY d0",
    """
    SELECT c.mktsegment, SUM(o.totalprice)
    FROM tpch.tiny.orders o
    LEFT JOIN tpch.tiny.customer c ON o.custkey = c.custkey
    WHERE o.orderstatus = 'F'
    GROUP BY c.mktsegment
    ORDER BY c.mktsegment
    """,
)
