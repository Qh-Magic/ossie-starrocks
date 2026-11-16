from __future__ import annotations

import json
import math
import os
import subprocess

from ossie_starrocks import Compiler, FieldFilter, MetricFilter, Query


CONTAINER = os.environ.get("STARROCKS_CONTAINER", "ossie-starrocks-live")


def sql(statement: str) -> tuple[tuple[str, ...], ...]:
    completed = subprocess.run(
        [
            "docker", "exec", "-i", CONTAINER, "mysql",
            "-h", "127.0.0.1", "-P", "9030", "-u", "root",
            "--batch", "--raw", "--skip-column-names",
        ],
        input=statement,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"StarRocks command failed ({completed.returncode})\n"
            f"SQL:\n{statement}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    rows = []
    for line in completed.stdout.splitlines():
        rows.append(tuple(line.split("\t")))
    return tuple(rows)


def assert_same(name: str, generated: str, baseline: str) -> None:
    actual = sql(f"USE ossie_test;\n{generated};")
    expected = sql(f"USE ossie_test;\n{baseline};")
    if actual != expected:
        raise AssertionError(
            f"{name} mismatch\ngenerated SQL:\n{generated}\n"
            f"expected rows: {expected!r}\nactual rows: {actual!r}"
        )
    print(f"PASS {name}: {actual!r}")


def assert_numeric_close(name: str, generated: str, baseline: str) -> None:
    actual = sql(f"USE ossie_test;\n{generated};")
    expected = sql(f"USE ossie_test;\n{baseline};")
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


sql(
    """
    CREATE DATABASE IF NOT EXISTS ossie_test;
    USE ossie_test;
    DROP TABLE IF EXISTS orders;
    DROP TABLE IF EXISTS customers;
    CREATE TABLE customers (
        customer_id BIGINT NOT NULL,
        country VARCHAR(32) NULL
    ) ENGINE=OLAP
    DUPLICATE KEY(customer_id)
    DISTRIBUTED BY HASH(customer_id) BUCKETS 1
    PROPERTIES ("replication_num" = "1");
    CREATE TABLE orders (
        order_id BIGINT NOT NULL,
        customer_id BIGINT NOT NULL,
        amount DECIMAL(18,2) NULL,
        status VARCHAR(32) NULL,
        ordered_at DATETIME NULL
    ) ENGINE=OLAP
    DUPLICATE KEY(order_id)
    DISTRIBUTED BY HASH(order_id) BUCKETS 1
    PROPERTIES ("replication_num" = "1");
    INSERT INTO customers VALUES
        (1, 'TW'), (2, 'TW'), (3, 'JP');
    INSERT INTO orders VALUES
        (10, 1, 100.00, 'complete', '2026-01-02 10:00:00'),
        (11, 1, 25.00, 'cancelled', '2026-01-15 12:00:00'),
        (12, 2, 80.00, 'complete', '2026-02-01 09:00:00'),
        (13, 3, NULL, 'complete', '2026-02-03 08:00:00'),
        (14, 99, 50.00, 'complete', '2026-02-04 08:00:00');
    """
)

model = {
    "version": "0.3.0.dev0",
    "name": "live_sales",
    "datasets": [
        {
            "name": "orders",
            "source": "ossie_test.orders",
            "fields": [
                {"name": "customer_id", "expression": {"sql": "customer_id"}},
                {"name": "amount", "expression": {"sql": "amount"}},
                {"name": "status", "expression": {"sql": "status"}},
                {
                    "name": "status_label",
                    "expression": {
                        "sql": "CONCAT(UPPER(status), '-', SPLIT_PART('a-b', '-', 2))"
                    },
                },
                {"name": "status_position", "expression": {"sql": "CHARINDEX('o', status)"}},
                {"name": "status_contains", "expression": {"sql": "CONTAINS(status, 'o')"}},
                {"name": "status_starts", "expression": {"sql": "STARTSWITH(status, 'c')"}},
                {"name": "status_ends", "expression": {"sql": "ENDSWITH(status, 'e')"}},
                {"name": "ordered_at", "expression": {"sql": "ordered_at"}},
                {"name": "order_month", "expression": {"sql": "DATE_TRUNC('month', ordered_at)"}},
            ],
            "metrics": [
                {"name": "orders_count", "expression": {"sql": "COUNT(customer_id)"}}
            ],
        },
        {
            "name": "customers",
            "source": "ossie_test.customers",
            "primary_key": ["customer_id"],
            "fields": [
                {"name": "customer_id", "expression": {"sql": "customer_id"}},
                {"name": "country", "expression": {"sql": "country"}},
            ],
            "metrics": [
                {"name": "customers_count", "expression": {"sql": "COUNT(customer_id)"}}
            ],
        },
    ],
    "relationships": [
        {
            "name": "orders_customer",
            "from": "orders",
            "to": "customers",
            "from_columns": ["customer_id"],
            "to_columns": ["customer_id"],
        }
    ],
    "metrics": [
        {"name": "revenue", "expression": {"sql": "SUM(orders.amount)"}},
        {
            "name": "math_profile",
            "expression": {
                "sql": (
                    "GREATEST(MOD(COUNT(orders.customer_id), 5), "
                    "LEAST(POWER(2, 3), 10)) + SIGN(SUM(orders.amount)) + "
                    "EXP(0) + LN(1) + LOG(10, 100) + LOG10(100)"
                )
            },
        },
        *(
            {
                "name": f"stat_{name.lower()}",
                "expression": {"sql": f"{name}(orders.amount)"},
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
            "name": "active_customers",
            "expression": {
                "sql": "COUNT(DISTINCT orders.customer_id) FILTER (WHERE orders.status = 'complete')"
            },
        },
        {
            "name": "orders_per_customer",
            "expression": {
                "sql": "AGG(orders.orders_count) / AGG(customers.customers_count)"
            },
        },
        {
            "name": "revenue_per_customer",
            "expression": {
                "sql": "SUM(orders.amount) / COUNT(customers.customer_id)"
            },
        },
        {
            "name": "safe_revenue_per_customer",
            "expression": {
                "sql": "COALESCE(SUM(orders.amount), 0) / NULLIF(COUNT(customers.customer_id), 0)"
            },
        },
        {
            "name": "aliased_revenue_per_customer",
            "expression": {
                "sql": (
                    "IFF(NULLIFZERO(COUNT(customers.customer_id)) IS NULL, 0, "
                    "NVL2(SUM(orders.amount), "
                    "ZEROIFNULL(SUM(orders.amount)) / NULLIFZERO(COUNT(customers.customer_id)), "
                    "NVL(SUM(orders.amount), 0)))"
                )
            },
        },
    ],
}

compiler = Compiler.from_text(json.dumps(model), format="json")

dimension_only = compiler.compile(
    Query(dimensions=("orders.status", "customers.country"))
)
assert_same(
    "dimension-only query with inferred anchor",
    dimension_only.sql + " ORDER BY d0, d1",
    """
    SELECT o.status, c.country
    FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
    GROUP BY o.status, c.country
    ORDER BY o.status, c.country
    """,
)

string_search = compiler.compile(
    Query(
        metrics=("revenue",),
        dimensions=tuple(
            f"orders.status_{suffix}"
            for suffix in ("position", "contains", "starts", "ends")
        ),
    )
)
assert_same(
    "required string search functions",
    string_search.sql + " ORDER BY d0, d1, d2, d3",
    """
    SELECT LOCATE('o', status), LOCATE('o', status) > 0,
           LOCATE('c', status) = 1, RIGHT(status, LENGTH('e')) = 'e',
           SUM(amount)
    FROM orders
    GROUP BY LOCATE('o', status), LOCATE('o', status) > 0,
             LOCATE('c', status) = 1, RIGHT(status, LENGTH('e')) = 'e'
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
    )
)
assert_numeric_close(
    "sample and population statistical aggregates",
    statistics.sql,
    """
    SELECT STDDEV_SAMP(amount), STDDEV_POP(amount), STDDEV_SAMP(amount),
           VAR_SAMP(amount), VAR_POP(amount), VAR_SAMP(amount)
    FROM orders
    """,
)

grouped = compiler.compile(Query(metrics=("revenue",), dimensions=("customers.country",)))
assert_same(
    "joined dimension and aggregate",
    grouped.sql + " ORDER BY d0",
    """
    SELECT c.country, SUM(o.amount)
    FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
    GROUP BY c.country ORDER BY c.country
    """,
)

portable_functions = compiler.compile(
    Query(metrics=("math_profile",), dimensions=("orders.status_label",))
)
assert_same(
    "required string and math functions",
    portable_functions.sql + " ORDER BY d0",
    """
    SELECT CONCAT(UPPER(status), '-', SPLIT_PART('a-b', '-', 2)),
           GREATEST(MOD(COUNT(customer_id), 5), LEAST(POWER(2, 3), 10))
             + SIGN(SUM(amount)) + EXP(0) + LN(1) + LOG(10, 100) + LOG10(100)
    FROM orders
    GROUP BY CONCAT(UPPER(status), '-', SPLIT_PART('a-b', '-', 2))
    ORDER BY 1
    """,
)

multi_home_metrics = compiler.compile(
    Query(
        metrics=("revenue", "customers.customers_count"),
        dimensions=("customers.country",),
    )
)
assert_same(
    "metrics from different home datasets",
    multi_home_metrics.sql + " ORDER BY d0",
    """
    WITH order_stage AS (
        SELECT c.country, SUM(o.amount) AS value, 1 AS present
        FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
        GROUP BY c.country
    ), customer_stage AS (
        SELECT country, COUNT(customer_id) AS value, 1 AS present
        FROM customers
        GROUP BY country
    )
    SELECT COALESCE(order_stage.country, customer_stage.country),
           CASE WHEN order_stage.present IS NULL THEN NULL ELSE order_stage.value END,
           CASE WHEN customer_stage.present IS NULL THEN 0 ELSE customer_stage.value END
    FROM order_stage
    FULL OUTER JOIN customer_stage
      ON order_stage.country = customer_stage.country
         OR (order_stage.country IS NULL AND customer_stage.country IS NULL)
    ORDER BY COALESCE(order_stage.country, customer_stage.country)
    """,
)

raw_component_metric = compiler.compile(
    Query(
        metrics=("revenue_per_customer",),
        dimensions=("customers.country",),
    )
)
assert_same(
    "raw cross-dataset aggregate components",
    raw_component_metric.sql + " ORDER BY d0",
    """
    WITH order_stage AS (
        SELECT c.country, SUM(o.amount) AS value, 1 AS present
        FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
        GROUP BY c.country
    ), customer_stage AS (
        SELECT country, COUNT(customer_id) AS value, 1 AS present
        FROM customers
        GROUP BY country
    )
    SELECT COALESCE(order_stage.country, customer_stage.country),
           (CASE WHEN order_stage.present IS NULL THEN NULL ELSE order_stage.value END) /
           (CASE WHEN customer_stage.present IS NULL THEN 0 ELSE customer_stage.value END)
    FROM order_stage
    FULL OUTER JOIN customer_stage
      ON order_stage.country = customer_stage.country
         OR (order_stage.country IS NULL AND customer_stage.country IS NULL)
    ORDER BY COALESCE(order_stage.country, customer_stage.country)
    """,
)

conditional_component_metric = compiler.compile(
    Query(
        metrics=("safe_revenue_per_customer",),
        dimensions=("customers.country",),
    )
)

aliased_conditional_metric = compiler.compile(
    Query(
        metrics=("aliased_revenue_per_customer",),
        dimensions=("customers.country",),
    )
)
assert_same(
    "required conditional aliases lowered across components",
    aliased_conditional_metric.sql + " ORDER BY d0",
    """
    WITH order_stage AS (
        SELECT c.country, SUM(o.amount) AS value
        FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
        GROUP BY c.country
    ), customer_stage AS (
        SELECT country, COUNT(customer_id) AS value
        FROM customers
        GROUP BY country
    )
    SELECT COALESCE(order_stage.country, customer_stage.country),
           IF(NULLIF(COALESCE(customer_stage.value, 0), 0) IS NULL,
              0,
              IF(order_stage.value IS NOT NULL,
                 IFNULL(order_stage.value, 0) / NULLIF(COALESCE(customer_stage.value, 0), 0),
                 IFNULL(order_stage.value, 0)))
    FROM order_stage
    FULL OUTER JOIN customer_stage
      ON order_stage.country = customer_stage.country
         OR (order_stage.country IS NULL AND customer_stage.country IS NULL)
    ORDER BY COALESCE(order_stage.country, customer_stage.country)
    """,
)
assert_same(
    "required conditional functions across components",
    conditional_component_metric.sql + " ORDER BY d0",
    """
    WITH order_stage AS (
        SELECT c.country, SUM(o.amount) AS value, 1 AS present
        FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
        GROUP BY c.country
    ), customer_stage AS (
        SELECT country, COUNT(customer_id) AS value, 1 AS present
        FROM customers
        GROUP BY country
    )
    SELECT COALESCE(order_stage.country, customer_stage.country),
           COALESCE(
             CASE WHEN order_stage.present IS NULL THEN NULL ELSE order_stage.value END,
             0
           ) / NULLIF(
             CASE WHEN customer_stage.present IS NULL THEN 0 ELSE customer_stage.value END,
             0
           )
    FROM order_stage
    FULL OUTER JOIN customer_stage
      ON order_stage.country = customer_stage.country
         OR (order_stage.country IS NULL AND customer_stage.country IS NULL)
    ORDER BY COALESCE(order_stage.country, customer_stage.country)
    """,
)

expression_filtered_metric = compiler.compile(
    Query(
        metrics=("safe_revenue_per_customer",),
        dimensions=("customers.country",),
        filter="customers.country IN ('TW') OR customers.country LIKE 'J%'",
    )
)
assert_same(
    "bound Layer 3 filter expression in every branch",
    expression_filtered_metric.sql + " ORDER BY d0",
    """
    WITH order_stage AS (
        SELECT c.country, SUM(o.amount) AS value, 1 AS present
        FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
        WHERE c.country IN ('TW') OR c.country LIKE 'J%'
        GROUP BY c.country
    ), customer_stage AS (
        SELECT country, COUNT(customer_id) AS value, 1 AS present
        FROM customers
        WHERE country IN ('TW') OR country LIKE 'J%'
        GROUP BY country
    )
    SELECT COALESCE(order_stage.country, customer_stage.country),
           COALESCE(
             CASE WHEN order_stage.present IS NULL THEN NULL ELSE order_stage.value END,
             0
           ) / NULLIF(
             CASE WHEN customer_stage.present IS NULL THEN 0 ELSE customer_stage.value END,
             0
           )
    FROM order_stage
    FULL OUTER JOIN customer_stage
      ON order_stage.country = customer_stage.country
         OR (order_stage.country IS NULL AND customer_stage.country IS NULL)
    ORDER BY COALESCE(order_stage.country, customer_stage.country)
    """,
)

cross_grain = compiler.compile(Query(metrics=("orders_per_customer",)))
assert_same(
    "global cross-grain multi-stage aggregate",
    cross_grain.sql,
    """
    SELECT order_stage.value / customer_stage.value
    FROM (SELECT COUNT(customer_id) AS value FROM orders) order_stage
    CROSS JOIN (SELECT COUNT(customer_id) AS value FROM customers) customer_stage
    """,
)

keyed_cross_grain = compiler.compile(
    Query(metrics=("orders_per_customer",), dimensions=("customers.country",))
)
assert_same(
    "keyed cross-grain multi-stage aggregate",
    keyed_cross_grain.sql + " ORDER BY d0",
    """
    SELECT COALESCE(order_stage.country, customer_stage.country),
           order_stage.value / customer_stage.value
    FROM (
        SELECT c.country, COUNT(o.customer_id) AS value
        FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
        GROUP BY c.country
    ) order_stage
    FULL OUTER JOIN (
        SELECT country, COUNT(customer_id) AS value
        FROM customers
        GROUP BY country
    ) customer_stage
    ON order_stage.country = customer_stage.country
       OR (order_stage.country IS NULL AND customer_stage.country IS NULL)
    ORDER BY COALESCE(order_stage.country, customer_stage.country)
    """,
)

filtered_cross_grain = compiler.compile(
    Query(
        metrics=("orders_per_customer",),
        dimensions=("customers.country",),
        where=(FieldFilter("customers.country", "eq", "TW"),),
    )
)
assert_same(
    "safe filter pushed into every cross-grain stage",
    filtered_cross_grain.sql + " ORDER BY d0",
    """
    SELECT order_stage.country,
           order_stage.value / customer_stage.value
    FROM (
        SELECT c.country, COUNT(o.customer_id) AS value
        FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
        WHERE c.country = 'TW'
        GROUP BY c.country
    ) order_stage
    JOIN (
        SELECT country, COUNT(customer_id) AS value
        FROM customers
        WHERE country = 'TW'
        GROUP BY country
    ) customer_stage
    ON order_stage.country = customer_stage.country
    ORDER BY order_stage.country
    """,
)

having_cross_grain = compiler.compile(
    Query(
        metrics=("orders_per_customer",),
        dimensions=("customers.country",),
        having=(MetricFilter("orders_per_customer", "gt", 1),),
    )
)
assert_same(
    "cross-grain metric filter after stage join",
    having_cross_grain.sql + " ORDER BY d0",
    """
    SELECT COALESCE(order_stage.country, customer_stage.country),
           order_stage.value / customer_stage.value
    FROM (
        SELECT c.country, COUNT(o.customer_id) AS value
        FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
        GROUP BY c.country
    ) order_stage
    FULL OUTER JOIN (
        SELECT country, COUNT(customer_id) AS value
        FROM customers
        GROUP BY country
    ) customer_stage
    ON order_stage.country = customer_stage.country
       OR (order_stage.country IS NULL AND customer_stage.country IS NULL)
    WHERE order_stage.value / customer_stage.value > 1
    ORDER BY COALESCE(order_stage.country, customer_stage.country)
    """,
)

filtered = compiler.compile(Query(metrics=("active_customers",), dimensions=("orders.order_month",)))
assert_same(
    "date function and filtered distinct aggregate",
    filtered.sql + " ORDER BY d0",
    """
    SELECT DATE_TRUNC('month', ordered_at),
           COUNT(DISTINCT CASE WHEN status = 'complete' THEN customer_id END)
    FROM orders
    GROUP BY DATE_TRUNC('month', ordered_at)
    ORDER BY DATE_TRUNC('month', ordered_at)
    """,
)
