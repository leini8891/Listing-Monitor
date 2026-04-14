from __future__ import annotations

"""
Clean the raw perp listing watchboard for dashboard use.

Input:
  - listing_watchboard.csv

Output:
  - listing_watchboard_clean.csv

This is a separate transformation step from the listing detector.
It preserves the raw metadata_json for traceability while adding:
  - symbol_raw
  - symbol_display
  - settle_ccy
  - listing_time_utc
  - listing_time_sgt
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.paths import CLEAN_WATCHBOARD_FILE, RAW_WATCHBOARD_FILE


INPUT_FILE = RAW_WATCHBOARD_FILE
OUTPUT_FILE = CLEAN_WATCHBOARD_FILE

OUTPUT_COLUMNS = [
    "venue",
    "symbol_raw",
    "symbol_display",
    "base_asset",
    "quote_asset",
    "settle_ccy",
    "contract_type",
    "listing_time_utc",
    "listing_time_sgt",
    "first_seen_at",
    "metadata_json",
]

SGT = ZoneInfo("Asia/Singapore") if ZoneInfo else timezone(timedelta(hours=8), name="SGT")


def log(message: str):
    print(f"[Clean Watchboard] {message}")


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def safe_load_metadata(raw_metadata: str) -> tuple[dict, bool]:
    text = clean_text(raw_metadata)
    if not text:
        return {}, False

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}, True

    if isinstance(data, dict):
        return data, False
    return {}, True


def clean_symbol_display(venue: str, symbol_raw: str) -> str:
    symbol_display = clean_text(symbol_raw)
    if venue == "okx":
        symbol_display = symbol_display.replace("_UM", "")
    return symbol_display


def extract_settle_ccy(venue: str, metadata: dict) -> str:
    if venue == "binance":
        return clean_text(metadata.get("marginAsset"))

    if venue == "bybit":
        return clean_text(metadata.get("settleCoin"))

    if venue == "okx":
        return clean_text(metadata.get("settleCcy"))

    if venue == "bitget":
        margin_coins = metadata.get("supportMarginCoins")
        if isinstance(margin_coins, list) and margin_coins:
            return clean_text(margin_coins[0])
        return ""

    # Keep the first pass conservative for venues where current raw metadata
    # does not expose a clear settlement currency field.
    return ""


def coerce_millisecond_timestamp(raw_value) -> int | None:
    if raw_value in (None, ""):
        return None

    if isinstance(raw_value, str):
        raw_value = raw_value.strip()
        if not raw_value:
            return None

    try:
        milliseconds = int(raw_value)
    except (TypeError, ValueError):
        return None

    if milliseconds <= 0:
        return None
    return milliseconds


def extract_listing_timestamp_ms(venue: str, metadata: dict) -> int | None:
    field_by_venue = {
        "binance": "onboardDate",
        "bybit": "launchTime",
        "okx": "listTime",
        "bitget": "launchTime",
    }
    field_name = field_by_venue.get(venue)
    if not field_name:
        return None
    return coerce_millisecond_timestamp(metadata.get(field_name))


def timestamp_to_utc(timestamp_ms: int | None) -> datetime | None:
    if timestamp_ms is None:
        return None

    try:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None

    if dt.year < 2000 or dt.year > 2100:
        return None
    return dt.replace(microsecond=0)


def format_listing_time_utc(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).isoformat()


def format_listing_time_sgt(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(SGT).strftime("%Y-%m-%d %H:%M:%S") + " SGT"


def clean_row(raw_row: dict, stats: dict[str, int]) -> dict:
    venue = clean_text(raw_row.get("venue")).lower()
    symbol_raw = clean_text(raw_row.get("symbol"))
    base_asset = clean_text(raw_row.get("base_asset"))
    quote_asset = clean_text(raw_row.get("quote_asset"))
    contract_type = clean_text(raw_row.get("contract_type"))
    first_seen_at = clean_text(raw_row.get("first_seen_at"))
    metadata_json = raw_row.get("metadata_json") or ""

    metadata, malformed_json = safe_load_metadata(metadata_json)
    if malformed_json:
        stats["malformed_metadata_rows"] += 1

    settle_ccy = extract_settle_ccy(venue, metadata)
    listing_timestamp_ms = extract_listing_timestamp_ms(venue, metadata)
    listing_dt_utc = timestamp_to_utc(listing_timestamp_ms)

    if listing_timestamp_ms is not None and listing_dt_utc is None:
        stats["invalid_listing_time_rows"] += 1

    return {
        "venue": venue,
        "symbol_raw": symbol_raw,
        "symbol_display": clean_symbol_display(venue, symbol_raw),
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "settle_ccy": settle_ccy,
        "contract_type": contract_type,
        "listing_time_utc": format_listing_time_utc(listing_dt_utc),
        "listing_time_sgt": format_listing_time_sgt(listing_dt_utc),
        "first_seen_at": first_seen_at,
        "metadata_json": metadata_json,
    }


def clean_watchboard(input_file: Path, output_file: Path):
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    stats = {
        "rows": 0,
        "malformed_metadata_rows": 0,
        "invalid_listing_time_rows": 0,
    }

    with input_file.open("r", encoding="utf-8", newline="") as src:
        reader = csv.DictReader(src)
        raw_rows = list(reader)

    cleaned_rows = []
    for raw_row in raw_rows:
        stats["rows"] += 1
        cleaned_rows.append(clean_row(raw_row, stats))

    with output_file.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(cleaned_rows)

    log(f"Wrote {len(cleaned_rows)} cleaned rows to {output_file}")
    if stats["malformed_metadata_rows"]:
        log(f"Rows with malformed metadata_json: {stats['malformed_metadata_rows']}")
    if stats["invalid_listing_time_rows"]:
        log(f"Rows with invalid listing timestamps: {stats['invalid_listing_time_rows']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean the raw listing watchboard CSV.")
    parser.add_argument(
        "--input",
        type=Path,
        default=INPUT_FILE,
        help=f"Raw watchboard CSV path (default: {INPUT_FILE.name})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_FILE,
        help=f"Cleaned watchboard CSV path (default: {OUTPUT_FILE.name})",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    clean_watchboard(args.input, args.output)


if __name__ == "__main__":
    main()
