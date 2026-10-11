#!/usr/bin/env python3

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv


API_PATH = "/wp-json/wc/v3/orders"
PAGE_SIZE = 100
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

STANDARD_COLUMNS = [
    "id",
    "parent_id",
    "number",
    "order_key",
    "created_via",
    "version",
    "status",
    "currency",
    "currency_symbol",
    "date_created",
    "date_created_gmt",
    "date_modified",
    "date_modified_gmt",
    "discount_total",
    "discount_tax",
    "shipping_total",
    "shipping_tax",
    "cart_tax",
    "total",
    "total_tax",
    "prices_include_tax",
    "customer_id",
    "customer_ip_address",
    "customer_user_agent",
    "customer_note",
    "billing.first_name",
    "billing.last_name",
    "billing.company",
    "billing.address_1",
    "billing.address_2",
    "billing.city",
    "billing.state",
    "billing.postcode",
    "billing.country",
    "billing.email",
    "billing.phone",
    "shipping.first_name",
    "shipping.last_name",
    "shipping.company",
    "shipping.address_1",
    "shipping.address_2",
    "shipping.city",
    "shipping.state",
    "shipping.postcode",
    "shipping.country",
    "shipping.phone",
    "payment_method",
    "payment_method_title",
    "transaction_id",
    "date_paid",
    "date_paid_gmt",
    "date_completed",
    "date_completed_gmt",
    "cart_hash",
    "line_items",
    "tax_lines",
    "shipping_lines",
    "fee_lines",
    "coupon_lines",
    "refunds",
    "payment_url",
    "is_editable",
    "needs_payment",
    "needs_processing",
    "cost_of_goods_sold",
    "_links",
]


# Read one setting from the environment.
def env_value(name, default=None):
    value = os.getenv(name)
    return value if value not in (None, "") else default


# Load .env from the current directory when it exists.
def load_env_file():
    env_path = Path.cwd() / ".env"
    if env_path.is_file():
        load_dotenv(env_path)


# Define the command-line arguments.
def build_parser():
    parser = argparse.ArgumentParser(
        description="Export WooCommerce orders to a CSV file."
    )
    parser.add_argument("--url", default=env_value("WOO_URL"), help="WooCommerce site URL")
    parser.add_argument(
        "--consumer-key",
        default=env_value("WOO_CONSUMER_KEY"),
        help="WooCommerce REST API consumer key",
    )
    parser.add_argument(
        "--consumer-secret",
        default=env_value("WOO_CONSUMER_SECRET"),
        help="WooCommerce REST API consumer secret",
    )
    parser.add_argument(
        "--start-date",
        help="UTC start date or ISO-8601 date/time",
    )
    parser.add_argument(
        "--end-date",
        help="UTC end date or ISO-8601 date/time",
    )
    parser.add_argument(
        "--since",
        help="Export from this UTC date or ISO-8601 date/time until now",
    )
    parser.add_argument(
        "--exclude-statuses",
        default=env_value("WOO_EXCLUDED_STATUSES", ""),
        help="Comma-separated order statuses to exclude",
    )
    parser.add_argument(
        "--custom-meta",
        default=env_value("WOO_CUSTOM_META", ""),
        help="Comma-separated order meta keys to export",
    )
    parser.add_argument(
        "--output",
        default=env_value("WOO_OUTPUT", "orders.csv"),
        help="Output CSV path",
    )
    parser.add_argument(
        "--raw-values",
        action="store_true",
        help="Keep values that spreadsheet apps may read as formulas",
    )
    return parser


# Apply date settings without mixing CLI modes.
def apply_date_env(args):
    if args.since:
        return args

    if args.start_date or args.end_date:
        args.start_date = args.start_date or env_value("WOO_START_DATE")
        args.end_date = args.end_date or env_value("WOO_END_DATE")
        return args

    args.start_date = env_value("WOO_START_DATE")
    args.end_date = env_value("WOO_END_DATE")
    args.since = env_value("WOO_SINCE")
    return args


# Split a comma-separated setting into unique values.
def parse_list(value):
    items = []
    for item in value.split(",") if value else []:
        item = item.strip()
        if item and item not in items:
            items.append(item)
    return items


# Normalize a WooCommerce status slug.
def normalize_status(status):
    value = str(status).strip().lower()
    return value[3:] if value.startswith("wc-") else value


# Parse one date value as a UTC timestamp.
def parse_utc(value, label):
    text = value.strip()
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))

    try:
        if date_only:
            parsed = datetime.combine(date.fromisoformat(text), datetime_time.min)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"{label} must be an ISO-8601 date or date/time.") from error

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    return parsed, date_only


# Format a UTC timestamp for the WooCommerce API.
def format_utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


# Round a timestamp up to the next full second.
def ceil_to_second(value):
    if value.microsecond:
        return value.replace(microsecond=0) + timedelta(seconds=1)
    return value


# Build the WooCommerce date filter parameters.
def build_date_params(start_date, end_date, since, now=None):
    if since and (start_date or end_date):
        raise RuntimeError("Use --since or --start-date with --end-date, not both.")

    run_started_at = now or datetime.now(timezone.utc)

    if since:
        start, _ = parse_utc(since, "--since")
        end = run_started_at
    elif start_date and end_date:
        start, _ = parse_utc(start_date, "--start-date")
        end, end_is_date = parse_utc(end_date, "--end-date")
        if end_is_date:
            end += timedelta(days=1)
    else:
        raise RuntimeError("Set --since, or set both --start-date and --end-date.")

    if start >= end:
        raise RuntimeError("The start date must be earlier than the end date.")

    start = ceil_to_second(start)
    end = ceil_to_second(end)

    return {
        "after": format_utc(start - timedelta(seconds=1)),
        "before": format_utc(end),
        "dates_are_gmt": "true",
    }


# Validate and normalize the store URL.
def validate_site_url(value):
    if not value:
        raise RuntimeError("Missing site URL. Set --url or WOO_URL.")

    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("The site URL must be a valid HTTPS URL.")
    if parsed.query or parsed.fragment:
        raise RuntimeError("The site URL must not contain a query string or fragment.")

    return value.rstrip("/")


# Validate required settings and return date filters.
def validate_config(args):
    args.url = validate_site_url(args.url)

    if not args.consumer_key:
        raise RuntimeError("Missing consumer key. Set --consumer-key or WOO_CONSUMER_KEY.")
    if not args.consumer_secret:
        raise RuntimeError(
            "Missing consumer secret. Set --consumer-secret or WOO_CONSUMER_SECRET."
        )

    return build_date_params(args.start_date, args.end_date, args.since)


# Create an authenticated WooCommerce HTTP session.
def create_session(consumer_key, consumer_secret, session=None):
    session = session or requests.Session()
    session.auth = (consumer_key, consumer_secret)
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "woo-order-exporter/1.0",
        }
    )
    return session


# Choose the delay before a transient request retry.
def retry_delay(response, attempt):
    retry_after = response.headers.get("Retry-After", "")
    if retry_after.isdigit():
        return int(retry_after)

    try:
        retry_at = parsedate_to_datetime(retry_after)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return 2**attempt


# Build a concise WooCommerce API error message.
def api_error_message(response):
    try:
        data = response.json()
    except ValueError:
        data = {}

    code = data.get("code") if isinstance(data, dict) else None
    message = data.get("message") if isinstance(data, dict) else None
    detail = ": ".join(str(part) for part in (code, message) if part)

    if detail:
        return f"WooCommerce API error {response.status_code}: {detail}"
    return f"WooCommerce API error {response.status_code}."


# Request one WooCommerce API page with retries.
def get_response(session, orders_url, params):
    last_error = None

    for attempt in range(3):
        try:
            response = session.get(orders_url, params=params, timeout=30)
        except requests.RequestException as error:
            last_error = error
            if attempt == 2:
                break
            time.sleep(2**attempt)
            continue

        if response.status_code in RETRY_STATUS_CODES and attempt < 2:
            time.sleep(retry_delay(response, attempt))
            continue

        if not response.ok:
            raise RuntimeError(api_error_message(response))

        return response

    raise RuntimeError(f"Could not connect to WooCommerce: {last_error}")


# Fetch every order page for the selected date range.
def fetch_orders(session, orders_url, date_params):
    orders = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        print(f"Fetching orders page {page}...", file=sys.stderr)
        params = {
            "page": page,
            "per_page": PAGE_SIZE,
            "status": "any",
            "orderby": "date",
            "order": "asc",
            **date_params,
        }
        response = get_response(session, orders_url, params)

        try:
            page_orders = response.json()
        except ValueError as error:
            raise RuntimeError("WooCommerce returned invalid JSON.") from error

        if not isinstance(page_orders, list):
            raise RuntimeError("WooCommerce returned an unexpected orders response.")

        orders.extend(page_orders)

        total_pages_header = response.headers.get("X-WP-TotalPages")
        if total_pages_header is None:
            raise RuntimeError(
                "WooCommerce response is missing the X-WP-TotalPages header."
            )

        try:
            total_pages = int(total_pages_header)
        except ValueError as error:
            raise RuntimeError(
                "WooCommerce returned an invalid pagination header."
            ) from error

        page += 1

    return orders


# Flatten a nested object into dotted CSV columns.
def flatten_dict(data, prefix=""):
    flat = {}

    for key, value in data.items():
        column = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten_dict(value, column))
        else:
            flat[column] = value

    return flat


# Flatten standard order objects and omit raw order meta.
def flatten_order(order):
    flat = {}

    for key, value in order.items():
        if key == "meta_data":
            continue
        if isinstance(value, dict) and key in {"billing", "shipping"}:
            flat.update(flatten_dict(value, key))
        else:
            flat[key] = value

    return flat


# Read one requested order meta value.
def meta_value(order, key):
    values = [
        item.get("value")
        for item in order.get("meta_data", [])
        if isinstance(item, dict) and item.get("key") == key
    ]

    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return values


# Convert one API value into a safe CSV value.
def csv_value(value, protect_formulas=True):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    if protect_formulas and isinstance(value, str):
        visible_value = value.lstrip(" \t\r\n")
        if visible_value.startswith(("=", "+", "-", "@")):
            return f"'{value}"
    return value


# Build stable CSV headers and order rows.
def prepare_csv(orders, custom_meta, raw_values=False):
    known_columns = set(STANDARD_COLUMNS)
    extra_columns = set()

    for order in orders:
        extra_columns.update(flatten_order(order).keys() - known_columns)

    meta_columns = [f"meta.{key}" for key in custom_meta]
    headers = STANDARD_COLUMNS + sorted(extra_columns) + meta_columns
    rows = []
    protect_formulas = not raw_values

    for order in orders:
        flat_order = flatten_order(order)
        row = {
            column: csv_value(flat_order.get(column), protect_formulas)
            for column in headers
        }
        for key, column in zip(custom_meta, meta_columns):
            row[column] = csv_value(meta_value(order, key), protect_formulas)
        rows.append(row)

    return headers, rows


# Write the CSV atomically to the output path.
def write_csv(output_path, headers, rows):
    path = Path(output_path).expanduser()
    temporary_path = None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as csv_file:
            temporary_path = Path(csv_file.name)
            writer = csv.DictWriter(csv_file, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary_path, path)
    except (OSError, csv.Error) as error:
        raise RuntimeError(f"Could not write {path}: {error}") from error
    finally:
        if temporary_path and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass

    return path.resolve()


# Remove orders with excluded statuses.
def filter_orders(orders, excluded_statuses):
    excluded = {normalize_status(status) for status in excluded_statuses}
    return [
        order
        for order in orders
        if normalize_status(order.get("status", "")) not in excluded
    ]


# Run the complete order export.
def run(args):
    date_params = validate_config(args)
    excluded_statuses = parse_list(args.exclude_statuses)
    custom_meta = parse_list(args.custom_meta)
    orders_url = f"{args.url}{API_PATH}"
    session = create_session(args.consumer_key, args.consumer_secret)

    try:
        orders = fetch_orders(session, orders_url, date_params)
    finally:
        session.close()

    exported_orders = filter_orders(orders, excluded_statuses)
    headers, rows = prepare_csv(exported_orders, custom_meta, args.raw_values)
    output_path = write_csv(args.output, headers, rows)
    skipped = len(orders) - len(exported_orders)

    if skipped:
        print(f"Skipped {skipped} order(s) with excluded statuses.")
    print(f"Exported {len(exported_orders)} order(s) to {output_path}")


# Parse configuration and return the process exit code.
def main():
    load_env_file()
    parser = build_parser()
    args = apply_date_env(parser.parse_args())

    try:
        run(args)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nExport cancelled.", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
