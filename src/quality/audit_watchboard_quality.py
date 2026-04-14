from __future__ import annotations

"""
Generate lightweight data-quality audit CSVs for the perp listing watchboard.

Outputs:
  - listing_coverage_audit.csv
  - token_market_metrics_audit.csv

Notes:
  - Listing coverage audit focuses on where rows appear across the current raw watchboard,
    cleaned watchboard, and the latest archived cleaned snapshot.
  - Token market audit focuses on CoinGecko token-level match quality, not venue-level volume.
"""

import argparse
import csv
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*",
    category=Warning,
)

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.paths import (
    AUDITS_DIR,
    CLEAN_WATCHBOARD_FILE,
    HISTORY_DIR,
    LISTING_COVERAGE_AUDIT_FILE as LISTING_AUDIT_FILE,
    RAW_WATCHBOARD_FILE,
    TOKEN_MATCH_AUDIT_FILE,
    TOKEN_METRICS_AUDIT_FILE as TOKEN_AUDIT_FILE,
    ensure_directory_layout,
)


HISTORY_ROOT = HISTORY_DIR
BINANCE_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

DEFAULT_TOKENS = ["QQQ", "RAVE", "BASED", "BSB", "EDGE", "PRL", "0G", "XAUT", "DRIFT", "BTC"]

LISTING_AUDIT_COLUMNS = [
    "token",
    "binance_live_symbols",
    "binance_live_contract_types",
    "binance_live_statuses",
    "binance_live_onboard_time_utc",
    "raw_watchboard_venues",
    "raw_watchboard_binance_symbols",
    "clean_watchboard_venues",
    "clean_watchboard_binance_symbols",
    "latest_archived_snapshot",
    "archived_clean_venues",
    "archived_clean_binance_symbols",
    "dropped_stage",
    "audit_note",
]

TOKEN_AUDIT_COLUMNS = [
    "token",
    "selected_coingecko_id",
    "candidate_count",
    "match_status",
    "current_price_usd",
    "volume_24h_usd",
    "market_cap_usd",
    "market_data_as_of",
]


def log(message: str):
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[Audit] [{timestamp}] {message}")


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate listing and token-market audit CSVs.")
    parser.add_argument(
        "--tokens",
        nargs="*",
        default=DEFAULT_TOKENS,
        help="Sample tokens to audit (default: predefined audit sample set).",
    )
    return parser.parse_args()


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    ensure_directory_layout()
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def latest_snapshot_date(history_root: Path) -> str:
    snapshot_dirs = [
        path.name
        for path in history_root.iterdir()
        if path.is_dir() and len(path.name) == 10 and path.name[4] == "-" and path.name[7] == "-"
    ] if history_root.exists() else []
    return max(snapshot_dirs) if snapshot_dirs else ""


def token_rows(rows: list[dict], token_field: str, token: str) -> list[dict]:
    token_upper = clean_text(token).upper()
    return [row for row in rows if clean_text(row.get(token_field)).upper() == token_upper]


def venue_list(rows: list[dict], venue_field: str = "venue") -> str:
    values = sorted({clean_text(row.get(venue_field)) for row in rows if clean_text(row.get(venue_field))})
    return ", ".join(values)


def symbol_list(rows: list[dict], venue: str = "") -> str:
    filtered = rows
    if venue:
        filtered = [row for row in rows if clean_text(row.get("venue")).lower() == venue.lower()]
    values = sorted({clean_text(row.get("symbol_raw")) or clean_text(row.get("symbol")) for row in filtered if clean_text(row.get("symbol_raw")) or clean_text(row.get("symbol"))})
    return ", ".join(values)


def format_binance_onboard_date(rows: list[dict]) -> str:
    timestamps = []
    for row in rows:
        value = row.get("onboardDate")
        try:
            timestamp_ms = int(value)
        except (TypeError, ValueError):
            continue
        timestamps.append(datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).replace(microsecond=0).isoformat())
    return ", ".join(sorted(set(timestamps)))


def fetch_live_binance_symbols() -> dict[str, list[dict]]:
    response = requests.get(BINANCE_EXCHANGE_INFO_URL, timeout=30)
    response.raise_for_status()
    grouped = {}
    for item in response.json().get("symbols", []):
        base_asset = clean_text(item.get("baseAsset")).upper()
        contract_type = clean_text(item.get("contractType")).upper()
        if not base_asset or not contract_type.endswith("PERPETUAL"):
            continue
        grouped.setdefault(base_asset, []).append(item)
    return grouped


def build_listing_audit_rows(tokens: list[str]) -> list[dict]:
    raw_rows = load_rows(RAW_WATCHBOARD_FILE)
    clean_rows = load_rows(CLEAN_WATCHBOARD_FILE)
    latest_snapshot = latest_snapshot_date(HISTORY_ROOT)
    archived_rows = load_rows(HISTORY_ROOT / latest_snapshot / CLEAN_WATCHBOARD_FILE.name) if latest_snapshot else []
    live_binance = fetch_live_binance_symbols()
    rows = []

    for token in tokens:
        raw_token_rows = token_rows(raw_rows, "base_asset", token)
        clean_token_rows = token_rows(clean_rows, "base_asset", token)
        archived_token_rows = token_rows(archived_rows, "base_asset", token)
        live_rows = live_binance.get(clean_text(token).upper(), [])

        live_binance_present = bool(live_rows)
        raw_binance_present = any(clean_text(row.get("venue")).lower() == "binance" for row in raw_token_rows)
        clean_binance_present = any(clean_text(row.get("venue")).lower() == "binance" for row in clean_token_rows)
        archived_binance_present = any(clean_text(row.get("venue")).lower() == "binance" for row in archived_token_rows)

        dropped_stage = ""
        audit_note = ""
        if live_binance_present and not raw_binance_present:
            dropped_stage = "fetch/state"
            contract_types = sorted({clean_text(row.get("contractType")) for row in live_rows if clean_text(row.get("contractType"))})
            if any(contract_type == "TRADIFI_PERPETUAL" for contract_type in contract_types):
                audit_note = "Live Binance listing uses TRADIFI_PERPETUAL and was excluded by the old fetch filter."
            else:
                audit_note = "Live Binance listing exists but is missing from the raw local watchboard."
        elif raw_binance_present and not clean_binance_present:
            dropped_stage = "cleaning"
            audit_note = "Row exists in the raw watchboard but is missing after cleaning."
        elif clean_binance_present and latest_snapshot and not archived_binance_present:
            dropped_stage = "snapshot/archive"
            audit_note = "Row exists in the cleaned watchboard but was not captured in the latest archived snapshot."
        elif live_binance_present:
            dropped_stage = "none"
            audit_note = "Live Binance listing is present across the current local layers."
        else:
            dropped_stage = "n/a"
            audit_note = "No live Binance perp found for this token in the current exchangeInfo response."

        rows.append(
            {
                "token": token,
                "binance_live_symbols": ", ".join(sorted({clean_text(row.get("symbol")) for row in live_rows if clean_text(row.get("symbol"))})),
                "binance_live_contract_types": ", ".join(sorted({clean_text(row.get("contractType")) for row in live_rows if clean_text(row.get("contractType"))})),
                "binance_live_statuses": ", ".join(sorted({clean_text(row.get("status")) for row in live_rows if clean_text(row.get("status"))})),
                "binance_live_onboard_time_utc": format_binance_onboard_date(live_rows),
                "raw_watchboard_venues": venue_list(raw_token_rows),
                "raw_watchboard_binance_symbols": symbol_list(raw_token_rows, venue="binance"),
                "clean_watchboard_venues": venue_list(clean_token_rows),
                "clean_watchboard_binance_symbols": symbol_list(clean_token_rows, venue="binance"),
                "latest_archived_snapshot": latest_snapshot,
                "archived_clean_venues": venue_list(archived_token_rows),
                "archived_clean_binance_symbols": symbol_list(archived_token_rows, venue="binance"),
                "dropped_stage": dropped_stage,
                "audit_note": audit_note,
            }
        )

    return rows


def build_token_audit_rows(tokens: list[str]) -> list[dict]:
    audit_rows = load_rows(TOKEN_MATCH_AUDIT_FILE)
    audit_lookup = {clean_text(row.get("token")).upper(): row for row in audit_rows}
    rows = []

    for token in tokens:
        match_row = audit_lookup.get(clean_text(token).upper(), {})
        rows.append(
            {
                "token": token,
                "selected_coingecko_id": clean_text(match_row.get("selected_coingecko_id")),
                "candidate_count": clean_text(match_row.get("candidate_count")),
                "match_status": clean_text(match_row.get("match_status")),
                "current_price_usd": clean_text(match_row.get("current_price_usd")),
                "volume_24h_usd": clean_text(match_row.get("volume_24h_usd")),
                "market_cap_usd": clean_text(match_row.get("market_cap_usd")),
                "market_data_as_of": clean_text(match_row.get("market_data_as_of")),
            }
        )

    return rows


def main():
    args = parse_args()
    tokens = [clean_text(token).upper() for token in args.tokens if clean_text(token)]
    if not tokens:
        raise ValueError("No tokens provided for the audit.")

    if not TOKEN_MATCH_AUDIT_FILE.exists():
        raise FileNotFoundError(
            f"CoinGecko match audit not found: {TOKEN_MATCH_AUDIT_FILE}. Run python src/transform/enrich_watchboard_coingecko.py first."
        )

    listing_rows = build_listing_audit_rows(tokens)
    token_rows_out = build_token_audit_rows(tokens)

    write_csv(LISTING_AUDIT_FILE, listing_rows, LISTING_AUDIT_COLUMNS)
    write_csv(TOKEN_AUDIT_FILE, token_rows_out, TOKEN_AUDIT_COLUMNS)

    log(f"Wrote listing coverage audit to {LISTING_AUDIT_FILE.name}")
    log(f"Wrote token market audit to {TOKEN_AUDIT_FILE.name}")


if __name__ == "__main__":
    main()
