from __future__ import annotations

import json
import os
import subprocess

from ossie_starrocks import Compiler, Query


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
        check=True,
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
        (13, 3, NULL, 'complete', '2026-02-03 08:00:00');
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
                {"name": "ordered_at", "expression": {"sql": "ordered_at"}},
                {"name": "order_month", "expression": {"sql": "DATE_TRUNC('month', ordered_at)"}},
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
            "name": "active_customers",
            "expression": {
                "sql": "COUNT(DISTINCT orders.customer_id) FILTER (WHERE orders.status = 'complete')"
            },
        },
    ],
}

compiler = Compiler.from_text(json.dumps(model), format="json")

grouped = compiler.compile(Query(metrics=("revenue",), dimensions=("customers.country",)))
assert_same(
    "joined dimension and aggregate",
    grouped.sql + " ORDER BY d0",
    """
    SELECT c.country, SUM(o.amount)
    FROM orders o JOIN customers c ON o.customer_id = c.customer_id
    GROUP BY c.country ORDER BY c.country
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
