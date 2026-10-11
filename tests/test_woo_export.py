import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from woo_export import (
    apply_date_env,
    build_parser,
    build_date_params,
    create_session,
    fetch_orders,
    filter_orders,
    get_response,
    load_env_file,
    parse_list,
    prepare_csv,
    write_csv,
)


class FakeResponse:
    def __init__(self, data, headers=None, status_code=200):
        self.data = data
        self.headers = headers or {}
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.auth = None
        self.headers = {}
        self.closed = False

    def get(self, url, params, timeout):
        self.requests.append((url, params, timeout))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class DateTests(unittest.TestCase):
    def test_range_uses_inclusive_date_boundaries(self):
        params = build_date_params("2026-01-01", "2026-01-31", None)

        self.assertEqual(params["after"], "2025-12-31T23:59:59Z")
        self.assertEqual(params["before"], "2026-02-01T00:00:00Z")
        self.assertEqual(params["dates_are_gmt"], "true")

    def test_since_ends_at_run_start(self):
        now = datetime(2026, 2, 1, 12, 30, tzinfo=timezone.utc)
        params = build_date_params(None, None, "2026-01-01", now=now)

        self.assertEqual(params["before"], "2026-02-01T12:30:00Z")

    def test_since_includes_the_current_partial_second(self):
        now = datetime(2026, 2, 1, 12, 30, 0, 900000, tzinfo=timezone.utc)
        params = build_date_params(None, None, "2026-01-01", now=now)

        self.assertEqual(params["before"], "2026-02-01T12:30:01Z")

    def test_rejects_conflicting_date_modes(self):
        with self.assertRaises(RuntimeError):
            build_date_params("2026-01-01", "2026-01-31", "2026-01-15")


class ApiTests(unittest.TestCase):
    @patch("woo_export.time.sleep")
    def test_fetches_every_page(self, _sleep):
        session = FakeSession(
            [
                FakeResponse([{"id": 1}], {"X-WP-TotalPages": "2"}),
                FakeResponse([{"id": 2}], {"X-WP-TotalPages": "2"}),
            ]
        )
        session = create_session("ck_test", "cs_test", session)

        orders = fetch_orders(
            session,
            "https://store.example.com/wp-json/wc/v3/orders",
            {"after": "a", "before": "b"},
        )

        self.assertEqual([order["id"] for order in orders], [1, 2])
        self.assertEqual([call[1]["page"] for call in session.requests], [1, 2])
        self.assertTrue(all(call[1]["status"] == "any" for call in session.requests))
        self.assertEqual(session.auth, ("ck_test", "cs_test"))

    def test_formats_api_errors(self):
        response = FakeResponse(
            {"code": "woocommerce_rest_cannot_view", "message": "Unauthorized"},
            status_code=401,
        )
        session = FakeSession([response])
        session = create_session("ck_test", "cs_test", session)

        with self.assertRaisesRegex(RuntimeError, "Unauthorized"):
            get_response(session, "https://store.example.com/orders", {})

    @patch("woo_export.time.sleep")
    def test_retries_transient_errors(self, sleep):
        session = FakeSession(
            [
                FakeResponse({}, status_code=503),
                FakeResponse([{"id": 1}], {"X-WP-TotalPages": "1"}),
            ]
        )
        session = create_session("ck_test", "cs_test", session)

        response = get_response(session, "https://store.example.com/orders", {})

        self.assertEqual(response.json(), [{"id": 1}])
        self.assertEqual(len(session.requests), 2)
        sleep.assert_called_once_with(1)

    @patch("woo_export.time.sleep")
    def test_honors_numeric_retry_after(self, sleep):
        session = FakeSession(
            [
                FakeResponse({}, {"Retry-After": "120"}, status_code=429),
                FakeResponse([], {"X-WP-TotalPages": "1"}),
            ]
        )
        session = create_session("ck_test", "cs_test", session)

        get_response(session, "https://store.example.com/orders", {})

        sleep.assert_called_once_with(120)

    def test_rejects_missing_pagination_header(self):
        session = FakeSession([FakeResponse([])])
        session = create_session("ck_test", "cs_test", session)

        with self.assertRaisesRegex(RuntimeError, "X-WP-TotalPages"):
            fetch_orders(session, "https://store.example.com/orders", {})


class ConfigTests(unittest.TestCase):
    def test_cli_overrides_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "WOO_URL=https://env.example.com\nWOO_SINCE=2026-01-01\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                with patch("woo_export.Path.cwd", return_value=Path(directory)):
                    load_env_file()
                args = apply_date_env(
                    build_parser().parse_args(["--url", "https://cli.example.com"])
                )

        self.assertEqual(args.url, "https://cli.example.com")
        self.assertEqual(args.since, "2026-01-01")

    def test_cli_range_overrides_dotenv_since(self):
        with patch.dict(os.environ, {"WOO_SINCE": "2025-01-01"}, clear=True):
            args = build_parser().parse_args(
                ["--start-date", "2026-01-01", "--end-date", "2026-01-31"]
            )
            args = apply_date_env(args)

        self.assertIsNone(args.since)
        self.assertEqual(args.start_date, "2026-01-01")
        self.assertEqual(args.end_date, "2026-01-31")


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.orders = [
            {
                "id": 25,
                "status": "processing",
                "billing": {"first_name": "Zoë", "email": "zoe@example.com"},
                "line_items": [
                    {
                        "id": 8,
                        "name": "Coffee, large",
                        "quantity": 2,
                        "meta_data": [{"key": "Size", "value": "Large"}],
                    }
                ],
                "meta_data": [
                    {"key": "_tracking_number", "value": "A-1"},
                    {"key": "tag", "value": "first"},
                    {"key": "tag", "value": "second"},
                ],
                "store_extension": {"source": "test"},
                "cost_of_goods_sold": {"total_value": "4.50"},
                "_links": {"self": [{"href": "https://store.example.com/order/25"}]},
            }
        ]

    def test_prepares_standard_and_requested_meta_columns(self):
        headers, rows = prepare_csv(self.orders, ["_tracking_number", "tag"])

        self.assertNotIn("meta_data", headers)
        self.assertIn("billing.email", headers)
        self.assertIn("store_extension", headers)
        self.assertEqual(json.loads(rows[0]["store_extension"]), {"source": "test"})
        self.assertEqual(
            json.loads(rows[0]["cost_of_goods_sold"]), {"total_value": "4.50"}
        )
        self.assertEqual(rows[0]["meta._tracking_number"], "A-1")
        self.assertEqual(json.loads(rows[0]["meta.tag"]), ["first", "second"])
        self.assertEqual(json.loads(rows[0]["line_items"])[0]["quantity"], 2)
        self.assertEqual(
            json.loads(rows[0]["_links"])["self"][0]["href"],
            "https://store.example.com/order/25",
        )

    def test_writes_valid_utf8_csv(self):
        headers, rows = prepare_csv(self.orders, [])

        with tempfile.TemporaryDirectory() as directory:
            path = write_csv(Path(directory) / "orders.csv", headers, rows)
            with path.open(encoding="utf-8", newline="") as csv_file:
                saved_rows = list(csv.DictReader(csv_file))

        self.assertEqual(saved_rows[0]["billing.first_name"], "Zoë")
        self.assertEqual(json.loads(saved_rows[0]["line_items"])[0]["name"], "Coffee, large")

    def test_filters_normalized_statuses(self):
        orders = [
            {"id": 1, "status": "processing"},
            {"id": 2, "status": "cancelled"},
        ]

        result = filter_orders(orders, parse_list("wc-cancelled, failed"))

        self.assertEqual([order["id"] for order in result], [1])

    def test_empty_export_keeps_standard_headers(self):
        headers, rows = prepare_csv([], ["tracking"])

        self.assertIn("line_items", headers)
        self.assertIn("meta.tracking", headers)
        self.assertEqual(rows, [])

    def test_protects_spreadsheets_from_formula_values(self):
        orders = [{"customer_note": "=HYPERLINK(\"https://example.com\")"}]

        _, safe_rows = prepare_csv(orders, [])
        _, raw_rows = prepare_csv(orders, [], raw_values=True)

        self.assertTrue(safe_rows[0]["customer_note"].startswith("'="))
        self.assertTrue(raw_rows[0]["customer_note"].startswith("="))

    def test_failed_write_keeps_the_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.csv"
            path.write_text("previous export\n", encoding="utf-8")

            with patch(
                "woo_export.csv.DictWriter.writerows",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    write_csv(path, ["id"], [{"id": 1}])

            self.assertEqual(path.read_text(encoding="utf-8"), "previous export\n")


if __name__ == "__main__":
    unittest.main()
