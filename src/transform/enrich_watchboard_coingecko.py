from __future__ import annotations

"""
CoinGecko token-market enrichment and token leaderboard pipeline.

Input:
  - listing_watchboard_clean.csv

Outputs:
  - token_market_metrics.csv
  - token_market_match_audit.csv
  - listing_watchboard_token_metrics.csv
  - top_volume_tokens.csv
  - top_gainers_tokens.csv
  - top_losers_tokens.csv
  - hot_new_tokens.csv

Important modeling distinction:
  - CoinGecko data in this script is token-level aggregated market data.
  - It is used for token-level ranking only.
  - It must not be interpreted as venue-specific Binance / Bybit / OKX volume.

Venue-specific ticker and volume snapshots live in fetch_venue_ticker_metrics.py.
"""

import csv
import os
import re
import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*",
    category=Warning,
)

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.coingecko_overrides import COINGECKO_OVERRIDE_MAP
from src.common.paths import (
    CLEAN_WATCHBOARD_FILE,
    ENRICHMENT_SCRIPT,
    ENV_FILE,
    HOT_NEW_FILE,
    TOKEN_MARKET_FILE,
    TOKEN_MATCH_AUDIT_FILE,
    TOKEN_METRICS_FILE,
    TOP_GAINERS_FILE,
    TOP_LOSERS_FILE,
    TOP_VOLUME_FILE,
)

load_dotenv(ENV_FILE)


COINGECKO_COINS_LIST_URL = "https://api.coingecko.com/api/v3/coins/list"
COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

INPUT_FILE = CLEAN_WATCHBOARD_FILE

TOKEN_MARKET_COLUMNS = [
    "token",
    "coingecko_id",
    "current_price_usd",
    "price_change_24h_pct",
    "volume_24h_usd",
    "market_cap_usd",
    "market_data_as_of",
    "match_status",
]

TOKEN_METRICS_COLUMNS = [
    "token",
    "venue_count",
    "venues",
    "earliest_listing_time_utc",
    "earliest_listing_time_sgt",
    "listing_age_days",
    "coingecko_id",
    "current_price_usd",
    "price_change_24h_pct",
    "volume_24h_usd",
    "market_cap_usd",
    "match_status",
]

TOKEN_MATCH_AUDIT_COLUMNS = [
    "token",
    "selected_coingecko_id",
    "candidate_count",
    "match_status",
    "current_price_usd",
    "volume_24h_usd",
    "market_cap_usd",
    "market_data_as_of",
]

CHUNK_SIZE = 1000
REQUEST_SLEEP_SECONDS = 1.3
RETRY_ATTEMPTS = 4
RETRY_BASE_SLEEP_SECONDS = 5.0

PRICE_CHANGE_MIN_VOLUME_USD = 1_000_000
HOT_NEW_MAX_AGE_DAYS = 30
SGT_TZ = timezone(timedelta(hours=8), name="SGT")

VENUE_LABELS = {
    "binance": "Binance",
    "bitget": "Bitget",
    "bybit": "Bybit",
    "drift": "Drift",
    "dydx": "dYdX",
    "hyperliquid": "Hyperliquid",
    "okx": "OKX",
}


def log(message: str):
    print(f"[CoinGecko Enrich] {message}")


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


def coingecko_headers() -> dict:
    headers = {}
    pro_key = os.getenv("COINGECKO_PRO_API_KEY", "").strip()
    demo_key = os.getenv("COINGECKO_DEMO_API_KEY", "").strip()

    if pro_key:
        headers["x-cg-pro-api-key"] = pro_key
    elif demo_key:
        headers["x-cg-demo-api-key"] = demo_key

    return headers


def coingecko_get(url: str, params: dict | None = None) -> requests.Response:
    last_error = None

    for attempt in range(RETRY_ATTEMPTS):
        response = requests.get(url, headers=coingecko_headers(), params=params, timeout=30)
        if response.status_code != 429:
            response.raise_for_status()
            return response

        retry_after = response.headers.get("Retry-After")
        sleep_seconds = float(retry_after) if retry_after else RETRY_BASE_SLEEP_SECONDS * (2**attempt)
        log(f"CoinGecko rate limit hit (429). Sleeping {sleep_seconds:.1f}s before retry {attempt + 2}/{RETRY_ATTEMPTS}.")
        time.sleep(sleep_seconds)
        last_error = requests.HTTPError(f"429 Too Many Requests: {response.url}", response=response)

    if last_error:
        raise last_error
    raise RuntimeError("CoinGecko request failed without a response")


def normalize_token(token: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean_text(token).upper())


def candidate_symbols(base_asset: str) -> list[str]:
    token = normalize_token(base_asset)
    candidates = []

    if token:
        candidates.append(token)

    stripped_multiplier = re.sub(r"^[0-9]+(?:[KMB])?", "", token)
    if stripped_multiplier and stripped_multiplier not in candidates:
        candidates.append(stripped_multiplier)

    return candidates


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def load_watchboard_rows(input_file: Path) -> list[dict]:
    with input_file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def fetch_coin_list() -> list[dict]:
    response = coingecko_get(COINGECKO_COINS_LIST_URL)
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("Unexpected CoinGecko /coins/list response")
    return data


def build_symbol_map(coin_list: list[dict]) -> dict[str, list[dict]]:
    symbol_map = {}
    for coin in coin_list:
        symbol = normalize_token(coin.get("symbol", ""))
        if not symbol:
            continue
        symbol_map.setdefault(symbol, []).append(coin)
    return symbol_map


def build_coin_id_map(coin_list: list[dict]) -> dict[str, dict]:
    coin_id_map = {}
    for coin in coin_list:
        coin_id = clean_text(coin.get("id"))
        if coin_id:
            coin_id_map[coin_id] = coin
    return coin_id_map


def fetch_market_data_by_ids(ids: list[str]) -> dict[str, dict]:
    markets_by_id = {}
    if not ids:
        return markets_by_id

    unique_ids = sorted(set(ids))
    id_chunks = chunked(unique_ids, CHUNK_SIZE)
    for index, id_chunk in enumerate(id_chunks):
        params = {
            "vs_currencies": "usd",
            "ids": ",".join(id_chunk),
            "include_market_cap": "true",
            "include_24hr_vol": "true",
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        }
        response = coingecko_get(COINGECKO_SIMPLE_PRICE_URL, params=params)
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected CoinGecko /simple/price response")

        for coin_id, market in data.items():
            if not isinstance(market, dict):
                continue
            markets_by_id[coin_id] = {
                "current_price": market.get("usd"),
                "price_change_percentage_24h": market.get("usd_24h_change"),
                "total_volume": market.get("usd_24h_vol"),
                "market_cap": market.get("usd_market_cap"),
                "last_updated_at": market.get("last_updated_at"),
            }

        if index < len(id_chunks) - 1:
            time.sleep(REQUEST_SLEEP_SECONDS)

    return markets_by_id


def resolve_token_matches(unique_tokens: list[str]) -> tuple[dict[str, dict], list[str]]:
    coin_list = fetch_coin_list()
    symbol_map = build_symbol_map(coin_list)
    coin_id_map = build_coin_id_map(coin_list)

    token_resolution = {}
    market_lookup_candidate_ids = set()

    for token in unique_tokens:
        override_entry = COINGECKO_OVERRIDE_MAP.get(token.upper())
        if override_entry:
            override_id = clean_text(override_entry.get("coingecko_id"))
            override_coin = coin_id_map.get(override_id)
            if not override_coin:
                raise ValueError(f"CoinGecko override ID not found in /coins/list: {token} -> {override_id}")

            override_candidate_count = 0
            for candidate_symbol in candidate_symbols(token):
                candidates = symbol_map.get(candidate_symbol, [])
                if candidates:
                    override_candidate_count = len(candidates)
                    break

            token_resolution[token] = {
                "selected_coin": override_coin,
                "candidate_symbol": "",
                "match_status": "matched_override_map",
                "candidate_count": override_candidate_count,
                "override_reason": clean_text(override_entry.get("reason")),
            }
            market_lookup_candidate_ids.add(override_coin["id"])
            continue

        matched = False
        for index, candidate_symbol in enumerate(candidate_symbols(token)):
            candidates = symbol_map.get(candidate_symbol, [])
            if not candidates:
                continue

            match_prefix = "matched_exact_symbol" if index == 0 else "matched_stripped_multiplier"
            if len(candidates) == 1:
                token_resolution[token] = {
                    "selected_coin": candidates[0],
                    "candidate_symbol": candidate_symbol,
                    "match_status": f"{match_prefix}_unique",
                    "candidate_count": 1,
                }
                market_lookup_candidate_ids.add(candidates[0]["id"])
            else:
                token_resolution[token] = {
                    "selected_coin": None,
                    "candidate_symbol": candidate_symbol,
                    "match_status": "unmatched_ambiguous_symbol_candidates",
                    "candidate_count": len(candidates),
                }
            matched = True
            break

        if not matched:
            token_resolution[token] = {
                "selected_coin": None,
                "candidate_symbol": "",
                "match_status": "unmatched_no_symbol_candidate",
                "candidate_count": 0,
            }

    return token_resolution, sorted(market_lookup_candidate_ids)


def write_csv(rows: list[dict], fieldnames: list[str], output_file: Path):
    with output_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize_matches(token_resolution: dict[str, dict]):
    total = len(token_resolution)
    matched = 0
    unmatched = 0
    for resolution in token_resolution.values():
        status = resolution.get("match_status", "")
        if status.startswith("matched_"):
            matched += 1
        else:
            unmatched += 1

    log(f"Resolved {matched}/{total} unique base assets to CoinGecko.")
    log(f"Unmatched base assets: {unmatched}")


def parse_datetime(value: str) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_utc_iso(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).isoformat()


def format_sgt(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(SGT_TZ).strftime("%Y-%m-%d %H:%M:%S SGT")


def format_coingecko_updated_at(value) -> str:
    try:
        epoch_seconds = int(value)
    except (TypeError, ValueError):
        return ""
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).replace(microsecond=0)
    return dt.isoformat()


def numeric_value(value) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def earliest_listing_dt(rows: list[dict]) -> datetime | None:
    candidates = []
    for row in rows:
        dt = parse_datetime(row.get("listing_time_utc", ""))
        if dt:
            candidates.append(dt)
    return min(candidates) if candidates else None


def token_sort_key(row: dict) -> tuple:
    return clean_text(row.get("token")).upper(), clean_text(row.get("coingecko_id"))


def build_token_market_rows(
    unique_tokens: list[str],
    token_resolution: dict[str, dict],
    market_data_by_id: dict[str, dict],
) -> tuple[list[dict], dict[str, dict]]:
    rows = []
    lookup = {}

    for token in sorted(unique_tokens, key=lambda item: normalize_token(item)):
        resolution = token_resolution.get(token, {})
        selected_coin = resolution.get("selected_coin")

        row = {
            "token": token,
            "coingecko_id": "",
            "current_price_usd": "",
            "price_change_24h_pct": "",
            "volume_24h_usd": "",
            "market_cap_usd": "",
            "market_data_as_of": "",
            "match_status": resolution.get("match_status", "unmatched"),
        }

        if selected_coin:
            coin_id = selected_coin["id"]
            market = market_data_by_id.get(coin_id, {})
            row.update(
                {
                    "coingecko_id": coin_id,
                    "current_price_usd": format_number(market.get("current_price")),
                    "price_change_24h_pct": format_number(market.get("price_change_percentage_24h")),
                    "volume_24h_usd": format_number(market.get("total_volume")),
                    "market_cap_usd": format_number(market.get("market_cap")),
                    "market_data_as_of": format_coingecko_updated_at(market.get("last_updated_at")),
                }
            )

        rows.append(row)
        lookup[token.upper()] = row

    return rows, lookup


def build_token_metrics_rows(listing_rows: list[dict], token_market_lookup: dict[str, dict]) -> list[dict]:
    grouped_rows: dict[str, list[dict]] = defaultdict(list)

    for row in listing_rows:
        token = clean_text(row.get("base_asset")).upper()
        if token:
            grouped_rows[token].append(row)

    now_utc = datetime.now(timezone.utc)
    token_rows = []

    for token, rows in grouped_rows.items():
        venue_keys = sorted(
            {
                clean_text(row.get("venue")).lower()
                for row in rows
                if clean_text(row.get("venue"))
            }
        )
        earliest_dt = earliest_listing_dt(rows)
        listing_age_days = ""
        if earliest_dt:
            delta_days = max((now_utc - earliest_dt).total_seconds() / 86400, 0)
            listing_age_days = format_number(round(delta_days, 1))

        market_row = token_market_lookup.get(token, {})
        token_rows.append(
            {
                "token": token,
                "venue_count": len(venue_keys),
                "venues": ", ".join(VENUE_LABELS.get(venue, venue.title()) for venue in venue_keys),
                "earliest_listing_time_utc": format_utc_iso(earliest_dt),
                "earliest_listing_time_sgt": format_sgt(earliest_dt),
                "listing_age_days": listing_age_days,
                "coingecko_id": clean_text(market_row.get("coingecko_id")),
                "current_price_usd": clean_text(market_row.get("current_price_usd")),
                "price_change_24h_pct": clean_text(market_row.get("price_change_24h_pct")),
                "volume_24h_usd": clean_text(market_row.get("volume_24h_usd")),
                "market_cap_usd": clean_text(market_row.get("market_cap_usd")),
                "match_status": clean_text(market_row.get("match_status")),
            }
        )

    token_rows.sort(key=token_sort_key)
    return token_rows


def build_token_match_audit_rows(
    unique_tokens: list[str],
    token_resolution: dict[str, dict],
    market_data_by_id: dict[str, dict],
) -> list[dict]:
    rows = []

    for token in sorted(unique_tokens, key=lambda item: normalize_token(item)):
        resolution = token_resolution.get(token, {})
        selected_coin = resolution.get("selected_coin")
        coin_id = selected_coin.get("id", "") if isinstance(selected_coin, dict) else ""
        market = market_data_by_id.get(coin_id, {}) if coin_id else {}

        rows.append(
            {
                "token": token,
                "selected_coingecko_id": coin_id,
                "candidate_count": resolution.get("candidate_count", 0),
                "match_status": resolution.get("match_status", "unmatched"),
                "current_price_usd": format_number(market.get("current_price")),
                "volume_24h_usd": format_number(market.get("total_volume")),
                "market_cap_usd": format_number(market.get("market_cap")),
                "market_data_as_of": format_coingecko_updated_at(market.get("last_updated_at")),
            }
        )

    return rows


def sort_by_numeric_desc(rows: list[dict], field: str) -> list[dict]:
    eligible = [row for row in rows if numeric_value(row.get(field)) is not None and numeric_value(row.get(field)) > 0]
    return sorted(
        eligible,
        key=lambda row: (-numeric_value(row.get(field)), clean_text(row.get("token")).upper()),
    )


def build_top_gainers(rows: list[dict]) -> list[dict]:
    eligible = []
    for row in rows:
        volume = numeric_value(row.get("volume_24h_usd"))
        change = numeric_value(row.get("price_change_24h_pct"))
        if volume is None or change is None:
            continue
        if volume < PRICE_CHANGE_MIN_VOLUME_USD:
            continue
        eligible.append(row)

    return sorted(
        eligible,
        key=lambda row: (
            -numeric_value(row.get("price_change_24h_pct")),
            -numeric_value(row.get("volume_24h_usd")),
            clean_text(row.get("token")).upper(),
        ),
    )


def build_top_losers(rows: list[dict]) -> list[dict]:
    eligible = []
    for row in rows:
        volume = numeric_value(row.get("volume_24h_usd"))
        change = numeric_value(row.get("price_change_24h_pct"))
        if volume is None or change is None:
            continue
        if volume < PRICE_CHANGE_MIN_VOLUME_USD:
            continue
        eligible.append(row)

    return sorted(
        eligible,
        key=lambda row: (
            numeric_value(row.get("price_change_24h_pct")),
            -numeric_value(row.get("volume_24h_usd")),
            clean_text(row.get("token")).upper(),
        ),
    )


def build_hot_new_tokens(rows: list[dict]) -> list[dict]:
    eligible = []
    for row in rows:
        age_days = numeric_value(row.get("listing_age_days"))
        if age_days is None or age_days > HOT_NEW_MAX_AGE_DAYS:
            continue
        eligible.append(row)

    return sorted(
        eligible,
        key=lambda row: (
            -(numeric_value(row.get("venue_count")) or 0),
            -(numeric_value(row.get("volume_24h_usd")) or 0),
            clean_text(row.get("token")).upper(),
        ),
    )


def main():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Input file not found: {INPUT_FILE}")

    rows = load_watchboard_rows(INPUT_FILE)
    if not rows:
        raise ValueError(f"Input file is empty: {INPUT_FILE}")

    unique_tokens = sorted({clean_text(row.get("base_asset")) for row in rows if clean_text(row.get("base_asset"))})
    log(f"Loaded {len(rows)} cleaned watchboard rows from {INPUT_FILE.name}")
    log(f"Unique tokens to enrich: {len(unique_tokens)}")

    token_resolution, market_lookup_candidate_ids = resolve_token_matches(unique_tokens)
    market_data_by_id = fetch_market_data_by_ids(market_lookup_candidate_ids)

    token_market_rows, token_market_lookup = build_token_market_rows(
        unique_tokens,
        token_resolution,
        market_data_by_id,
    )
    token_match_audit_rows = build_token_match_audit_rows(
        unique_tokens,
        token_resolution,
        market_data_by_id,
    )
    token_metrics_rows = build_token_metrics_rows(rows, token_market_lookup)

    top_volume_rows = sort_by_numeric_desc(token_metrics_rows, "volume_24h_usd")
    top_gainers_rows = build_top_gainers(token_metrics_rows)
    top_losers_rows = build_top_losers(token_metrics_rows)
    hot_new_rows = build_hot_new_tokens(token_metrics_rows)

    write_csv(token_market_rows, TOKEN_MARKET_COLUMNS, TOKEN_MARKET_FILE)
    write_csv(token_match_audit_rows, TOKEN_MATCH_AUDIT_COLUMNS, TOKEN_MATCH_AUDIT_FILE)
    write_csv(token_metrics_rows, TOKEN_METRICS_COLUMNS, TOKEN_METRICS_FILE)
    write_csv(top_volume_rows, TOKEN_METRICS_COLUMNS, TOP_VOLUME_FILE)
    write_csv(top_gainers_rows, TOKEN_METRICS_COLUMNS, TOP_GAINERS_FILE)
    write_csv(top_losers_rows, TOKEN_METRICS_COLUMNS, TOP_LOSERS_FILE)
    write_csv(hot_new_rows, TOKEN_METRICS_COLUMNS, HOT_NEW_FILE)

    summarize_matches(token_resolution)
    log(f"Wrote token-level CoinGecko market metrics to {TOKEN_MARKET_FILE.name}")
    log(f"Wrote CoinGecko token match audit to {TOKEN_MATCH_AUDIT_FILE.name}")
    log(f"Wrote token metrics output to {TOKEN_METRICS_FILE.name}")
    log(f"Wrote top volume leaderboard to {TOP_VOLUME_FILE.name}")
    log(f"Wrote top gainers leaderboard to {TOP_GAINERS_FILE.name} with min volume >= {PRICE_CHANGE_MIN_VOLUME_USD:,.0f} USD")
    log(f"Wrote top losers leaderboard to {TOP_LOSERS_FILE.name} with min volume >= {PRICE_CHANGE_MIN_VOLUME_USD:,.0f} USD")
    log(f"Wrote hot new leaderboard to {HOT_NEW_FILE.name} for tokens listed within {HOT_NEW_MAX_AGE_DAYS} days")


if __name__ == "__main__":
    main()
