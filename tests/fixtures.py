from __future__ import annotations

import copy


def sales_model() -> dict:
    return {
        "version": "0.2.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": "orders",
                "source": "default_catalog.sales.orders",
                "fields": [
                    _field("customer_id", "customer_id"),
                    _field("amount", "amount"),
                    _field("status", "status"),
                ],
            },
            {
                "name": "customers",
                "source": "default_catalog.sales.customers",
                "primary_key": ["customer_id"],
                "fields": [
                    _field("customer_id", "customer_id"),
                    _field("country", "country"),
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
            _metric("revenue", "SUM(orders.amount)"),
            _metric("order_count", "COUNT(orders.customer_id)"),
        ],
    }


def clone(model: dict) -> dict:
    return copy.deepcopy(model)


def _field(name: str, sql: str) -> dict:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": sql}]},
    }


def _metric(name: str, sql: str) -> dict:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": sql}]},
    }


def field(name: str, sql: str) -> dict:
    return _field(name, sql)


def metric(name: str, sql: str) -> dict:
    return _metric(name, sql)
