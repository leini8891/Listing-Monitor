from __future__ import annotations

"""
Fetch venue-specific perp ticker snapshots for the listing watchboard.

Input:
  - listing_watchboard_clean.csv

Output:
  - venue_ticker_metrics.csv

Important modeling distinction:
  - CoinGecko metrics are token-level aggregated market data.
  - Exchange API metrics in this file are venue-specific ticker snapshots.
  - Use this file for venue drill-down and venue-specific price / volume ranking.
"""

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

from src.common.paths import CLEAN_WATCHBOARD_FILE, VENUE_TICKER_FILE


INPUT_FILE = CLEAN_WATCHBOARD_FILE
OUTPUT_FILE = VENUE_TICKER_FILE

VENUE_TICKER_COLUMNS = [
    "venue",
    "symbol_raw",
    "base_token",
    "quote_asset",
    "last_price",
    "price_change_24h_pct",
    "volume_24h_base",
    "volume_24h_quote",
    "turnover_24h_usd",
    "open_interest",
    "snapshot_time",
]

USD_LIKE_ASSETS = {"USD", "USDT", "USDC"}
SUPPORTED_VENUES = ("binance", "bybit", "okx", "bitget", "dydx")


def log(message: str):
    print(f"[Venue Tickers] {message}")


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def format_number(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return ""
        text = f"{value:.12f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def numeric_value(value) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def ratio_to_percent(value) -> str:
    ratio = numeric_value(value)
    if ratio is None:
        return ""
    return format_number(ratio * 100.0)


def percent_from_open(last_price, open_price) -> str:
    last_value = numeric_value(last_price)
    open_value = numeric_value(open_price)
    if last_value is None or open_value in (None, 0):
        return ""
    return format_number(((last_value - open_value) / open_value) * 100.0)


def product_value(price, size) -> str:
    price_value = numeric_value(price)
    size_value = numeric_value(size)
    if price_value is None or size_value is None:
        return ""
    return format_number(price_value * size_value)


def as_utc_iso_from_ms(value) -> str:
    try:
        timestamp_ms = int(clean_text(value))
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_watchboard_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(rows: list[dict], fieldnames: list[str], output_file: Path):
    with output_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fetch_json(url: str, params: dict | None = None):
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def build_universe(rows: list[dict]) -> dict[str, dict[str, dict]]:
    universe = {venue: {} for venue in SUPPORTED_VENUES}
    for row in rows:
        venue = clean_text(row.get("venue")).lower()
        if venue not in universe:
            continue
        symbol_raw = clean_text(row.get("symbol_raw"))
        if not symbol_raw:
            continue
        universe[venue][symbol_raw] = {
            "venue": venue,
            "symbol_raw": symbol_raw,
            "base_token": clean_text(row.get("base_asset")).upper(),
            "quote_asset": clean_text(row.get("quote_asset")).upper(),
        }
    return universe


def turnover_usd_from_quote(quote_asset: str, quote_volume: str) -> str:
    if quote_asset in USD_LIKE_ASSETS:
        return clean_text(quote_volume)
    return ""


def fetch_binance_rows(universe: dict[str, dict]) -> list[dict]:
    fetch_time = now_utc_iso()
    data = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not isinstance(data, list):
        raise ValueError("Unexpected Binance ticker response")

    by_symbol = {clean_text(item.get("symbol")): item for item in data if clean_text(item.get("symbol"))}
    rows = []
    for symbol_raw, listing_row in universe.items():
        ticker = by_symbol.get(symbol_raw)
        if not ticker:
            continue
        quote_volume = clean_text(ticker.get("quoteVolume"))
        rows.append(
            {
                "venue": "binance",
                "symbol_raw": symbol_raw,
                "base_token": listing_row["base_token"],
                "quote_asset": listing_row["quote_asset"],
                "last_price": clean_text(ticker.get("lastPrice")),
                "price_change_24h_pct": clean_text(ticker.get("priceChangePercent")),
                "volume_24h_base": clean_text(ticker.get("volume")),
                "volume_24h_quote": quote_volume,
                "turnover_24h_usd": turnover_usd_from_quote(listing_row["quote_asset"], quote_volume),
                "open_interest": "",
                "snapshot_time": as_utc_iso_from_ms(ticker.get("closeTime")) or fetch_time,
            }
        )
    return rows


def fetch_bybit_rows(universe: dict[str, dict]) -> list[dict]:
    fetch_time = now_utc_iso()
    tickers_by_symbol = {}

    for category in ("linear", "inverse"):
        data = fetch_json("https://api.bybit.com/v5/market/tickers", {"category": category})
        tickers = data.get("result", {}).get("list", [])
        if not isinstance(tickers, list):
            continue
        for ticker in tickers:
            symbol_raw = clean_text(ticker.get("symbol"))
            if symbol_raw:
                tickers_by_symbol[symbol_raw] = ticker

    rows = []
    for symbol_raw, listing_row in universe.items():
        ticker = tickers_by_symbol.get(symbol_raw)
        if not ticker:
            continue
        quote_volume = clean_text(ticker.get("turnover24h"))
        rows.append(
            {
                "venue": "bybit",
                "symbol_raw": symbol_raw,
                "base_token": listing_row["base_token"],
                "quote_asset": listing_row["quote_asset"],
                "last_price": clean_text(ticker.get("lastPrice")),
                "price_change_24h_pct": ratio_to_percent(ticker.get("price24hPcnt")),
                "volume_24h_base": clean_text(ticker.get("volume24h")),
                "volume_24h_quote": quote_volume,
                "turnover_24h_usd": turnover_usd_from_quote(listing_row["quote_asset"], quote_volume),
                "open_interest": clean_text(ticker.get("openInterest")),
                "snapshot_time": fetch_time,
            }
        )
    return rows


def fetch_okx_rows(universe: dict[str, dict]) -> list[dict]:
    fetch_time = now_utc_iso()
    ticker_data = fetch_json("https://www.okx.com/api/v5/market/tickers", {"instType": "SWAP"})
    oi_data = fetch_json("https://www.okx.com/api/v5/public/open-interest", {"instType": "SWAP"})

    tickers = ticker_data.get("data", [])
    open_interest_rows = oi_data.get("data", [])
    if not isinstance(tickers, list):
        raise ValueError("Unexpected OKX ticker response")
    if not isinstance(open_interest_rows, list):
        open_interest_rows = []

    tickers_by_symbol = {clean_text(item.get("instId")): item for item in tickers if clean_text(item.get("instId"))}
    oi_by_symbol = {clean_text(item.get("instId")): item for item in open_interest_rows if clean_text(item.get("instId"))}

    rows = []
    for symbol_raw, listing_row in universe.items():
        ticker = tickers_by_symbol.get(symbol_raw)
        if not ticker:
            continue
        base_volume = clean_text(ticker.get("volCcy24h"))
        quote_volume = product_value(ticker.get("last"), base_volume)
        oi_row = oi_by_symbol.get(symbol_raw, {})
        rows.append(
            {
                "venue": "okx",
                "symbol_raw": symbol_raw,
                "base_token": listing_row["base_token"],
                "quote_asset": listing_row["quote_asset"],
                "last_price": clean_text(ticker.get("last")),
                "price_change_24h_pct": percent_from_open(ticker.get("last"), ticker.get("open24h")),
                "volume_24h_base": base_volume,
                "volume_24h_quote": quote_volume,
                "turnover_24h_usd": turnover_usd_from_quote(listing_row["quote_asset"], quote_volume),
                "open_interest": clean_text(oi_row.get("oi")),
                "snapshot_time": as_utc_iso_from_ms(ticker.get("ts")) or fetch_time,
            }
        )
    return rows


def fetch_bitget_rows(universe: dict[str, dict]) -> list[dict]:
    tickers_by_symbol = {}

    for product_type in ("USDT-FUTURES", "COIN-FUTURES"):
        data = fetch_json("https://api.bitget.com/api/v2/mix/market/tickers", {"productType": product_type})
        tickers = data.get("data", [])
        if not isinstance(tickers, list):
            continue
        for ticker in tickers:
            symbol_raw = clean_text(ticker.get("symbol"))
            if symbol_raw:
                tickers_by_symbol[symbol_raw] = ticker

    rows = []
    for symbol_raw, listing_row in universe.items():
        ticker = tickers_by_symbol.get(symbol_raw)
        if not ticker:
            continue
        rows.append(
            {
                "venue": "bitget",
                "symbol_raw": symbol_raw,
                "base_token": listing_row["base_token"],
                "quote_asset": listing_row["quote_asset"],
                "last_price": clean_text(ticker.get("lastPr")),
                "price_change_24h_pct": ratio_to_percent(ticker.get("change24h")),
                "volume_24h_base": clean_text(ticker.get("baseVolume")),
                "volume_24h_quote": clean_text(ticker.get("quoteVolume")),
                "turnover_24h_usd": clean_text(ticker.get("usdtVolume")),
                "open_interest": clean_text(ticker.get("holdingAmount")),
                "snapshot_time": as_utc_iso_from_ms(ticker.get("ts")) or now_utc_iso(),
            }
        )
    return rows


def fetch_dydx_rows(universe: dict[str, dict]) -> list[dict]:
    fetch_time = now_utc_iso()
    data = fetch_json("https://indexer.dydx.trade/v4/perpetualMarkets")
    markets = data.get("markets", {})
    if not isinstance(markets, dict):
        raise ValueError("Unexpected dYdX market response")

    rows = []
    for symbol_raw, listing_row in universe.items():
        market = markets.get(symbol_raw)
        if not isinstance(market, dict):
            continue
        if clean_text(market.get("status")).upper() != "ACTIVE":
            continue

        last_price = clean_text(market.get("oraclePrice"))
        quote_volume = clean_text(market.get("volume24H"))
        volume_24h_base = ""
        price_value = numeric_value(last_price)
        quote_volume_value = numeric_value(quote_volume)
        if price_value not in (None, 0) and quote_volume_value is not None:
            volume_24h_base = format_number(quote_volume_value / price_value)

        absolute_change = numeric_value(market.get("priceChange24H"))
        current_price = numeric_value(last_price)
        open_price = None
        if absolute_change is not None and current_price is not None:
            open_price = current_price - absolute_change

        rows.append(
            {
                "venue": "dydx",
                "symbol_raw": symbol_raw,
                "base_token": listing_row["base_token"],
                "quote_asset": listing_row["quote_asset"],
                "last_price": last_price,
                "price_change_24h_pct": percent_from_open(current_price, open_price),
                "volume_24h_base": volume_24h_base,
                "volume_24h_quote": quote_volume,
                "turnover_24h_usd": turnover_usd_from_quote(listing_row["quote_asset"], quote_volume),
                "open_interest": clean_text(market.get("baseOpenInterest")) or clean_text(market.get("openInterest")),
                "snapshot_time": fetch_time,
            }
        )
    return rows


def main():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Input file not found: {INPUT_FILE}")

    watchboard_rows = load_watchboard_rows(INPUT_FILE)
    universe = build_universe(watchboard_rows)

    log(f"Loaded {len(watchboard_rows)} cleaned listing rows from {INPUT_FILE.name}")

    venue_rows = []
    venue_rows.extend(fetch_binance_rows(universe["binance"]))
    venue_rows.extend(fetch_bybit_rows(universe["bybit"]))
    venue_rows.extend(fetch_okx_rows(universe["okx"]))
    venue_rows.extend(fetch_bitget_rows(universe["bitget"]))
    venue_rows.extend(fetch_dydx_rows(universe["dydx"]))

    venue_rows.sort(key=lambda row: (row["venue"], row["base_token"], row["symbol_raw"]))
    write_csv(venue_rows, VENUE_TICKER_COLUMNS, OUTPUT_FILE)

    log(f"Wrote {len(venue_rows)} venue ticker rows to {OUTPUT_FILE.name}")
    for venue in SUPPORTED_VENUES:
        log(f"{venue}: {sum(1 for row in venue_rows if row['venue'] == venue)} rows")


if __name__ == "__main__":
    main()
