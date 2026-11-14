from __future__ import annotations

import json
import unittest

from ossie_starrocks import (
    CompilationError,
    Compiler,
    FieldFilter,
    MetricFilter,
    Query,
)

from tests.fixtures import clone, field, metric, sales_model


class CompilerTests(unittest.TestCase):
    def compile(self, model: dict, query: Query):
        return Compiler.from_text(json.dumps(model), format="json").compile(query)

    def assert_error(self, code: str, model: dict, query: Query | None = None):
        with self.assertRaises(CompilationError) as caught:
            compiler = Compiler.from_text(json.dumps(model), format="json")
            if query is not None:
                compiler.compile(query)
        self.assertEqual(code, caught.exception.code)

    def test_compiles_metric_and_joined_dimension(self):
        result = self.compile(
            sales_model(),
            Query(metrics=("revenue",), dimensions=("customers.country",)),
        )
        self.assertEqual(
            "SELECT t1.`country` AS `d0`, SUM(t0.`amount`) AS `m0`\n"
            "FROM `default_catalog`.`sales`.`orders` AS t0\n"
            "INNER JOIN `default_catalog`.`sales`.`customers` AS t1 "
            "ON t0.`customer_id` = t1.`customer_id`\n"
            "GROUP BY t1.`country`",
            result.sql,
        )
        self.assertEqual(("customers.country", "revenue"), tuple(c.semantic_name for c in result.columns))

    def test_compiles_where_having_and_limit_with_safe_literals(self):
        result = self.compile(
            sales_model(),
            Query(
                metrics=("revenue",),
                where=(FieldFilter("orders.status", "eq", "O'Reilly"),),
                having=(MetricFilter("revenue", "gt", 100),),
                limit=10,
            ),
        )
        self.assertIn("WHERE t0.`status` = 'O''Reilly'", result.sql)
        self.assertIn("HAVING SUM(t0.`amount`) > 100", result.sql)
        self.assertTrue(result.sql.endswith("LIMIT 10"))

    def test_rejects_unknown_reference(self):
        self.assert_error(
            "UNKNOWN_FIELD",
            sales_model(),
            Query(metrics=("revenue",), dimensions=("customers.missing",)),
        )

    def test_rejects_mixed_metric_grain(self):
        model = sales_model()
        model["datasets"][1]["metrics"] = [metric("customer_count", "COUNT(customer_id)")]
        self.assert_error(
            "MIXED_GRAIN",
            model,
            Query(metrics=("revenue", "customers.customer_count")),
        )

    def test_rejects_reverse_fanout(self):
        model = sales_model()
        model["datasets"][1]["metrics"] = [metric("customer_count", "COUNT(customer_id)")]
        self.assert_error(
            "FANOUT_JOIN",
            model,
            Query(metrics=("customers.customer_count",), dimensions=("orders.status",)),
        )

    def test_rejects_ambiguous_paths(self):
        model = sales_model()
        model["datasets"].append(
            {
                "name": "regions",
                "source": "default_catalog.sales.regions",
                "primary_key": ["region_id"],
                "fields": [field("region_id", "region_id"), field("name", "name")],
            }
        )
        model["datasets"][0]["fields"].append(field("region_id", "region_id"))
        model["datasets"][1]["fields"].append(field("region_id", "region_id"))
        model["relationships"].extend(
            [
                {
                    "name": "orders_region",
                    "from": "orders",
                    "to": "regions",
                    "from_columns": ["region_id"],
                    "to_columns": ["region_id"],
                },
                {
                    "name": "customers_region",
                    "from": "customers",
                    "to": "regions",
                    "from_columns": ["region_id"],
                    "to_columns": ["region_id"],
                },
            ]
        )
        self.assert_error(
            "AMBIGUOUS_JOIN",
            model,
            Query(metrics=("revenue",), dimensions=("regions.name",)),
        )

    def test_rejects_relationship_cycle_at_model_boundary(self):
        model = sales_model()
        model["datasets"][0]["primary_key"] = ["customer_id"]
        model["datasets"][1]["fields"].append(field("order_customer_id", "order_customer_id"))
        model["relationships"].append(
            {
                "name": "customers_orders",
                "from": "customers",
                "to": "orders",
                "from_columns": ["order_customer_id"],
                "to_columns": ["customer_id"],
            }
        )
        self.assert_error("RELATIONSHIP_CYCLE", model)

    def test_rejects_unsupported_expression_instead_of_passing_sql_through(self):
        model = sales_model()
        model["metrics"][0] = metric("revenue", "SUM(orders.amount) OVER ()")
        self.assert_error("UNSUPPORTED_EXPRESSION", model)

    def test_expands_computed_fields_inside_metrics(self):
        model = sales_model()
        model["datasets"][0]["fields"].append(field("double_amount", "amount * 2"))
        model["metrics"][0] = metric("revenue", "SUM(orders.double_amount)")
        result = self.compile(model, Query(metrics=("revenue",)))
        self.assertIn("SUM((t0.`amount` * 2)) AS `m0`", result.sql)
        self.assertNotIn("`double_amount`", result.sql)

    def test_rejects_computed_field_cycles(self):
        model = sales_model()
        model["datasets"][0]["fields"].extend(
            [field("first", "second + 1"), field("second", "first + 1")]
        )
        self.assert_error("EXPRESSION_CYCLE", model)

    def test_rejects_cross_dataset_computed_fields(self):
        model = sales_model()
        model["datasets"][1]["fields"].append(field("order_status", "orders.status"))
        self.assert_error("CROSS_DATASET_FIELD", model)

    def test_rejects_star_for_aggregates_other_than_count(self):
        model = sales_model()
        model["metrics"][0] = metric("revenue", "SUM(*)")
        self.assert_error("INVALID_METRIC_EXPRESSION", model)

    def test_rejects_nested_or_free_star(self):
        for expression in ("SUM(orders.amount + *)", "SUM(orders.amount) + *"):
            with self.subTest(expression=expression):
                model = sales_model()
                model["metrics"][0] = metric("revenue", expression)
                self.assert_error("INVALID_METRIC_EXPRESSION", model)

        model = sales_model()
        model["datasets"][0]["fields"].append(field("wild", "*"))
        self.assert_error("INVALID_FIELD_EXPRESSION", model)

    def test_rejects_computed_relationship_keys(self):
        model = sales_model()
        customer_id = model["datasets"][0]["fields"][0]
        customer_id["expression"]["dialects"][0]["expression"] = "customer_id + 1"
        self.assert_error("COMPUTED_RELATIONSHIP_KEY", model)

    def test_ossie_03_expression_sql_shape_is_accepted(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {"sql": "SUM(orders.amount)"}
        result = self.compile(model, Query(metrics=("revenue",)))
        self.assertIn("SUM(t0.`amount`) AS `m0`", result.sql)


if __name__ == "__main__":
    unittest.main()
