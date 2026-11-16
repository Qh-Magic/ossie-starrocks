from __future__ import annotations

import json
import math
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


def assert_numeric_close(name: str, generated: str, baseline: str) -> None:
    actual = sql(generated)
    expected = sql(baseline)
    if len(actual) != len(expected) or any(len(left) != len(right) for left, right in zip(actual, expected)):
        raise AssertionError(f"{name} result shape mismatch: expected {expected!r}, actual {actual!r}")
    for actual_row, expected_row in zip(actual, expected):
        for actual_value, expected_value in zip(actual_row, expected_row):
            try:
                matches = math.isclose(
                    float(actual_value),
                    float(expected_value),
                    rel_tol=1e-12,
                    abs_tol=1e-6,
                )
            except ValueError:
                matches = actual_value == expected_value
            if not matches:
                raise AssertionError(f"{name} mismatch: expected {expected!r}, actual {actual!r}")
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
                {"name": "status_left", "expression": {"sql": "LEFT(orderstatus, 1)"}},
                {"name": "status_right", "expression": {"sql": "RIGHT(orderstatus, 1)"}},
                {
                    "name": "status_label",
                    "expression": {
                        "sql": "CONCAT(UPPER(orderstatus), '-', SPLIT_PART('a-b', '-', 2))"
                    },
                },
                {
                    "name": "status_position",
                    "expression": {"sql": "CHARINDEX('O', orderstatus)"},
                },
                {
                    "name": "status_contains",
                    "expression": {"sql": "CONTAINS(orderstatus, 'O')"},
                },
                {
                    "name": "status_starts",
                    "expression": {"sql": "STARTSWITH(orderstatus, 'F')"},
                },
                {
                    "name": "status_ends",
                    "expression": {"sql": "ENDSWITH(orderstatus, 'P')"},
                },
                {"name": "trunc_positive", "expression": {"sql": "TRUNC(123.456, 2)"}},
                {"name": "trunc_negative", "expression": {"sql": "TRUNCATE(-123.456, 2)"}},
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
        {"name": "revenue", "expression": {"sql": "ROUND(SUM(orders.totalprice), 2)"}},
        {
            "name": "math_profile",
            "expression": {
                "sql": (
                    "GREATEST(MOD(COUNT(orders.custkey), 5), "
                    "LEAST(POWER(2, 3), 10)) + SIGN(SUM(orders.totalprice)) + "
                    "EXP(0) + LN(1) + LOG(10, 100) + LOG10(100)"
                )
            },
        },
        *(
            {
                "name": f"stat_{name.lower()}",
                "expression": {"sql": f"{name}(orders.totalprice)"},
            }
            for name in (
                "STDDEV",
                "STDDEV_POP",
                "STDDEV_SAMP",
                "VARIANCE",
                "VAR_POP",
                "VAR_SAMP",
            )
        ),
        {
            "name": "revenue_per_customer",
            "expression": {
                "sql": (
                    "ROUND(NVL(SUM(orders.totalprice), 0) / "
                    "NULLIFZERO(COUNT(customers.custkey)), 6)"
                )
            },
        },
        {
            "name": "count_sevenths",
            "expression": {"sql": "COUNT(orders.custkey) / 7"},
        },
        {
            "name": "orders_per_customer_count",
            "expression": {
                "sql": "COUNT(orders.custkey) / COUNT(customers.custkey)"
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
    SELECT c.mktsegment, ROUND(SUM(o.totalprice), 2)
    FROM tpch.tiny.orders o
    LEFT JOIN tpch.tiny.customer c ON o.custkey = c.custkey
    GROUP BY c.mktsegment
    ORDER BY c.mktsegment
    """,
)

true_division = compiler.compile(
    Query(metrics=("count_sevenths",)),
    target="trino",
)
assert_numeric_close(
    "portable true division for integer aggregates",
    true_division.sql,
    "SELECT COUNT(custkey) * 1.0 / 7 FROM tpch.tiny.orders",
)

staged_true_division = compiler.compile(
    Query(
        metrics=("orders_per_customer_count",),
        dimensions=("customers.mktsegment",),
    ),
    target="trino",
)
assert_numeric_close(
    "portable true division after stage stitching",
    staged_true_division.sql + " ORDER BY d0",
    """
    WITH order_stage AS (
        SELECT c.mktsegment, COUNT(o.custkey) AS value
        FROM tpch.tiny.orders o
        LEFT JOIN tpch.tiny.customer c ON o.custkey = c.custkey
        GROUP BY c.mktsegment
    ), customer_stage AS (
        SELECT mktsegment, COUNT(custkey) AS value
        FROM tpch.tiny.customer
        GROUP BY mktsegment
    )
    SELECT COALESCE(order_stage.mktsegment, customer_stage.mktsegment),
           order_stage.value * 1.0 / customer_stage.value
    FROM order_stage
    FULL OUTER JOIN customer_stage
      ON order_stage.mktsegment IS NOT DISTINCT FROM customer_stage.mktsegment
    ORDER BY 1
    """,
)

truncation = compiler.compile(
    Query(
        metrics=("revenue",),
        dimensions=("orders.trunc_positive", "orders.trunc_negative"),
    ),
    target="trino",
)
assert_same(
    "portable truncation toward zero",
    truncation.sql,
    """
    SELECT 123.45, -123.45, ROUND(SUM(totalprice), 2)
    FROM tpch.tiny.orders
    GROUP BY 123.45, -123.45
    """,
)

string_search = compiler.compile(
    Query(
        metrics=("revenue",),
        dimensions=tuple(
            f"orders.status_{suffix}"
            for suffix in ("position", "contains", "starts", "ends")
        ),
    ),
    target="trino",
)
assert_same(
    "required string search functions",
    string_search.sql + " ORDER BY d0, d1, d2, d3",
    """
    SELECT STRPOS(orderstatus, 'O'), STRPOS(orderstatus, 'O') > 0,
           STRPOS(orderstatus, 'F') = 1,
           SUBSTRING(orderstatus, -LENGTH('P')) = 'P',
           ROUND(SUM(totalprice), 2)
    FROM tpch.tiny.orders
    GROUP BY STRPOS(orderstatus, 'O'), STRPOS(orderstatus, 'O') > 0,
             STRPOS(orderstatus, 'F') = 1,
             SUBSTRING(orderstatus, -LENGTH('P')) = 'P'
    ORDER BY 1, 2, 3, 4
    """,
)

statistics = compiler.compile(
    Query(
        metrics=tuple(
            f"stat_{name.lower()}"
            for name in (
                "STDDEV",
                "STDDEV_POP",
                "STDDEV_SAMP",
                "VARIANCE",
                "VAR_POP",
                "VAR_SAMP",
            )
        )
    ),
    target="trino",
)
assert_numeric_close(
    "sample and population statistical aggregates",
    statistics.sql,
    """
    SELECT STDDEV_SAMP(totalprice), STDDEV_POP(totalprice), STDDEV_SAMP(totalprice),
           VAR_SAMP(totalprice), VAR_POP(totalprice), VAR_SAMP(totalprice)
    FROM tpch.tiny.orders
    """,
)

portable_functions = compiler.compile(
    Query(metrics=("math_profile",), dimensions=("orders.status_label",)),
    target="trino",
)
assert_same(
    "required string and math functions",
    portable_functions.sql + " ORDER BY d0",
    """
    SELECT CONCAT(UPPER(orderstatus), '-', SPLIT_PART('a-b', '-', 2)),
           GREATEST(MOD(COUNT(custkey), 5), LEAST(POWER(2, 3), 10))
             + SIGN(SUM(totalprice)) + EXP(0) + LN(1) + LOG(10, 100) + LOG10(100)
    FROM tpch.tiny.orders
    GROUP BY CONCAT(UPPER(orderstatus), '-', SPLIT_PART('a-b', '-', 2))
    ORDER BY 1
    """,
)

string_edges = compiler.compile(
    Query(
        metrics=("revenue",),
        dimensions=("orders.status_left", "orders.status_right"),
    ),
    target="trino",
)
assert_same(
    "portable LEFT and RIGHT functions",
    string_edges.sql + " ORDER BY d0, d1",
    """
    SELECT substring(orderstatus, 1, 1), substring(orderstatus, -1),
           ROUND(SUM(totalprice), 2)
    FROM tpch.tiny.orders
    GROUP BY substring(orderstatus, 1, 1), substring(orderstatus, -1)
    ORDER BY 1, 2
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
           ROUND(
               COALESCE(
                   CASE WHEN order_stage.present IS NULL THEN NULL ELSE order_stage.value END,
                   0
               ) / NULLIF(
                   CASE WHEN customer_stage.present IS NULL THEN 0 ELSE customer_stage.value END,
                   0
               ),
               6
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
    SELECT c.mktsegment, ROUND(SUM(o.totalprice), 2)
    FROM tpch.tiny.orders o
    LEFT JOIN tpch.tiny.customer c ON o.custkey = c.custkey
    WHERE o.orderstatus = 'F'
    GROUP BY c.mktsegment
    ORDER BY c.mktsegment
    """,
)
