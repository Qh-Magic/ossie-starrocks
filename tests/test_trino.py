from __future__ import annotations

from io import BytesIO
import json
import unittest

from ossie_starrocks import TrinoClient, TrinoError


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _Open:
    def __init__(self, *pages: dict):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        return _Response(json.dumps(self.pages.pop(0)).encode("utf-8"))


class TrinoClientTests(unittest.TestCase):
    def test_executes_statement_and_collects_paginated_rows(self):
        open_request = _Open(
            {
                "id": "query-1",
                "columns": [{"name": "d0", "type": "varchar"}],
                "data": [["TW"]],
                "nextUri": "http://trino.test:8080/v1/statement/query-1/1",
            },
            {"id": "query-1", "data": [["JP"]]},
        )
        client = TrinoClient(
            "http://trino.test:8080",
            "analyst",
            timeout=7,
            _open=open_request,
        )

        result = client.execute('SELECT "country" FROM "customers"')

        self.assertEqual("query-1", result.query_id)
        self.assertEqual(("d0", "varchar"), (result.columns[0].name, result.columns[0].type))
        self.assertEqual((("TW",), ("JP",)), result.rows)
        first, second = open_request.requests
        self.assertEqual("POST", first[0].method)
        self.assertEqual(b'SELECT "country" FROM "customers"', first[0].data)
        self.assertEqual("analyst", first[0].get_header("X-trino-user"))
        self.assertEqual("GET", second[0].method)
        self.assertEqual(7, second[1])

    def test_surfaces_trino_query_error_with_query_id(self):
        client = TrinoClient(
            "http://trino.test:8080",
            "analyst",
            _open=_Open({
                "id": "query-2",
                "error": {"errorName": "SYNTAX_ERROR", "message": "bad query"},
            }),
        )

        with self.assertRaises(TrinoError) as raised:
            client.execute("SELECT")

        self.assertEqual("SYNTAX_ERROR", raised.exception.code)
        self.assertEqual("query-2", raised.exception.query_id)
        self.assertEqual("bad query", raised.exception.message)

    def test_rejects_cross_origin_next_uri(self):
        client = TrinoClient(
            "https://trino.test",
            "analyst",
            _open=_Open({
                "id": "query-3",
                "nextUri": "https://attacker.test/steal",
            }),
        )

        with self.assertRaisesRegex(TrinoError, "configured Trino origin"):
            client.execute("SELECT 1")

    def test_rejects_rows_that_do_not_match_columns(self):
        client = TrinoClient(
            "http://trino.test:8080",
            "analyst",
            _open=_Open({
                "id": "query-4",
                "columns": [{"name": "a", "type": "bigint"}],
                "data": [[1, 2]],
            }),
        )

        with self.assertRaisesRegex(TrinoError, "row width"):
            client.execute("SELECT 1")

    def test_rejects_invalid_connection_options(self):
        with self.assertRaisesRegex(ValueError, "absolute http"):
            TrinoClient("file:///tmp/trino", "analyst")
        with self.assertRaisesRegex(ValueError, "single-line"):
            TrinoClient("http://trino.test", "analyst\nX-Trino-Role: admin")
        with self.assertRaisesRegex(ValueError, "positive finite"):
            TrinoClient("http://trino.test", "analyst", timeout=0)

    def test_rejects_non_query_statements(self):
        client = TrinoClient("http://trino.test", "analyst", _open=_Open())

        with self.assertRaisesRegex(ValueError, "only compiler-style"):
            client.execute("DELETE FROM orders")


if __name__ == "__main__":
    unittest.main()
