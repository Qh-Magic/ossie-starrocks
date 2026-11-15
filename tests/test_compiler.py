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

    def test_ossie_03_field_expression_names_source_columns(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(field("gross", "raw_amount * 2"))
        model["metrics"][0]["expression"] = {"sql": "SUM(orders.gross)"}

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn("SUM((t0.`raw_amount` * 2))", result.sql)

    def test_ossie_03_does_not_expand_field_names_inside_field_expressions(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].extend(
            [field("first", "second + 1"), field("second", "first + 1")]
        )
        model["metrics"][0]["expression"] = {"sql": "SUM(orders.first)"}

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn("SUM((t0.`second` + 1))", result.sql)

    def test_ossie_03_rejects_unknown_core_properties(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["dialects"] = []
        self.assert_error("INVALID_MODEL", model)

    def test_ossie_03_official_schema_rejects_invalid_metadata(self):
        cases = []

        invalid_datatype = sales_model()
        invalid_datatype["version"] = "0.3.0.dev0"
        invalid_datatype["datasets"][0]["fields"][0]["datatype"] = "Money"
        cases.append(invalid_datatype)

        invalid_dimension = sales_model()
        invalid_dimension["version"] = "0.3.0.dev0"
        invalid_dimension["datasets"][0]["fields"][0]["dimension"] = {"is_time": "yes"}
        cases.append(invalid_dimension)

        invalid_dialect = sales_model()
        invalid_dialect["version"] = "0.3.0.dev0"
        invalid_dialect["datasets"][0]["fields"][0]["expression"] = {
            "dialects": [{"dialect": "OSSIE_SQL_2026"}]
        }
        cases.append(invalid_dialect)

        invalid_extension = sales_model()
        invalid_extension["version"] = "0.3.0.dev0"
        invalid_extension["nonportable_extensions"] = [{"vendor_name": "STARROCKS"}]
        cases.append(invalid_extension)

        for model in cases:
            with self.subTest(model=model):
                self.assert_error("INVALID_MODEL", model)

    def test_ossie_03_official_schema_accepts_portable_metadata(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["description"] = "Sales model"
        model["datasets"][0]["fields"][0].update(
            {"datatype": "Integer", "dimension": {"is_time": False}}
        )
        model["nonportable_extensions"] = [
            {"vendor_name": "STARROCKS", "data": {"distribution": ["customer_id"]}}
        ]

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn("SUM(t0.`amount`)", result.sql)

    def test_ossie_03_requires_expression_objects(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = "SUM(orders.amount)"
        self.assert_error("INVALID_MODEL", model)

    def test_rejects_field_metric_namespace_collision(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [metric("amount", "SUM(amount)")]
        self.assert_error("DUPLICATE_NAME", model)

    def test_ossie_03_dataset_metric_accepts_source_column(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = []
        model["datasets"][0]["metrics"] = [
            {"name": "raw_revenue", "expression": {"sql": "SUM(source.raw_amount)"}}
        ]

        result = self.compile(model, Query(metrics=("orders.raw_revenue",)))

        self.assertIn("SUM(t0.`raw_amount`) AS `m0`", result.sql)

    def test_ossie_03_source_field_shadows_source_column_qualifier(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = []
        model["datasets"][0]["fields"].append(field("source", "physical_source"))
        model["datasets"][0]["metrics"] = [
            {"name": "raw_revenue", "expression": {"sql": "SUM(source.raw_amount)"}}
        ]

        self.assert_error("INVALID_METRIC_EXPRESSION", model)

    def test_ossie_03_model_metric_requires_dataset_qualified_fields(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {"sql": "SUM(amount)"}

        self.assert_error("UNKNOWN_FIELD", model)

    def test_ossie_03_lowers_direct_scalar_functions_to_starrocks(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].extend(
            [
                {"name": "normalized_status", "expression": {"sql": "UPPER(TRIM(raw_status))"}},
                {"name": "order_month", "expression": {"sql": "DATE_TRUNC('month', ordered_at)"}},
            ]
        )
        model["metrics"][0]["expression"] = {"sql": "SUM(ABS(orders.amount))"}

        result = self.compile(
            model,
            Query(metrics=("revenue",), dimensions=("orders.normalized_status", "orders.order_month")),
        )

        self.assertIn("UPPER(TRIM(t0.`raw_status`))", result.sql)
        self.assertIn("DATE_TRUNC('month', t0.`ordered_at`)", result.sql)
        self.assertIn("SUM(ABS(t0.`amount`))", result.sql)

    def test_rejects_wrong_scalar_function_arity(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {"name": "bad_status", "expression": {"sql": "UPPER(status, status)"}}
        )

        self.assert_error("INVALID_EXPRESSION_ARITY", model)

    def test_rejects_unmapped_ossie_function(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {"name": "formatted", "expression": {"sql": "TO_CHAR(ordered_at, 'YYYY')"}}
        )

        self.assert_error("UNSUPPORTED_EXPRESSION", model)

    def test_ossie_03_compiles_conditional_aggregation(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {
            "sql": (
                "SUM(CASE WHEN orders.status = 'complete' AND orders.amount IS NOT NULL "
                "THEN -orders.amount % 10 ELSE 0 END)"
            )
        }

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn(
            "SUM(CASE WHEN ((t0.`status` = 'complete') AND (t0.`amount` IS NOT NULL)) "
            "THEN ((-t0.`amount`) % 10) ELSE 0 END)",
            result.sql,
        )

    def test_ossie_03_compiles_case_field_with_null_predicate(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {
                "name": "status_bucket",
                "expression": {
                    "sql": "CASE WHEN raw_status IS NULL OR raw_status = '' THEN 'unknown' ELSE raw_status END"
                },
            }
        )

        result = self.compile(
            model,
            Query(metrics=("revenue",), dimensions=("orders.status_bucket",)),
        )

        self.assertIn(
            "CASE WHEN ((t0.`raw_status` IS NULL) OR (t0.`raw_status` = '')) "
            "THEN 'unknown' ELSE t0.`raw_status` END",
            result.sql,
        )

    def test_ossie_03_predicate_precedence_is_preserved(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {
                "name": "flag",
                "expression": {
                    "sql": "CASE WHEN NOT raw_status = 'closed' AND (amount + 1) IS NOT NULL THEN 1 ELSE 0 END"
                },
            }
        )

        result = self.compile(model, Query(metrics=("revenue",), dimensions=("orders.flag",)))

        self.assertIn(
            "CASE WHEN ((NOT (t0.`raw_status` = 'closed')) AND ((t0.`amount` + 1) IS NOT NULL)) "
            "THEN 1 ELSE 0 END",
            result.sql,
        )

    def test_ossie_03_compiles_list_range_and_pattern_predicates(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {
                "name": "segment",
                "expression": {
                    "sql": (
                        "CASE WHEN raw_status IN ('new', 'open') AND amount BETWEEN 10 AND 100 "
                        "AND customer_name LIKE 'A%' THEN 'target' "
                        "WHEN raw_status NOT IN ('closed') OR amount NOT BETWEEN 0 AND 1000 "
                        "OR customer_name NOT LIKE '%test%' THEN 'other' ELSE 'excluded' END"
                    )
                },
            }
        )

        result = self.compile(model, Query(metrics=("revenue",), dimensions=("orders.segment",)))

        self.assertIn("t0.`raw_status` IN ('new', 'open')", result.sql)
        self.assertIn("t0.`amount` BETWEEN 10 AND 100", result.sql)
        self.assertIn("t0.`customer_name` LIKE 'A%'", result.sql)
        self.assertIn("t0.`raw_status` NOT IN ('closed')", result.sql)
        self.assertIn("t0.`amount` NOT BETWEEN 0 AND 1000", result.sql)
        self.assertIn("t0.`customer_name` NOT LIKE '%test%'", result.sql)

    def test_ossie_03_rejects_empty_in_list(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["fields"].append(
            {"name": "bad", "expression": {"sql": "raw_status IN ()"}}
        )

        self.assert_error("UNSUPPORTED_EXPRESSION", model)

    def test_ossie_03_lowers_distinct_filtered_aggregates(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {
                "name": "active_customers",
                "expression": {
                    "sql": "COUNT(DISTINCT orders.customer_id) FILTER (WHERE orders.status = 'complete')"
                },
            },
            {
                "name": "active_rows",
                "expression": {
                    "sql": "COUNT(*) FILTER (WHERE orders.status = 'complete')"
                },
            },
            {
                "name": "active_average",
                "expression": {
                    "sql": "AVG(orders.amount) FILTER (WHERE orders.status = 'complete')"
                },
            },
        ]

        result = self.compile(
            model,
            Query(metrics=("active_customers", "active_rows", "active_average")),
        )

        self.assertIn(
            "COUNT(DISTINCT CASE WHEN (t0.`status` = 'complete') THEN t0.`customer_id` END)",
            result.sql,
        )
        self.assertIn(
            "COUNT(CASE WHEN (t0.`status` = 'complete') THEN 1 END)",
            result.sql,
        )
        self.assertIn(
            "AVG(CASE WHEN (t0.`status` = 'complete') THEN t0.`amount` END)",
            result.sql,
        )
        self.assertNotIn("ELSE 0", result.sql)

    def test_ossie_03_supports_sum_distinct(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {"sql": "SUM(DISTINCT orders.amount)"}

        result = self.compile(model, Query(metrics=("revenue",)))

        self.assertIn("SUM(DISTINCT t0.`amount`)", result.sql)

    def test_ossie_03_rejects_distinct_on_unsupported_aggregate(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"][0]["expression"] = {"sql": "AVG(DISTINCT orders.amount)"}

        self.assert_error("INVALID_METRIC_EXPRESSION", model)

    def test_ossie_03_evaluates_same_grain_dataset_metrics_with_agg(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [
            {"name": "gross_revenue", "expression": {"sql": "SUM(amount)"}},
            {"name": "orders_count", "expression": {"sql": "COUNT(customer_id)"}},
        ]
        model["metrics"] = [
            {
                "name": "average_order_value",
                "expression": {
                    "sql": "AGG(orders.gross_revenue) / AGG(orders.orders_count)"
                },
            }
        ]

        result = self.compile(model, Query(metrics=("average_order_value",)))

        self.assertIn("(SUM(t0.`amount`) / COUNT(t0.`customer_id`))", result.sql)
        self.assertNotIn("AGG(", result.sql)

    def test_ossie_03_rejects_cross_grain_agg_until_multistage_planning(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["datasets"][0]["metrics"] = [
            {"name": "orders_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["datasets"][1]["metrics"] = [
            {"name": "customers_count", "expression": {"sql": "COUNT(customer_id)"}}
        ]
        model["metrics"] = [
            {
                "name": "ratio",
                "expression": {
                    "sql": "AGG(orders.orders_count) / AGG(customers.customers_count)"
                },
            }
        ]

        self.assert_error("MIXED_GRAIN", model)

    def test_ossie_03_rejects_agg_of_a_field(self):
        model = sales_model()
        model["version"] = "0.3.0.dev0"
        model["metrics"] = [
            {"name": "invalid", "expression": {"sql": "AGG(orders.amount)"}}
        ]

        self.assert_error("UNKNOWN_METRIC", model)


if __name__ == "__main__":
    unittest.main()
