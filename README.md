# WooCommerce Order CSV Exporter

> **Note:** This version exports orders only. Support for products and other WooCommerce objects is on the roadmap.

A small Python CLI that reads WooCommerce orders through the REST API and writes one order per CSV row.

## Requirements

- Python 3.10 or newer
- A WooCommerce REST API key with read access
- An HTTPS store URL

Create a read-only key in **WooCommerce > Settings > Advanced > REST API**. Keep the consumer secret private.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, activate the environment with:

```powershell
Copy-Item .env.example .env
.venv\Scripts\Activate.ps1
```

Edit `.env`, then run:

```bash
python woo_export.py
```

The script loads `.env` from the current directory. Command-line values override matching `.env` values. Choosing a date mode on the command line overrides the other date mode from `.env`.

## Configuration

| CLI option | `.env` key | Required | Description |
| --- | --- | --- | --- |
| `--url` | `WOO_URL` | Yes | Store base URL, such as `https://store.example.com` |
| `--consumer-key` | `WOO_CONSUMER_KEY` | Yes | REST API consumer key |
| `--consumer-secret` | `WOO_CONSUMER_SECRET` | Yes | REST API consumer secret |
| `--start-date` | `WOO_START_DATE` | Date range only | UTC range start |
| `--end-date` | `WOO_END_DATE` | Date range only | UTC range end |
| `--since` | `WOO_SINCE` | Since mode only | UTC start; the export ends when it starts running |
| `--exclude-statuses` | `WOO_EXCLUDED_STATUSES` | No | Comma-separated status slugs |
| `--custom-meta` | `WOO_CUSTOM_META` | No | Comma-separated order meta keys |
| `--output` | `WOO_OUTPUT` | No | CSV path; default is `orders.csv` |
| `--raw-values` | — | No | Disable spreadsheet-formula protection |

Use either `--since` or both `--start-date` and `--end-date`. Values accept `YYYY-MM-DD` or an ISO-8601 date/time. Date-only end values include the full day. A date-time end is an exclusive boundary.

Examples:

```bash
python woo_export.py --since 2026-01-01 --output exports/orders.csv
```

```bash
python woo_export.py \
  --start-date 2026-01-01 \
  --end-date 2026-01-31 \
  --exclude-statuses cancelled,failed \
  --custom-meta _tracking_number,_delivery_date
```

Run `python woo_export.py --help` for the full CLI reference.

## CSV format

- Standard order fields are included as columns.
- Billing and shipping objects use dotted columns, such as `billing.email`.
- `line_items` contains every product line as compact JSON in one column.
- Other object and list values, including taxes, shipping lines, fees, coupons, and refunds, are compact JSON.
- `_links` contains the standard WooCommerce API links as compact JSON.
- Raw order `meta_data` is excluded. Requested keys are added as `meta.<key>` columns.
- Repeated meta values are written as a JSON array. Missing values are blank.
- Extra non-meta fields returned by WooCommerce are appended as columns.
- Scalar values that look like spreadsheet formulas receive a leading apostrophe. Use `--raw-values` only when exact raw values are required and the CSV will not be opened in a spreadsheet.

The full line-item response is preserved, including line-item metadata used for variations or product options.

## API behavior

The exporter calls `/wp-json/wc/v3/orders` with HTTP Basic Authentication, 100 orders per page, and a fixed UTC time range. Excluded statuses are removed after download because the API has no general status-exclusion filter.

Transient connection errors and HTTP `429`/`5xx` responses use up to three attempts. Authentication and other API errors stop the export with a non-zero exit code.

Orders are held in memory until the CSV is written. Use smaller date ranges on very large stores.

## Tests

```bash
python -m unittest discover -s tests -v
```

WooCommerce API reference: <https://developer.woocommerce.com/docs/apis/rest-api/v3/orders/>
