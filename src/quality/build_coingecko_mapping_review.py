from __future__ import annotations

"""
One-off identity-resolution worksheet for current review_pending tokens blocked by
missing CoinGecko IDs.

This script is intentionally offline-review oriented:
- It does not update production overrides.
- It does not modify production classification logic.
- It only produces a ranked worksheet of high-confidence mapping candidates.
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

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

from src.common.paths import (  # noqa: E402
    COINGECKO_RWA_UNIVERSE_CACHE_FILE,
    COINGECKO_SEARCH_CACHE_FILE,
    ENV_FILE,
    HISTORY_DB_FILE,
    TOKEN_COINGECKO_MAPPING_REVIEW_FILE,
    ensure_directory_layout,
)
from src.transform.enrich_watchboard_coingecko import (  # noqa: E402
    build_symbol_map,
    candidate_symbols,
    clean_text,
    coingecko_headers,
    fetch_coin_list,
    normalize_token,
)


load_dotenv(ENV_FILE)


COINGECKO_SEARCH_URL = "https://api.coingecko.com/api/v3/search"
COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
SEARCH_CACHE_TTL_DAYS = 30
SEARCH_SLEEP_SECONDS = 3.0
SEARCH_MAX_QUERIES_PER_RUN = 25
MARKET_CHUNK_SIZE = 60

USD_QUOTES = {"USDT", "USDC", "USD", "USD_UM"}
WRAPPER_KEYWORDS = (
    "bridged",
    "wrapped",
    "wormhole",
    "binance-peg",
    "allbridge",
    "neonpass",
    "intents",
    "starkgate",
    "hyperlane",
    "omnibridge",
    "rainbow bridged",
    "linea bridged",
    "base bridged",
    "beam bridged",
    "tac bridged",
    "immutable zkevm bridged",
    "osmosis all",
)
STOCK_PRODUCT_KEYWORDS = (
    "xstock",
    "tokenized stock",
    "dshares",
    "rstock",
    "st0x",
    "wrapped ",
)
HARD_AMBIGUOUS_TOKENS = {"M", "B", "H", "IN", "CL", "XAU", "XAG"}
OUTPUT_COLUMNS = [
    "token",
    "current_blocker",
    "venue_turnover_24h_usd",
    "representative_price_usd",
    "listing_venue_count",
    "venues",
    "overview_visibility_count",
    "candidate_symbol",
    "candidate_source",
    "candidate_count",
    "candidate_coingecko_id",
    "candidate_name",
    "candidate_market_cap_usd",
    "candidate_market_cap_rank",
    "candidate_current_price_usd",
    "price_distance_pct",
    "confidence",
    "cg_broad_rwa_member_if_mapped",
    "mapping_impact_hint",
    "why_this_candidate_is_likely_correct",
    "should_add_to_override",
    "resolution_status",
    "review_note",
]


def log(message: str):
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[CG Mapping Review] [{timestamp}] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a ranked CoinGecko mapping review worksheet for current missing_coingecko_id blockers.")
    parser.add_argument("--db", type=Path, default=HISTORY_DB_FILE, help=f"SQLite history DB (default: {HISTORY_DB_FILE})")
    parser.add_argument("--output", type=Path, default=TOKEN_COINGECKO_MAPPING_REVIEW_FILE, help=f"Output CSV path (default: {TOKEN_COINGECKO_MAPPING_REVIEW_FILE})")
    parser.add_argument("--search-max", type=int, default=SEARCH_MAX_QUERIES_PER_RUN, help=f"Max CoinGecko search queries this run (default: {SEARCH_MAX_QUERIES_PER_RUN})")
    parser.add_argument("--limit", type=int, default=150, help="How many top unresolved tokens to include in the worksheet (default: 150)")
    return parser.parse_args()


def connect_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def latest_snapshot_date(db_path: Path) -> str:
    with connect_db(db_path) as con:
        row = con.execute("SELECT MAX(snapshot_date) AS snapshot_date FROM snapshot_runs").fetchone()
    snapshot_date = clean_text(row["snapshot_date"] if row else "")
    if not snapshot_date:
        raise RuntimeError("No snapshot date found in SQLite history store.")
    return snapshot_date


def to_float(value) -> float:
    text = clean_text(value)
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def format_number(value, decimals: int = 6) -> str:
    if value in ("", None):
        return ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return clean_text(value)
    if numeric != numeric:
        return ""
    return f"{numeric:.{decimals}f}".rstrip("0").rstrip(".")


def format_pct(value) -> str:
    if value in ("", None):
        return ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return clean_text(value)
    if numeric != numeric:
        return ""
    return f"{numeric * 100:.2f}%"


def load_unresolved_rows(db_path: Path, snapshot_date: str) -> list[dict]:
    sql = """
    WITH unresolved AS (
      SELECT
          tr.snapshot_date,
          tr.token,
          COALESCE(tr.evidence_type, '') AS current_blocker,
          COALESCE(
              (SELECT COUNT(*)
               FROM leaderboard_daily ld
               WHERE ld.snapshot_date = tr.snapshot_date
                 AND ld.token = tr.token),
              0
          ) AS overview_visibility_count
      FROM token_rwa_labels_daily tr
      WHERE tr.snapshot_date = ?
        AND tr.rwa_label = 'review_pending'
        AND COALESCE(tr.coingecko_id, '') = ''
    ),
    listing_cov AS (
      SELECT
          snapshot_date,
          token,
          COUNT(DISTINCT venue) AS listing_venue_count,
          GROUP_CONCAT(DISTINCT venue) AS venues
      FROM token_venue_history
      WHERE snapshot_date = ?
      GROUP BY snapshot_date, token
    )
    SELECT
        unresolved.token,
        unresolved.current_blocker,
        unresolved.overview_visibility_count,
        COALESCE(listing_cov.listing_venue_count, 0) AS listing_venue_count,
        COALESCE(listing_cov.venues, '') AS venues
    FROM unresolved
    LEFT JOIN listing_cov
      ON unresolved.snapshot_date = listing_cov.snapshot_date
     AND unresolved.token = listing_cov.token
    """
    with connect_db(db_path) as con:
        rows = [dict(row) for row in con.execute(sql, [snapshot_date, snapshot_date]).fetchall()]
    return rows


def load_latest_venue_ticker_rows(db_path: Path, snapshot_date: str, tokens: list[str]) -> list[dict]:
    if not tokens:
        return []
    placeholders = ",".join("?" for _ in tokens)
    sql = f"""
    SELECT
        base_token AS token,
        venue,
        quote_asset,
        last_price,
        turnover_24h_usd
    FROM venue_ticker_metrics_daily
    WHERE snapshot_date = ?
      AND base_token IN ({placeholders})
    """
    with connect_db(db_path) as con:
        rows = [dict(row) for row in con.execute(sql, [snapshot_date, *tokens]).fetchall()]
    return rows


def aggregate_market_context(unresolved_rows: list[dict], venue_ticker_rows: list[dict]) -> list[dict]:
    ticker_by_token: dict[str, list[dict]] = defaultdict(list)
    for row in venue_ticker_rows:
        token = clean_text(row.get("token")).upper()
        if token:
            ticker_by_token[token].append(row)

    enriched = []
    for row in unresolved_rows:
        token = clean_text(row.get("token")).upper()
        tickers = ticker_by_token.get(token, [])
        turnover = sum(to_float(item.get("turnover_24h_usd")) for item in tickers)
        usd_prices = [
            to_float(item.get("last_price"))
            for item in tickers
            if clean_text(item.get("quote_asset")).upper() in USD_QUOTES and to_float(item.get("last_price")) > 0
        ]
        all_prices = [to_float(item.get("last_price")) for item in tickers if to_float(item.get("last_price")) > 0]
        representative_price = median(usd_prices) if usd_prices else (median(all_prices) if all_prices else 0.0)
        enriched.append(
            {
                **row,
                "venue_turnover_24h_usd": turnover,
                "representative_price_usd": representative_price,
            }
        )

    enriched.sort(
        key=lambda row: (
            -to_float(row.get("venue_turnover_24h_usd")),
            -to_float(row.get("listing_venue_count")),
            -to_float(row.get("overview_visibility_count")),
            clean_text(row.get("token")).upper(),
        )
    )
    return enriched


def parse_multiplier(token: str) -> tuple[float, str]:
    normalized = normalize_token(token)
    match = re.match(r"^([0-9]+)([KMB]?)([A-Z].*)$", normalized)
    if not match:
        return 1.0, normalized

    magnitude = int(match.group(1))
    suffix = match.group(2)
    factor = magnitude * {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return float(factor), match.group(3)


def is_wrapper_candidate(coin: dict) -> bool:
    text = f"{clean_text(coin.get('id'))} {clean_text(coin.get('name'))}".lower()
    return any(keyword in text for keyword in WRAPPER_KEYWORDS)


def is_stock_product_candidate(coin: dict) -> bool:
    text = f"{clean_text(coin.get('id'))} {clean_text(coin.get('name'))} {clean_text(coin.get('symbol'))}".lower()
    return any(keyword in text for keyword in STOCK_PRODUCT_KEYWORDS)


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict):
    ensure_directory_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def parse_iso_datetime(value: str) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def search_cache_fresh(entry: dict) -> bool:
    fetched_at = parse_iso_datetime(clean_text(entry.get("fetched_at")))
    if not fetched_at:
        return False
    return fetched_at >= datetime.now(timezone.utc) - timedelta(days=SEARCH_CACHE_TTL_DAYS)


def search_coingecko(query: str, search_cache: dict, search_budget: dict) -> list[dict]:
    cached_entry = search_cache.setdefault("queries", {}).get(query)
    if cached_entry and search_cache_fresh(cached_entry):
        return cached_entry.get("coins", [])

    if search_budget["used"] >= search_budget["max"]:
        return cached_entry.get("coins", []) if cached_entry else []

    response = requests.get(COINGECKO_SEARCH_URL, headers=coingecko_headers(), params={"query": query}, timeout=30)
    if response.status_code == 429:
        log(f"CoinGecko search rate limit hit for query `{query}`; using cache only for the rest of this run.")
        search_budget["used"] = search_budget["max"]
        return cached_entry.get("coins", []) if cached_entry else []

    response.raise_for_status()
    payload = response.json()
    rows = []
    for coin in payload.get("coins", []) or []:
        rows.append(
            {
                "id": clean_text(coin.get("id")),
                "symbol": clean_text(coin.get("symbol")),
                "name": clean_text(coin.get("name")),
                "market_cap_rank": coin.get("market_cap_rank"),
            }
        )

    search_cache.setdefault("queries", {})[query] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "coins": rows,
    }
    search_budget["used"] += 1
    time.sleep(SEARCH_SLEEP_SECONDS)
    return rows


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def fetch_market_data_safe(ids: list[str]) -> dict[str, dict]:
    market_lookup: dict[str, dict] = {}
    unique_ids = sorted({clean_text(item) for item in ids if clean_text(item)})
    id_chunks = chunked(unique_ids, MARKET_CHUNK_SIZE)
    for index, id_chunk in enumerate(id_chunks):
        response = requests.get(
            COINGECKO_SIMPLE_PRICE_URL,
            headers=coingecko_headers(),
            params={
                "vs_currencies": "usd",
                "ids": ",".join(id_chunk),
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            },
            timeout=30,
        )
        if response.status_code == 429:
            log("CoinGecko simple/price rate limit hit during mapping review; continuing with search-rank evidence and any partial market cache already collected.")
            break
        response.raise_for_status()
        payload = response.json()
        for coin_id, market in payload.items():
            if not isinstance(market, dict):
                continue
            market_lookup[coin_id] = {
                "current_price": market.get("usd"),
                "market_cap": market.get("usd_market_cap"),
                "last_updated_at": market.get("last_updated_at"),
            }
        if index < len(id_chunks) - 1:
            time.sleep(1.0)
    return market_lookup


def choose_search_tokens(rows: list[dict], symbol_map: dict[str, list[dict]]) -> list[str]:
    queue = []
    for index, row in enumerate(rows):
        token = clean_text(row.get("token")).upper()
        factor, base_symbol = parse_multiplier(token)
        candidates = []
        for candidate_symbol in candidate_symbols(token):
            candidates.extend(symbol_map.get(candidate_symbol, []))

        token_is_short = len(base_symbol) <= 5 and base_symbol.isalpha()
        high_impact = index < 40
        needs_search = high_impact or not candidates or token in HARD_AMBIGUOUS_TOKENS or (factor == 1 and token_is_short and clean_text(row.get("venues")) in {"binance,bitget,okx", "binance,bitget,bybit", "binance,bitget,bybit,okx"})
        if needs_search:
            queue.append(token)
    return queue


def build_candidate_rows(
    row: dict,
    symbol_map: dict[str, list[dict]],
    search_results_by_token: dict[str, list[dict]],
) -> list[dict]:
    token = clean_text(row.get("token")).upper()
    factor, _ = parse_multiplier(token)
    candidates = []
    seen = set()
    search_rank_by_id = {
        clean_text(item.get("id")): item.get("market_cap_rank")
        for item in search_results_by_token.get(token, [])
        if clean_text(item.get("id"))
    }

    for index, candidate_symbol in enumerate(candidate_symbols(token)):
        source = "exact_symbol" if index == 0 else "stripped_multiplier"
        for coin in symbol_map.get(candidate_symbol, []):
            coin_id = clean_text(coin.get("id"))
            if not coin_id or coin_id in seen:
                continue
            seen.add(coin_id)
            candidates.append(
                {
                    "candidate_symbol": candidate_symbol,
                    "candidate_source": source,
                    "coin": coin,
                    "multiplier_factor": factor if source == "stripped_multiplier" and factor > 1 else 1.0,
                    "search_rank": search_rank_by_id.get(coin_id),
                }
            )

    for coin in search_results_by_token.get(token, []):
        coin_id = clean_text(coin.get("id"))
        if not coin_id or coin_id in seen:
            continue
        seen.add(coin_id)
        candidates.append(
            {
                "candidate_symbol": clean_text(coin.get("symbol")).upper(),
                "candidate_source": "coingecko_search",
                "coin": coin,
                "multiplier_factor": 1.0,
                "search_rank": coin.get("market_cap_rank"),
            }
        )

    base_query = parse_multiplier(token)[1].lower()

    def coarse_priority(candidate: dict) -> tuple:
        coin = candidate["coin"]
        coin_id = clean_text(coin.get("id")).lower()
        coin_name = clean_text(coin.get("name")).lower()
        coin_symbol = clean_text(coin.get("symbol")).lower()
        wrapper_penalty = 1 if is_wrapper_candidate(coin) else 0
        stock_bonus = 0 if is_stock_product_candidate(coin) else 1
        contains_query = 0 if (base_query and (base_query in coin_id or base_query in coin_name or base_query == coin_symbol)) else 1
        source_penalty = {"exact_symbol": 0, "stripped_multiplier": 1, "coingecko_search": 2}.get(candidate.get("candidate_source"), 3)
        search_rank = candidate.get("search_rank")
        search_rank_penalty = int(search_rank) if clean_text(search_rank).isdigit() else 999999
        return (
            wrapper_penalty,
            source_penalty,
            stock_bonus,
            contains_query,
            search_rank_penalty,
            len(coin_id),
            coin_id,
        )

    candidates.sort(key=coarse_priority)
    symbol_candidates = [item for item in candidates if item.get("candidate_source") != "coingecko_search"][:8]
    search_candidates = [item for item in candidates if item.get("candidate_source") == "coingecko_search"][:6]
    return symbol_candidates + search_candidates


def candidate_market_cap(candidate: dict, market_lookup: dict[str, dict]) -> float:
    market = market_lookup.get(clean_text(candidate["coin"].get("id")), {})
    return to_float(market.get("market_cap"))


def candidate_price(candidate: dict, market_lookup: dict[str, dict]) -> float:
    market = market_lookup.get(clean_text(candidate["coin"].get("id")), {})
    return to_float(market.get("current_price"))


def candidate_adjusted_price(candidate: dict, market_lookup: dict[str, dict]) -> float:
    price = candidate_price(candidate, market_lookup)
    return price * candidate.get("multiplier_factor", 1.0)


def price_distance_pct(reference_price: float, candidate_price_value: float) -> float | None:
    if reference_price <= 0 or candidate_price_value <= 0:
        return None
    return abs(candidate_price_value - reference_price) / reference_price


def build_broad_universe_set() -> set[str]:
    raw = load_json(COINGECKO_RWA_UNIVERSE_CACHE_FILE, {"coin_ids": []})
    return {clean_text(item).lower() for item in raw.get("coin_ids", []) if clean_text(item)}


def looks_high_confidence_symbol_match(row: dict, candidates: list[dict], market_lookup: dict[str, dict]) -> tuple[dict | None, float, str]:
    token = clean_text(row.get("token")).upper()
    if token in HARD_AMBIGUOUS_TOKENS:
        return None, 0.0, "Symbol is too short or too generic for a safe override without stronger identity evidence."

    reference_price = to_float(row.get("representative_price_usd"))
    clean_candidates = []
    for candidate in candidates:
        if candidate.get("candidate_source") == "coingecko_search":
            continue
        market_cap = candidate_market_cap(candidate, market_lookup)
        adjusted_price = candidate_adjusted_price(candidate, market_lookup)
        distance = price_distance_pct(reference_price, adjusted_price)
        candidate["market_cap_usd"] = market_cap
        candidate["adjusted_price"] = adjusted_price
        candidate["price_distance_pct"] = distance
        candidate["is_wrapper"] = is_wrapper_candidate(candidate["coin"])
        if candidate["is_wrapper"]:
            continue
        threshold = 0.22 if candidate.get("multiplier_factor", 1.0) > 1 else 0.15
        if reference_price < 0.01:
            threshold = max(threshold, 0.25)
        if distance is not None and distance <= threshold:
            clean_candidates.append(candidate)
        elif clean_text(candidate.get("search_rank")):
            clean_candidates.append(candidate)

    if not clean_candidates:
        return None, 0.0, "No non-wrapper exact-symbol candidate had an acceptably close price match."

    def sort_key(item: dict) -> tuple:
        rank = item.get("search_rank")
        rank_penalty = int(rank) if clean_text(rank).isdigit() else 999999
        distance = item.get("price_distance_pct")
        distance_penalty = to_float(distance) if distance is not None else 99.0
        return (-to_float(item.get("market_cap_usd")), rank_penalty, distance_penalty)

    clean_candidates.sort(key=sort_key)
    top = clean_candidates[0]
    next_market_cap = to_float(clean_candidates[1].get("market_cap_usd")) if len(clean_candidates) > 1 else 0.0
    top_market_cap = to_float(top.get("market_cap_usd"))
    distance_value = top.get("price_distance_pct")
    distance = to_float(distance_value) if distance_value is not None else 99.0
    source = top.get("candidate_source")
    top_rank = int(top.get("search_rank")) if clean_text(top.get("search_rank")).isdigit() else None
    next_rank = int(clean_candidates[1].get("search_rank")) if len(clean_candidates) > 1 and clean_text(clean_candidates[1].get("search_rank")).isdigit() else None

    dominance_ok = (
        len(clean_candidates) == 1
        or top_market_cap >= max(next_market_cap * 10, 100_000_000)
        or (top_market_cap >= 1_000_000_000 and next_market_cap <= 50_000_000)
        or (top_rank is not None and top_rank <= 100 and (next_rank is None or next_rank >= top_rank * 5))
    )
    if not dominance_ok:
        return None, 0.0, "Multiple non-wrapper candidates remain plausible after price matching."

    if distance_value is None and top_rank is not None and top_rank <= 100:
        confidence = 0.92 if source == "exact_symbol" else 0.90
        return top, confidence, ""

    confidence = 0.97 if source == "exact_symbol" and distance <= 0.05 else 0.95
    if source == "stripped_multiplier":
        confidence = 0.95 if distance <= 0.08 else 0.92
    return top, confidence, ""


def looks_high_confidence_search_match(row: dict, candidates: list[dict], market_lookup: dict[str, dict]) -> tuple[dict | None, float, str]:
    token = clean_text(row.get("token")).upper()
    reference_price = to_float(row.get("representative_price_usd"))

    if token in HARD_AMBIGUOUS_TOKENS:
        return None, 0.0, "Search results are still too ambiguous for this generic symbol."

    stock_like_candidates = []
    plausible = []
    for candidate in candidates:
        if candidate.get("candidate_source") != "coingecko_search":
            continue
        if not is_stock_product_candidate(candidate["coin"]):
            continue
        stock_like_candidates.append(candidate)
        market_cap = candidate_market_cap(candidate, market_lookup)
        adjusted_price = candidate_adjusted_price(candidate, market_lookup)
        distance = price_distance_pct(reference_price, adjusted_price)
        candidate["market_cap_usd"] = market_cap
        candidate["adjusted_price"] = adjusted_price
        candidate["price_distance_pct"] = distance
        if distance is not None and distance <= 0.05:
            plausible.append(candidate)

    if not stock_like_candidates:
        return None, 0.0, ""
    if len(plausible) != 1:
        return None, 0.0, "Search returned multiple plausible tokenized-stock style candidates, so no single override is safe yet."

    return plausible[0], 0.93, ""


def market_cap_rank_text(candidate: dict) -> str:
    rank = candidate.get("search_rank")
    return clean_text(rank)


def candidate_reason(row: dict, candidate: dict, market_lookup: dict[str, dict], candidate_count: int) -> str:
    token = clean_text(row.get("token")).upper()
    venues = clean_text(row.get("venues"))
    market_cap = to_float(candidate.get("market_cap_usd"))
    adjusted_price = to_float(candidate.get("adjusted_price"))
    distance = to_float(candidate.get("price_distance_pct"))
    source = clean_text(candidate.get("candidate_source"))
    coin = candidate["coin"]
    search_rank = clean_text(candidate.get("search_rank"))

    pieces = [
        f"`{token}` appears across {clean_text(row.get('listing_venue_count')) or '0'} venue(s) [{venues}]",
        f"and the representative perp price is about {format_number(row.get('representative_price_usd'))} USD.",
        f"Candidate `{clean_text(coin.get('id'))}` / `{clean_text(coin.get('name'))}` comes from {source.replace('_', ' ')}",
    ]
    if adjusted_price > 0 and clean_text(format_pct(distance)):
        pieces.append(f"with adjusted CoinGecko price {format_number(adjusted_price)} USD ({format_pct(distance)} away)")
    elif search_rank:
        pieces.append(f"and it is also supported by CoinGecko search rank {search_rank}")
    if market_cap > 0:
        pieces.append(f"with market cap about {format_number(market_cap, 2)} USD.")
    if source in {"exact_symbol", "stripped_multiplier"}:
        pieces.append(f"The same-symbol candidate set had {candidate_count} result(s), and the selected asset clearly dominates the non-wrapper alternatives.")
    else:
        pieces.append("CoinGecko search produced a single price-compatible tokenized-stock style candidate.")
    return " ".join(pieces)


def mapping_impact_hint(candidate_id: str, broad_universe: set[str]) -> str:
    if clean_text(candidate_id).lower() in broad_universe:
        return "Mapped token is inside the broad RWA universe, so it would still need downstream category/detail review."
    return "Mapped token is outside the broad RWA universe, so current production rules would likely stop blocking it at review_pending."


def review_note_for_unresolved(token: str, candidate_rows: list[dict], used_search: bool) -> str:
    if token in HARD_AMBIGUOUS_TOKENS:
        return "Generic symbol is too ambiguous for a safe override."
    if not candidate_rows and not used_search:
        return "No exact or stripped-symbol CoinGecko candidates were found."
    if not candidate_rows and used_search:
        return "No strong search-based candidate emerged."
    if any(item.get("candidate_source") == "coingecko_search" for item in candidate_rows):
        return "Search surfaced multiple plausible candidates; keeping unresolved for precision."
    return "Candidate set remained ambiguous after price and wrapper filtering."


def write_csv(path: Path, rows: list[dict]):
    ensure_directory_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    args = parse_args()
    snapshot_date = latest_snapshot_date(args.db)
    unresolved_rows = load_unresolved_rows(args.db, snapshot_date)
    if not unresolved_rows:
        log(f"No review_pending / missing_coingecko_id rows found for {snapshot_date}.")
        write_csv(args.output, [])
        return

    venue_ticker_rows = load_latest_venue_ticker_rows(args.db, snapshot_date, [clean_text(row.get("token")).upper() for row in unresolved_rows])
    ranked_rows = aggregate_market_context(unresolved_rows, venue_ticker_rows)
    ranked_rows = ranked_rows[: args.limit]

    coin_list = fetch_coin_list()
    symbol_map = build_symbol_map(coin_list)

    search_cache = load_json(COINGECKO_SEARCH_CACHE_FILE, {"queries": {}})
    search_budget = {"used": 0, "max": args.search_max}
    search_queue = choose_search_tokens(ranked_rows[:80], symbol_map)
    search_results_by_token: dict[str, list[dict]] = {}
    for token in search_queue:
        search_results_by_token[token] = search_coingecko(token, search_cache, search_budget)
    save_json(COINGECKO_SEARCH_CACHE_FILE, search_cache)

    all_candidate_ids = set()
    candidate_rows_by_token: dict[str, list[dict]] = {}
    for row in ranked_rows:
        token = clean_text(row.get("token")).upper()
        candidate_rows = build_candidate_rows(row, symbol_map, search_results_by_token)
        candidate_rows_by_token[token] = candidate_rows
        for candidate in candidate_rows:
            coin_id = clean_text(candidate["coin"].get("id"))
            if coin_id:
                all_candidate_ids.add(coin_id)

    market_lookup = fetch_market_data_safe(sorted(all_candidate_ids))
    broad_universe = build_broad_universe_set()

    output_rows = []
    strong_candidate_count = 0
    likely_non_rwa_after_mapping = 0

    for row in ranked_rows:
        token = clean_text(row.get("token")).upper()
        candidate_rows = candidate_rows_by_token.get(token, [])
        selected_candidate = None
        confidence = 0.0
        unresolved_reason = ""

        selected_candidate, confidence, unresolved_reason = looks_high_confidence_symbol_match(row, candidate_rows, market_lookup)
        if not selected_candidate:
            search_candidate, search_confidence, search_reason = looks_high_confidence_search_match(row, candidate_rows, market_lookup)
            if search_candidate:
                selected_candidate = search_candidate
                confidence = search_confidence
            elif search_reason:
                unresolved_reason = search_reason

        if selected_candidate and confidence >= 0.90:
            strong_candidate_count += 1
            candidate_id = clean_text(selected_candidate["coin"].get("id"))
            candidate_name = clean_text(selected_candidate["coin"].get("name"))
            market = market_lookup.get(candidate_id, {})
            broad_member = "yes" if candidate_id.lower() in broad_universe else "no"
            if broad_member == "no":
                likely_non_rwa_after_mapping += 1
            output_rows.append(
                {
                    "token": token,
                    "current_blocker": clean_text(row.get("current_blocker")) or "missing_coingecko_id",
                    "venue_turnover_24h_usd": format_number(row.get("venue_turnover_24h_usd"), 2),
                    "representative_price_usd": format_number(row.get("representative_price_usd")),
                    "listing_venue_count": clean_text(row.get("listing_venue_count")),
                    "venues": clean_text(row.get("venues")),
                    "overview_visibility_count": clean_text(row.get("overview_visibility_count")),
                    "candidate_symbol": clean_text(selected_candidate.get("candidate_symbol")),
                    "candidate_source": clean_text(selected_candidate.get("candidate_source")),
                    "candidate_count": str(len(candidate_rows)),
                    "candidate_coingecko_id": candidate_id,
                    "candidate_name": candidate_name,
                    "candidate_market_cap_usd": format_number(selected_candidate.get("market_cap_usd"), 2),
                    "candidate_market_cap_rank": market_cap_rank_text(selected_candidate),
                    "candidate_current_price_usd": format_number(market.get("current_price")),
                    "price_distance_pct": format_pct(selected_candidate.get("price_distance_pct")),
                    "confidence": format_number(confidence, 2),
                    "cg_broad_rwa_member_if_mapped": broad_member,
                    "mapping_impact_hint": mapping_impact_hint(candidate_id, broad_universe),
                    "why_this_candidate_is_likely_correct": candidate_reason(row, selected_candidate, market_lookup, len(candidate_rows)),
                    "should_add_to_override": "yes",
                    "resolution_status": "strong_candidate",
                    "review_note": "",
                }
            )
            continue

        output_rows.append(
            {
                "token": token,
                "current_blocker": clean_text(row.get("current_blocker")) or "missing_coingecko_id",
                "venue_turnover_24h_usd": format_number(row.get("venue_turnover_24h_usd"), 2),
                "representative_price_usd": format_number(row.get("representative_price_usd")),
                "listing_venue_count": clean_text(row.get("listing_venue_count")),
                "venues": clean_text(row.get("venues")),
                "overview_visibility_count": clean_text(row.get("overview_visibility_count")),
                "candidate_symbol": "",
                "candidate_source": "",
                "candidate_count": str(len(candidate_rows)),
                "candidate_coingecko_id": "",
                "candidate_name": "",
                "candidate_market_cap_usd": "",
                "candidate_market_cap_rank": "",
                "candidate_current_price_usd": "",
                "price_distance_pct": "",
                "confidence": "",
                "cg_broad_rwa_member_if_mapped": "",
                "mapping_impact_hint": "",
                "why_this_candidate_is_likely_correct": "",
                "should_add_to_override": "no",
                "resolution_status": "unresolved",
                "review_note": unresolved_reason or review_note_for_unresolved(token, candidate_rows, token in search_results_by_token),
            }
        )

    output_rows.sort(
        key=lambda row: (
            -to_float(row.get("venue_turnover_24h_usd")),
            -to_float(row.get("candidate_market_cap_usd")),
            -to_float(row.get("overview_visibility_count")),
            clean_text(row.get("token")).upper(),
        )
    )
    write_csv(args.output, output_rows)
    log(f"Wrote {len(output_rows)} mapping review rows to {args.output}")
    log(f"Strong mapping candidates: {strong_candidate_count}")
    log(f"Of those, {likely_non_rwa_after_mapping} sit outside the broad CoinGecko RWA universe and would likely drop out of review_pending quickly.")


if __name__ == "__main__":
    main()
