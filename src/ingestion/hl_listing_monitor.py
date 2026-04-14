from __future__ import annotations

"""
Perp Listing Watchboard v0.3
----------------------------
Lightweight perp-listing intelligence across:
  - Hyperliquid
  - Binance Futures
  - Bybit
  - OKX
  - Bitget
  - dYdX
  - Drift

- `poll` mode: checks every 30 minutes, alerts on new listings
- `daily-summary` mode: sends one 24h heartbeat summary
- `snapshot` mode: one-shot refresh of local listing state and raw watchboard, no Lark push
- also writes a local aggregated CSV watchboard

First run initializes local state and sends no alert.
"""

import argparse
import csv
import json
import os
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.paths import ENV_FILE, RAW_WATCHBOARD_FILE, STATE_FILE

# Silence a noisy local macOS SSL backend warning; requests still works for this use case.
warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*",
    category=Warning,
)

import requests

load_dotenv(ENV_FILE)


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

LARK_WEBHOOK_URL = os.getenv("LARK_WEBHOOK_URL", "").strip()
CHECK_INTERVAL_MINUTES = 30
SUMMARY_LOOKBACK_HOURS = 24

WATCHBOARD_FILE = RAW_WATCHBOARD_FILE

WATCHBOARD_COLUMNS = [
    "venue",
    "symbol",
    "base_asset",
    "quote_asset",
    "contract_type",
    "first_seen_at",
    "metadata_json",
]

HL_API_URL = "https://api.hyperliquid.xyz/info"
BINANCE_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
OKX_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
BITGET_CONTRACTS_URL = "https://api.bitget.com/api/v2/mix/market/contracts"
DYDX_MARKETS_URL = "https://indexer.dydx.trade/v4/perpetualMarkets"
DRIFT_MARKETS_URL = "https://data.api.drift.trade/stats/markets"


# ─────────────────────────────────────────────
# Time / logging
# ─────────────────────────────────────────────

def now_local() -> datetime:
    return datetime.now().astimezone()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def log(level: str, message: str):
    print(f"[{format_ts(now_local())}] [{level}] {message}")


# ─────────────────────────────────────────────
# Listing row helpers
# ─────────────────────────────────────────────

def serialize_metadata(metadata: dict) -> str:
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True)


def make_listing_row(
    venue: str,
    symbol: str,
    base_asset: str,
    quote_asset: str,
    contract_type: str,
    metadata: dict,
    first_seen_at: str | None = None,
) -> dict:
    return {
        "venue": venue,
        "symbol": symbol,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "contract_type": contract_type,
        "first_seen_at": first_seen_at,
        "metadata_json": serialize_metadata(metadata),
    }


def split_pair_symbol(symbol: str, separator: str = "-") -> tuple[str, str]:
    parts = symbol.split(separator)
    if len(parts) >= 2:
        return parts[0], parts[1]
    return symbol, ""


def parse_okx_inst_id(inst_id: str) -> tuple[str, str]:
    parts = inst_id.split("-")
    if len(parts) >= 3:
        return parts[0], parts[1]
    return inst_id, ""


def listing_brief(row: dict, token_venue_counts: dict[str, int] | None = None) -> str:
    token = row.get("base_asset") or row.get("symbol", "?")
    symbol = row.get("symbol", "?")
    quote_asset = row.get("quote_asset", "")
    contract_type = row.get("contract_type", "")
    venue_count = token_venue_counts.get(token, 1) if token_venue_counts else None

    parts = [symbol]
    if token != symbol:
        parts.append(f"token {token}")
    if quote_asset:
        parts.append(f"quote {quote_asset}")
    if contract_type:
        parts.append(contract_type)
    if venue_count is not None:
        parts.append(f"{venue_count} venue(s)")

    return " | ".join(parts)


def token_key(row: dict) -> str:
    return (row.get("base_asset") or row.get("symbol") or "").upper()


# ─────────────────────────────────────────────
# Venue fetchers
# ─────────────────────────────────────────────

def fetch_hyperliquid_listings() -> dict:
    response = requests.post(
        HL_API_URL,
        json={"type": "meta"},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    response.raise_for_status()

    data = response.json()
    universe = data.get("universe", [])
    if not isinstance(universe, list):
        raise ValueError("Unexpected Hyperliquid response: missing universe list")

    listings = {}
    for asset in universe:
        symbol = asset.get("name")
        if not symbol:
            continue

        metadata = {
            "szDecimals": asset.get("szDecimals"),
            "maxLeverage": asset.get("maxLeverage"),
            "onlyIsolated": asset.get("onlyIsolated", False),
        }
        listings[symbol] = make_listing_row(
            venue="hyperliquid",
            symbol=symbol,
            base_asset=symbol,
            quote_asset="USD",
            contract_type="perpetual",
            metadata=metadata,
        )
    return listings


def _binance_max_leverage(symbol_info: dict) -> int | None:
    for item in symbol_info.get("filters", []):
        if item.get("filterType") == "LEVERAGE":
            return item.get("maxLeverage")
    return None


def _binance_supported_contract_type(contract_type: str) -> bool:
    contract_type_text = str(contract_type or "").strip().upper()
    return contract_type_text.endswith("PERPETUAL")


def fetch_binance_futures_listings() -> dict:
    response = requests.get(BINANCE_EXCHANGE_INFO_URL, timeout=10)
    response.raise_for_status()

    listings = {}
    for item in response.json().get("symbols", []):
        if not _binance_supported_contract_type(item.get("contractType", "")):
            continue
        if item.get("status") != "TRADING":
            continue

        symbol = item.get("symbol", "")
        base_asset = item.get("baseAsset", "")
        quote_asset = item.get("quoteAsset", "")
        if not symbol or not base_asset:
            continue

        metadata = {
            "status": item.get("status"),
            "pair": item.get("pair"),
            "contractType": item.get("contractType"),
            "marginAsset": item.get("marginAsset"),
            "onboardDate": item.get("onboardDate"),
            "underlyingType": item.get("underlyingType"),
            "underlyingSubType": item.get("underlyingSubType"),
            "maxLeverage": _binance_max_leverage(item),
        }
        listings[symbol] = make_listing_row(
            venue="binance",
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            contract_type="linear_perpetual",
            metadata=metadata,
        )
    return listings


def fetch_bybit_listings() -> dict:
    listings = {}
    cursor = ""

    while True:
        params = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor

        response = requests.get(BYBIT_INSTRUMENTS_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("retCode") != 0:
            raise ValueError(f"Bybit API error: {data.get('retMsg')}")

        result = data.get("result", {})
        for item in result.get("list", []):
            if item.get("contractType") != "LinearPerpetual":
                continue
            if item.get("status") != "Trading":
                continue

            symbol = item.get("symbol", "")
            base_asset = item.get("baseCoin", "")
            quote_asset = item.get("quoteCoin", "")
            if not symbol or not base_asset:
                continue

            metadata = {
                "launchTime": item.get("launchTime"),
                "settleCoin": item.get("settleCoin"),
                "maxLeverage": item.get("leverageFilter", {}).get("maxLeverage"),
                "status": item.get("status"),
            }
            listings[symbol] = make_listing_row(
                venue="bybit",
                symbol=symbol,
                base_asset=base_asset,
                quote_asset=quote_asset,
                contract_type="linear_perpetual",
                metadata=metadata,
            )

        cursor = result.get("nextPageCursor", "")
        if not cursor:
            break

    return listings


def fetch_okx_listings() -> dict:
    response = requests.get(OKX_INSTRUMENTS_URL, params={"instType": "SWAP"}, timeout=10)
    response.raise_for_status()
    data = response.json()
    if data.get("code") != "0":
        raise ValueError(f"OKX API error: {data.get('msg')}")

    listings = {}
    for item in data.get("data", []):
        if item.get("instType") != "SWAP":
            continue
        if item.get("state") != "live":
            continue

        symbol = item.get("instId", "")
        if not symbol:
            continue

        parsed_base, parsed_quote = parse_okx_inst_id(symbol)
        base_asset = item.get("baseCcy") or parsed_base
        quote_asset = item.get("quoteCcy") or parsed_quote
        ct_type = item.get("ctType", "")
        contract_type = f"{ct_type}_perpetual" if ct_type else "perpetual"

        metadata = {
            "instFamily": item.get("instFamily"),
            "uly": item.get("uly"),
            "state": item.get("state"),
            "settleCcy": item.get("settleCcy"),
            "lever": item.get("lever"),
            "listTime": item.get("listTime"),
            "ctVal": item.get("ctVal"),
            "ctValCcy": item.get("ctValCcy"),
        }
        listings[symbol] = make_listing_row(
            venue="okx",
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            contract_type=contract_type,
            metadata=metadata,
        )
    return listings


def fetch_bitget_listings() -> dict:
    response = requests.get(
        BITGET_CONTRACTS_URL,
        params={"productType": "USDT-FUTURES"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != "00000":
        raise ValueError(f"Bitget API error: {data.get('msg')}")

    listings = {}
    for item in data.get("data", []):
        if item.get("symbolType") != "perpetual":
            continue
        if item.get("symbolStatus") != "normal":
            continue

        symbol = item.get("symbol", "")
        base_asset = item.get("baseCoin", "")
        quote_asset = item.get("quoteCoin", "")
        if not symbol or not base_asset:
            continue

        metadata = {
            "productType": "USDT-FUTURES",
            "symbolStatus": item.get("symbolStatus"),
            "maxLever": item.get("maxLever"),
            "minLever": item.get("minLever"),
            "launchTime": item.get("launchTime"),
            "supportMarginCoins": item.get("supportMarginCoins", []),
        }
        listings[symbol] = make_listing_row(
            venue="bitget",
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            contract_type="linear_perpetual",
            metadata=metadata,
        )
    return listings


def fetch_dydx_listings() -> dict:
    response = requests.get(DYDX_MARKETS_URL, timeout=10)
    response.raise_for_status()

    data = response.json()
    markets = data.get("markets", {})
    if not isinstance(markets, dict):
        raise ValueError("Unexpected dYdX response: missing markets map")

    listings = {}
    for symbol, item in markets.items():
        if item.get("status") != "ACTIVE":
            continue

        base_asset, quote_asset = split_pair_symbol(symbol)
        metadata = {
            "ticker": item.get("ticker"),
            "status": item.get("status"),
            "marketType": item.get("marketType"),
            "clobPairId": item.get("clobPairId"),
            "tickSize": item.get("tickSize"),
            "stepSize": item.get("stepSize"),
        }
        listings[symbol] = make_listing_row(
            venue="dydx",
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            contract_type="cross_perpetual",
            metadata=metadata,
        )
    return listings


def fetch_drift_listings() -> dict:
    response = requests.get(DRIFT_MARKETS_URL, timeout=10)
    response.raise_for_status()

    data = response.json()
    markets = data.get("markets", [])
    if not isinstance(markets, list):
        raise ValueError("Unexpected Drift response: missing markets list")

    listings = {}
    for item in markets:
        if item.get("marketType") != "perp":
            continue
        if item.get("status") != "active":
            continue

        symbol = item.get("symbol", "")
        base_asset = item.get("baseAsset", "")
        quote_asset = item.get("quoteAsset", "")
        if not symbol or not base_asset:
            continue

        metadata = {
            "marketIndex": item.get("marketIndex"),
            "status": item.get("status"),
            "uiStatus": item.get("uiStatus"),
            "precision": item.get("precision"),
            "limits": item.get("limits", {}),
        }
        listings[symbol] = make_listing_row(
            venue="drift",
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            contract_type="perpetual",
            metadata=metadata,
        )
    return listings


VENUES = {
    "hyperliquid": {
        "display_name": "Hyperliquid",
        "market_label": "perp",
        "listings_url": "https://app.hyperliquid.xyz/trade",
        "fetch_listings": fetch_hyperliquid_listings,
    },
    "binance": {
        "display_name": "Binance Futures",
        "market_label": "perp",
        "listings_url": "https://www.binance.com/en/futures",
        "fetch_listings": fetch_binance_futures_listings,
    },
    "bybit": {
        "display_name": "Bybit",
        "market_label": "perp",
        "listings_url": "https://www.bybit.com/trade/usdt/",
        "fetch_listings": fetch_bybit_listings,
    },
    "okx": {
        "display_name": "OKX",
        "market_label": "perp",
        "listings_url": "https://www.okx.com/trade-swap",
        "fetch_listings": fetch_okx_listings,
    },
    "bitget": {
        "display_name": "Bitget",
        "market_label": "perp",
        "listings_url": "https://www.bitget.com/futures/usdt",
        "fetch_listings": fetch_bitget_listings,
    },
    "dydx": {
        "display_name": "dYdX",
        "market_label": "perp",
        "listings_url": "https://dydx.trade/markets",
        "fetch_listings": fetch_dydx_listings,
    },
    "drift": {
        "display_name": "Drift",
        "market_label": "perp",
        "listings_url": "https://app.drift.trade/",
        "fetch_listings": fetch_drift_listings,
    },
}
ALL_VENUES = tuple(VENUES.keys())


# ─────────────────────────────────────────────
# State / watchboard helpers
# ─────────────────────────────────────────────

def default_venue_state() -> dict:
    return {
        "known_listings": {},
        "known_listings_updated_at": None,
        "detected_events": [],
        "last_heartbeat_date": None,
    }


def normalize_state(raw_state: dict) -> dict:
    state = {"venues": {}}

    if not raw_state:
        return state

    if isinstance(raw_state, dict) and isinstance(raw_state.get("venues"), dict):
        state["venues"] = raw_state["venues"]
    elif isinstance(raw_state, dict):
        venue_state = default_venue_state()
        if any(
            key in raw_state
            for key in ("known_listings", "known_listings_updated_at", "detected_events", "last_heartbeat_date")
        ):
            venue_state["known_listings"] = raw_state.get("known_listings", {})
            venue_state["known_listings_updated_at"] = raw_state.get("known_listings_updated_at")
            venue_state["detected_events"] = raw_state.get("detected_events", [])
            venue_state["last_heartbeat_date"] = raw_state.get("last_heartbeat_date")
        else:
            venue_state["known_listings"] = raw_state
        state["venues"]["hyperliquid"] = venue_state
    else:
        raise ValueError("Unexpected state file format")

    for venue_key, venue_state in list(state["venues"].items()):
        merged = default_venue_state()
        if isinstance(venue_state, dict):
            merged.update(venue_state)
        if not isinstance(merged["known_listings"], dict):
            merged["known_listings"] = {}
        if not isinstance(merged["detected_events"], list):
            merged["detected_events"] = []
        state["venues"][venue_key] = merged

    return state


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"venues": {}}

    with STATE_FILE.open("r", encoding="utf-8") as handle:
        raw_state = json.load(handle)
    return normalize_state(raw_state)


def save_state(state: dict):
    normalized = normalize_state(state)
    temp_path = STATE_FILE.with_name(f"{STATE_FILE.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(normalized, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    temp_path.replace(STATE_FILE)


def get_venue_state(state: dict, venue_key: str) -> dict:
    venues = state.setdefault("venues", {})
    venue_state = venues.get(venue_key)
    if not isinstance(venue_state, dict):
        venue_state = default_venue_state()
        venues[venue_key] = venue_state
        return venue_state

    merged = default_venue_state()
    merged.update(venue_state)
    if not isinstance(merged["known_listings"], dict):
        merged["known_listings"] = {}
    if not isinstance(merged["detected_events"], list):
        merged["detected_events"] = []
    venues[venue_key] = merged
    return merged


def ensure_listing_row(venue_key: str, symbol: str, raw_row: dict, fallback_first_seen_at: str | None) -> dict:
    if not isinstance(raw_row, dict):
        raw_row = {}

    if raw_row.get("venue") == venue_key and raw_row.get("symbol") == symbol and raw_row.get("base_asset"):
        row = dict(raw_row)
        row["first_seen_at"] = row.get("first_seen_at") or fallback_first_seen_at or now_utc().isoformat()
        metadata_json = row.get("metadata_json", "{}")
        if not isinstance(metadata_json, str):
            row["metadata_json"] = serialize_metadata(metadata_json)
        return row

    if venue_key == "hyperliquid":
        return make_listing_row(
            venue=venue_key,
            symbol=symbol,
            base_asset=symbol,
            quote_asset="USD",
            contract_type="perpetual",
            metadata=raw_row,
            first_seen_at=fallback_first_seen_at,
        )
    if venue_key == "binance":
        return make_listing_row(
            venue=venue_key,
            symbol=symbol,
            base_asset=raw_row.get("baseAsset") or symbol,
            quote_asset=raw_row.get("quoteAsset", ""),
            contract_type="linear_perpetual",
            metadata=raw_row,
            first_seen_at=fallback_first_seen_at,
        )
    if venue_key == "bybit":
        return make_listing_row(
            venue=venue_key,
            symbol=symbol,
            base_asset=raw_row.get("baseCoin") or symbol,
            quote_asset=raw_row.get("quoteCoin", ""),
            contract_type="linear_perpetual",
            metadata=raw_row,
            first_seen_at=fallback_first_seen_at,
        )

    base_asset, quote_asset = split_pair_symbol(symbol)
    return make_listing_row(
        venue=venue_key,
        symbol=symbol,
        base_asset=raw_row.get("base_asset") or base_asset,
        quote_asset=raw_row.get("quote_asset") or quote_asset,
        contract_type=raw_row.get("contract_type", "perpetual"),
        metadata=raw_row,
        first_seen_at=fallback_first_seen_at,
    )


def merge_current_listings(previous_known: dict, current_listings: dict, snapshot_time: str, venue_key: str) -> dict:
    merged = {}
    for symbol, row in current_listings.items():
        previous_row = previous_known.get(symbol, {})
        previous_first_seen = previous_row.get("first_seen_at") if isinstance(previous_row, dict) else None
        normalized_row = dict(row)
        normalized_row["first_seen_at"] = previous_first_seen or snapshot_time
        merged[symbol] = normalized_row
    return merged


def update_known_listings_snapshot(venue_state: dict, current_listings: dict, snapshot_time: str, venue_key: str) -> dict:
    previous_known = venue_state.get("known_listings", {})
    merged = merge_current_listings(previous_known, current_listings, snapshot_time, venue_key)
    venue_state["known_listings"] = merged
    venue_state["known_listings_updated_at"] = snapshot_time
    return merged


def collect_watchboard_rows(state: dict, overrides: dict[str, dict] | None = None) -> list[dict]:
    overrides = overrides or {}
    rows = []
    venues = state.get("venues", {})

    for venue_key in ALL_VENUES:
        venue_state = venues.get(venue_key, {})
        known_listings = overrides.get(venue_key, venue_state.get("known_listings", {}))
        fallback_first_seen = venue_state.get("known_listings_updated_at")

        for symbol in sorted(known_listings):
            rows.append(ensure_listing_row(venue_key, symbol, known_listings[symbol], fallback_first_seen))

    rows.sort(key=lambda row: (row["venue"], row["symbol"]))
    return rows


def export_watchboard_csv(state: dict):
    rows = collect_watchboard_rows(state)
    with WATCHBOARD_FILE.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=WATCHBOARD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def persist_state(state: dict):
    save_state(state)
    export_watchboard_csv(state)


def build_token_venue_counts(state: dict, overrides: dict[str, dict] | None = None) -> dict[str, int]:
    token_venues = {}
    for row in collect_watchboard_rows(state, overrides=overrides):
        token = token_key(row)
        if not token:
            continue
        token_venues.setdefault(token, set()).add(row["venue"])
    return {token: len(venues) for token, venues in token_venues.items()}


# ─────────────────────────────────────────────
# Detection / summary helpers
# ─────────────────────────────────────────────

def parse_event_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def record_detected_events(venue_state: dict, new_rows: dict, detected_at: str, source: str):
    for row in new_rows.values():
        venue_state["detected_events"].append(
            {
                "symbol": row["symbol"],
                "base_asset": row["base_asset"],
                "quote_asset": row["quote_asset"],
                "contract_type": row["contract_type"],
                "detected_at": detected_at,
                "source": source,
            }
        )


def recent_events_for_summary(events: list[dict]) -> list[dict]:
    cutoff = now_utc() - timedelta(hours=SUMMARY_LOOKBACK_HOURS)
    recent = []
    for event in events:
        detected_at = parse_event_time(event.get("detected_at", ""))
        if detected_at and detected_at >= cutoff:
            recent.append((detected_at, event))
    recent.sort(key=lambda item: item[0])
    return [event for _, event in recent]


def build_daily_summary_rows(venue_state: dict, current_listings: dict) -> tuple[list[dict], dict]:
    recent_events = recent_events_for_summary(venue_state["detected_events"])
    known = venue_state["known_listings"]
    snapshot_new_symbols = {symbol: current_listings[symbol] for symbol in sorted(current_listings) if symbol not in known}

    ordered_rows = []
    seen_symbols = set()

    for event in recent_events:
        symbol = event.get("symbol")
        if not symbol or symbol in seen_symbols:
            continue
        ordered_rows.append(
            {
                "venue": "",
                "symbol": symbol,
                "base_asset": event.get("base_asset") or symbol,
                "quote_asset": event.get("quote_asset", ""),
                "contract_type": event.get("contract_type", "perpetual"),
            }
        )
        seen_symbols.add(symbol)

    for symbol, row in snapshot_new_symbols.items():
        if symbol in seen_symbols:
            continue
        ordered_rows.append(row)
        seen_symbols.add(symbol)

    return ordered_rows, snapshot_new_symbols


def initialize_state_if_needed(state: dict, venue_key: str, current_listings: dict) -> bool:
    venue = VENUES[venue_key]
    venue_state = get_venue_state(state, venue_key)
    if venue_state["known_listings"]:
        return False

    snapshot_time = now_utc().isoformat()
    update_known_listings_snapshot(venue_state, current_listings, snapshot_time, venue_key)
    persist_state(state)
    log(
        "INIT",
        f"First run for {venue['display_name']}: recorded {len(current_listings)} current "
        f"{venue['market_label']} listings and sent no alert.",
    )
    return True


# ─────────────────────────────────────────────
# Lark formatting / delivery
# ─────────────────────────────────────────────

def send_lark(message: str) -> bool:
    if not LARK_WEBHOOK_URL:
        log("WARN", "LARK_WEBHOOK_URL is not set; skipping Lark push.")
        return False

    payload = {"msg_type": "text", "content": {"text": message}}
    try:
        response = requests.post(LARK_WEBHOOK_URL, json=payload, timeout=5)
    except requests.RequestException as exc:
        log("ERROR", f"Lark push request failed: {exc}")
        return False

    body_preview = response.text.strip()[:300]
    if response.status_code != 200:
        log("ERROR", f"Lark push failed with HTTP {response.status_code}: {body_preview}")
        return False

    try:
        data = response.json()
    except ValueError:
        log("ERROR", f"Lark push returned HTTP 200 but non-JSON body: {body_preview}")
        return False

    code = data.get("code", data.get("StatusCode"))
    message_text = data.get("msg") or data.get("message") or data.get("StatusMessage") or ""
    status_text = str(data.get("status", "")).lower()

    if code in (0, "0") or message_text.lower() == "success" or status_text == "success":
        log("INFO", "Lark push confirmed.")
        return True

    log("ERROR", f"Lark push not acknowledged as success: {data}")
    return False


def format_event_alert(venue_key: str, new_rows: dict) -> str:
    venue = VENUES[venue_key]
    lines = [
        f"🚀 {venue['display_name']} new perp listing alert",
        f"Time: {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"New listings: {len(new_rows)}",
        "----------------------",
    ]
    for row in new_rows.values():
        lines.append(f"- {listing_brief(row)}")
    lines.append("----------------------")
    lines.append(f"Reference: {venue['listings_url']}")
    return "\n".join(lines)


def format_daily_summary(venue_key: str, summary_rows: list[dict], token_venue_counts: dict[str, int]) -> str:
    venue = VENUES[venue_key]
    lines = [
        f"☀️ {venue['display_name']} perp listing summary",
        f"Time: {format_ts(now_local())}",
        f"Window: last {SUMMARY_LOOKBACK_HOURS} hours",
        "----------------------",
    ]

    if summary_rows:
        lines.append(f"New listings: {len(summary_rows)}")
        for row in summary_rows:
            lines.append(f"- {listing_brief(row, token_venue_counts)}")
    else:
        lines.append(f"No new {venue['display_name']} perp listings in the last 24 hours.")

    lines.append("----------------------")
    lines.append(f"Reference: {venue['listings_url']}")
    lines.append(f"Watchboard: {WATCHBOARD_FILE.name}")
    return "\n".join(lines)


def format_combined_daily_summary(prepared_summaries: list[dict], token_venue_counts: dict[str, int]) -> str:
    lines = [
        "☀️ Perp listing watchboard daily summary",
        f"Time: {format_ts(now_local())}",
        f"Window: last {SUMMARY_LOOKBACK_HOURS} hours",
        "----------------------",
    ]

    any_new = False
    new_tokens = set()

    for prepared in prepared_summaries:
        lines.append(f"{prepared['display_name']}:")
        if prepared["summary_rows"]:
            any_new = True
            for row in prepared["summary_rows"]:
                lines.append(f"- {listing_brief(row, token_venue_counts)}")
                token = token_key(row)
                if token:
                    new_tokens.add(token)
        else:
            lines.append("- No new listings in the last 24 hours.")
        lines.append("----------------------")

    if any_new:
        lines.append("Token venue coverage:")
        for token in sorted(new_tokens):
            lines.append(f"- {token}: {token_venue_counts.get(token, 1)} venue(s)")
        lines.append("----------------------")

    lines.append(f"Watchboard: {WATCHBOARD_FILE.name}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Monitor runtime
# ─────────────────────────────────────────────

def resolve_venue_keys(venue_arg: str) -> list[str]:
    if venue_arg == "all":
        return list(ALL_VENUES)
    return [venue_arg]


def run_event_check(venue_key: str, state: dict | None = None):
    venue = VENUES[venue_key]
    log("INFO", f"Checking {venue['display_name']} {venue['market_label']} listings...")

    current_listings = venue["fetch_listings"]()
    if state is None:
        state = load_state()

    if initialize_state_if_needed(state, venue_key, current_listings):
        return

    venue_state = get_venue_state(state, venue_key)
    new_symbols = [symbol for symbol in sorted(current_listings) if symbol not in venue_state["known_listings"]]

    if not new_symbols:
        log("INFO", f"No new listings. Current total: {len(current_listings)}.")
        return

    snapshot_time = now_utc().isoformat()
    merged_current = merge_current_listings(venue_state["known_listings"], current_listings, snapshot_time, venue_key)
    new_rows = {symbol: merged_current[symbol] for symbol in new_symbols}

    log("INFO", f"Detected {len(new_rows)} new listing(s): {', '.join(new_symbols)}")

    if not send_lark(format_event_alert(venue_key, new_rows)):
        log("WARN", "Alert push not confirmed. Local listing state was not updated; it will retry on the next run.")
        return

    venue_state["known_listings"] = merged_current
    venue_state["known_listings_updated_at"] = snapshot_time
    record_detected_events(venue_state, new_rows, snapshot_time, source="poll")
    persist_state(state)
    log("INFO", f"State updated after confirmed alert delivery. Known total: {len(current_listings)}.")


def prepare_daily_summary(venue_key: str, state: dict) -> dict | None:
    venue = VENUES[venue_key]
    venue_state = get_venue_state(state, venue_key)

    if not venue_state["known_listings"]:
        current_listings = venue["fetch_listings"]()
        log("INFO", f"{venue['display_name']} state is not initialized yet. Initializing from live listings now.")
        initialize_state_if_needed(state, venue_key, current_listings)
        return None

    today = now_local().date().isoformat()
    if venue_state["last_heartbeat_date"] == today:
        log("INFO", f"Daily heartbeat already sent today ({today}); skipping.")
        return None

    current_listings = venue["fetch_listings"]()
    summary_rows, snapshot_new_symbols = build_daily_summary_rows(venue_state, current_listings)

    if snapshot_new_symbols:
        log(
            "INFO",
            f"Daily summary found {len(snapshot_new_symbols)} new listing(s) by comparing current listings to the local snapshot.",
        )
    else:
        log("INFO", "Daily summary found no new listings versus the local snapshot.")

    merged_current = merge_current_listings(venue_state["known_listings"], current_listings, now_utc().isoformat(), venue_key)
    return {
        "venue_key": venue_key,
        "display_name": venue["display_name"],
        "current_listings": current_listings,
        "merged_current": merged_current,
        "summary_rows": summary_rows,
        "snapshot_new_symbols": snapshot_new_symbols,
        "today": today,
    }


def commit_daily_summary(prepared: dict, state: dict):
    venue_state = get_venue_state(state, prepared["venue_key"])
    summary_time = now_utc().isoformat()
    merged_current = update_known_listings_snapshot(
        venue_state,
        prepared["current_listings"],
        summary_time,
        prepared["venue_key"],
    )

    if prepared["snapshot_new_symbols"]:
        new_rows = {symbol: merged_current[symbol] for symbol in prepared["snapshot_new_symbols"]}
        record_detected_events(venue_state, new_rows, summary_time, source="daily-summary")

    venue_state["last_heartbeat_date"] = prepared["today"]


def run_daily_summary(venue_key: str, state: dict | None = None):
    if state is None:
        state = load_state()

    prepared = prepare_daily_summary(venue_key, state)
    if not prepared:
        return

    token_venue_counts = build_token_venue_counts(state, overrides={venue_key: prepared["merged_current"]})
    if not send_lark(format_daily_summary(venue_key, prepared["summary_rows"], token_venue_counts)):
        log("WARN", "Daily heartbeat push not confirmed. last_heartbeat_date was not updated.")
        return

    commit_daily_summary(prepared, state)
    persist_state(state)
    log("INFO", f"Daily heartbeat sent for {prepared['today']}.")


def run_combined_daily_summary(venue_keys: list[str]):
    state = load_state()
    prepared_summaries = []

    for venue_key in venue_keys:
        try:
            prepared = prepare_daily_summary(venue_key, state)
        except requests.RequestException as exc:
            log("ERROR", f"{VENUES[venue_key]['display_name']} request failed: {exc}")
            continue
        except Exception as exc:
            log("ERROR", f"{VENUES[venue_key]['display_name']} unexpected error: {exc}")
            continue

        if prepared:
            prepared_summaries.append(prepared)

    if not prepared_summaries:
        log("INFO", "No combined daily summary to send.")
        return

    overrides = {prepared["venue_key"]: prepared["merged_current"] for prepared in prepared_summaries}
    token_venue_counts = build_token_venue_counts(state, overrides=overrides)

    if not send_lark(format_combined_daily_summary(prepared_summaries, token_venue_counts)):
        log("WARN", "Combined daily heartbeat push not confirmed. Local summary state was not updated.")
        return

    for prepared in prepared_summaries:
        commit_daily_summary(prepared, state)

    persist_state(state)
    log("INFO", f"Combined daily heartbeat sent for {prepared_summaries[0]['today']}.")


def run_poll_cycle(venue_keys: list[str]):
    state = load_state()
    for venue_key in venue_keys:
        try:
            run_event_check(venue_key, state=state)
        except requests.RequestException as exc:
            log("ERROR", f"{VENUES[venue_key]['display_name']} request failed: {exc}")
        except Exception as exc:
            log("ERROR", f"{VENUES[venue_key]['display_name']} unexpected error: {exc}")


def run_poll_loop(venue_keys: list[str]):
    venue_label = ", ".join(VENUES[venue_key]["display_name"] for venue_key in venue_keys)
    log("INFO", f"Starting poll mode for: {venue_label}")
    log("INFO", f"Check interval: every {CHECK_INTERVAL_MINUTES} minutes.")
    log("INFO", f"State file: {STATE_FILE}")
    log("INFO", f"Watchboard file: {WATCHBOARD_FILE}")
    while True:
        run_poll_cycle(venue_keys)
        log("INFO", f"Sleeping for {CHECK_INTERVAL_MINUTES} minutes.")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


def run_snapshot_refresh(venue_keys: list[str]):
    state = load_state()
    snapshot_time = now_utc().isoformat()
    changed_venues = []

    for venue_key in venue_keys:
        try:
            venue = VENUES[venue_key]
            log("INFO", f"Refreshing {venue['display_name']} {venue['market_label']} snapshot...")
            current_listings = venue["fetch_listings"]()

            if initialize_state_if_needed(state, venue_key, current_listings):
                changed_venues.append(venue["display_name"])
                continue

            venue_state = get_venue_state(state, venue_key)
            previous_symbols = set(venue_state["known_listings"])
            current_symbols = set(current_listings)
            added_symbols = sorted(current_symbols - previous_symbols)
            removed_symbols = sorted(previous_symbols - current_symbols)

            update_known_listings_snapshot(venue_state, current_listings, snapshot_time, venue_key)
            changed_venues.append(venue["display_name"])
            log(
                "INFO",
                f"{venue['display_name']}: {len(current_listings)} known listings "
                f"({len(added_symbols)} added, {len(removed_symbols)} removed).",
            )
        except requests.RequestException as exc:
            log("ERROR", f"{VENUES[venue_key]['display_name']} request failed during snapshot refresh: {exc}")
        except Exception as exc:
            log("ERROR", f"{VENUES[venue_key]['display_name']} unexpected snapshot error: {exc}")

    persist_state(state)
    if changed_venues:
        log("INFO", f"Snapshot refresh complete for: {', '.join(changed_venues)}.")
    else:
        log("WARN", "Snapshot refresh finished, but no venue snapshot was updated.")


def main():
    parser = argparse.ArgumentParser(description="Perp listing watchboard: multi-venue, dependency-light, file-based.")
    parser.add_argument(
        "mode",
        nargs="?",
        default="poll",
        choices=["poll", "daily-summary", "snapshot"],
        help="poll = 30-minute loop, daily-summary = one-shot heartbeat, snapshot = one-shot state refresh without Lark",
    )
    parser.add_argument(
        "--venue",
        default="all",
        choices=["all", *sorted(VENUES.keys())],
        help="Venue to monitor: all | hyperliquid | binance | bybit | okx | bitget | dydx | drift",
    )
    args = parser.parse_args()
    venue_keys = resolve_venue_keys(args.venue)

    log("INFO", f"Perp Listing Watchboard v0.3 — {args.venue}")
    log("INFO", f"Mode: {args.mode}")
    log("INFO", f"Lark webhook: {'configured' if LARK_WEBHOOK_URL else 'not configured'}")

    try:
        if args.mode == "daily-summary":
            if args.venue == "all":
                run_combined_daily_summary(venue_keys)
            else:
                run_daily_summary(args.venue)
        elif args.mode == "snapshot":
            run_snapshot_refresh(venue_keys)
        else:
            run_poll_loop(venue_keys)
    except KeyboardInterrupt:
        log("INFO", "Stopped by user.")
    except requests.RequestException as exc:
        log("ERROR", f"Request failed: {exc}")
    except Exception as exc:
        log("ERROR", f"Unexpected error: {exc}")


if __name__ == "__main__":
    main()
