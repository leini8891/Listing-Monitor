from __future__ import annotations

"""
One-off shadow adjudication workflow for current review_pending tokens.

This script is intentionally offline-review oriented:
- It does not write back into production rules or allowlists.
- It does not mutate SQLite labels.
- It only produces a shadow review worksheet for the latest snapshot.
"""

import argparse
import csv
import json
import os
import re
import sqlite3
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

from src.common.paths import (  # noqa: E402
    COINGECKO_DETAIL_CACHE_FILE,
    COINGECKO_SHADOW_DETAIL_CACHE_FILE,
    COINGECKO_RWA_UNIVERSE_CACHE_FILE,
    ENV_FILE,
    HISTORY_DB_FILE,
    RWA_XYZ_PUBLIC_DIRECTORY_CACHE_FILE,
    TOKEN_RWA_SHADOW_REVIEW_FILE,
    ensure_directory_layout,
)
from src.transform.label_rwa_tokens import (  # noqa: E402
    CORE_CATEGORY_RULES,
    CORE_STRONG_PATTERNS,
    RELATED_CATEGORY_RULES,
    RELATED_STRONG_PATTERNS,
    category_rule_matches,
    clean_text,
    build_cache_entry,
    coingecko_get_coin_detail,
    coingecko_headers,
    detail_text,
    keyword_matches,
    load_cache,
    normalize_coin_id,
    save_cache,
)


load_dotenv(ENV_FILE)


RWA_XYZ_DIRECTORY_URL = "https://app.rwa.xyz/directory"
RWA_XYZ_CACHE_TTL_HOURS = 12
SHADOW_CG_DETAIL_FETCH_LIMIT = 8
SHADOW_CG_FETCH_SLEEP_SECONDS = 8.0
MESSARI_ASSET_DETAILS_URL = "https://api.messari.io/metrics/v2/assets/details"
MESSARI_PROJECT_URL = "https://messari.io/project/{slug}"

SHADOW_REVIEW_COLUMNS = [
    "token",
    "coingecko_id",
    "current_rwa_label",
    "current_label_source",
    "current_evidence_type",
    "shadow_rwa_label",
    "shadow_rwa_category",
    "shadow_confidence",
    "shadow_reason_summary",
    "evidence_sources_used",
    "cg_broad_rwa_member",
    "cg_category_hits",
    "rwa_xyz_match",
    "messari_category",
    "messari_description_snippet",
    "decision_basis",
    "recommended_action",
    "current_price_usd",
    "price_change_24h_pct",
    "volume_24h_usd",
    "market_cap_usd",
    "earliest_listing_time_sgt",
    "match_status",
    "overview_visibility_count",
]

RWA_XYZ_ROW_PATTERN = re.compile(
    r'\{"rank":\d+,"id":\d+,"name":"(?P<name>[^"]+)","ticker":"(?P<ticker>[^"]+)","url":"(?P<url>/assets/[^"]+)",'
    r'.*?"protocol":\{"name":"(?P<protocol>[^"]*)".*?\},"asset_class":\{"id":\d+,"url":"[^"]+","name":"(?P<asset_class>[^"]+)"',
    re.DOTALL,
)

NON_RWA_CORE_EXCLUSION_TOKENS = {
    "USDT",
    "USDC",
    "DAI",
    "FDUSD",
    "RLUSD",
    "PYUSD",
    "USD1",
    "USDE",
    "FRAX",
}

NON_RWA_CORE_EXCLUSION_IDS = {
    "tether",
    "usd-coin",
    "dai",
    "first-digital-usd",
    "ripple-usd",
    "paypal-usd",
    "usd1",
    "ethena-usde",
    "frax",
}

CORE_RWA_ASSET_CLASSES = {
    "commodities": "tokenized-commodity",
    "u.s. treasuries": "tokenized-treasury",
    "tokenized treasury bills (t-bills)": "tokenized-treasury",
    "tokenized treasury bonds (t-bonds)": "tokenized-treasury",
    "non-u.s. govt. debt": "tokenized-government-debt",
    "real estate": "tokenized-real-estate",
    "corporate bonds": "tokenized-credit",
    "asset-backed credit": "tokenized-credit",
    "private credit": "tokenized-credit",
    "stocks": "tokenized-equity",
    "public equity": "tokenized-equity",
    "exchange-traded funds (etfs)": "tokenized-etf",
}

RELATED_RWA_ASSET_CLASSES = {
    "stablecoins": "asset-mapped-stablecoin",
}

RWA_STABLECOIN_RELATED_PATTERNS = [
    re.compile(r"\byield[- ]bearing\b", re.IGNORECASE),
    re.compile(r"\btreasury[- ]backed\b", re.IGNORECASE),
    re.compile(r"\bbacked by (?:u\.?s\.? )?treasur", re.IGNORECASE),
    re.compile(r"\bshort[- ]term (?:u\.?s\.? )?treasur", re.IGNORECASE),
]

GENERIC_NAME_STOPWORDS = {
    "token",
    "protocol",
    "network",
    "dao",
    "finance",
    "fund",
    "digital",
    "crypto",
    "coin",
    "the",
    "and",
    "labs",
    "lab",
    "rights",
    "sale",
}


def log(message: str):
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[RWA Shadow Review] [{timestamp}] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a one-off shadow RWA review worksheet for current review_pending tokens.")
    parser.add_argument("--db", type=Path, default=HISTORY_DB_FILE, help=f"SQLite history DB (default: {HISTORY_DB_FILE})")
    parser.add_argument("--output", type=Path, default=TOKEN_RWA_SHADOW_REVIEW_FILE, help=f"Output CSV path (default: {TOKEN_RWA_SHADOW_REVIEW_FILE})")
    parser.add_argument("--snapshot-date", default="", help="Optional snapshot date override. Defaults to latest snapshot in SQLite.")
    parser.add_argument("--rwa-xyz-cache", type=Path, default=RWA_XYZ_PUBLIC_DIRECTORY_CACHE_FILE, help=f"RWA.xyz public directory cache path (default: {RWA_XYZ_PUBLIC_DIRECTORY_CACHE_FILE})")
    parser.add_argument("--rwa-xyz-cache-ttl-hours", type=int, default=RWA_XYZ_CACHE_TTL_HOURS, help=f"RWA.xyz cache TTL in hours (default: {RWA_XYZ_CACHE_TTL_HOURS})")
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
        raise RuntimeError("No snapshot_date found in SQLite history store.")
    return snapshot_date


def load_pending_rows(db_path: Path, snapshot_date: str) -> list[dict]:
    sql = """
    SELECT
        tr.snapshot_date,
        tr.token,
        COALESCE(tr.coingecko_id, '') AS coingecko_id,
        COALESCE(tr.rwa_label, '') AS current_rwa_label,
        COALESCE(tr.label_source, '') AS current_label_source,
        COALESCE(tr.evidence_type, '') AS current_evidence_type,
        COALESCE(tr.evidence_detail_json, '') AS current_evidence_detail_json,
        COALESCE(tm.current_price_usd, '') AS current_price_usd,
        COALESCE(tm.price_change_24h_pct, '') AS price_change_24h_pct,
        COALESCE(tm.volume_24h_usd, '') AS volume_24h_usd,
        COALESCE(tm.market_cap_usd, '') AS market_cap_usd,
        COALESCE(tm.match_status, '') AS match_status,
        COALESCE(metrics.earliest_listing_time_sgt, '') AS earliest_listing_time_sgt,
        (
            SELECT COUNT(*)
            FROM leaderboard_daily ld
            WHERE ld.snapshot_date = tr.snapshot_date
              AND ld.token = tr.token
        ) AS overview_visibility_count
    FROM token_rwa_labels_daily tr
    LEFT JOIN token_market_metrics_daily tm
      ON tr.snapshot_date = tm.snapshot_date
     AND tr.token = tm.token
    LEFT JOIN token_metrics_daily metrics
      ON tr.snapshot_date = metrics.snapshot_date
     AND tr.token = metrics.token
    WHERE tr.snapshot_date = ?
      AND tr.rwa_label = 'review_pending'
    """
    with connect_db(db_path) as con:
        rows = [dict(row) for row in con.execute(sql, [snapshot_date]).fetchall()]
    return rows


def parse_datetime(value: str) -> datetime | None:
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


def load_broad_universe_cache(path: Path) -> dict:
    return load_json(path, {"coin_ids": [], "fetched_at": "", "target_categories": []})


def build_broad_category_membership_map(cache_payload: dict) -> dict[str, list[str]]:
    memberships: dict[str, list[str]] = defaultdict(list)
    for category_id, coin_ids in (cache_payload.get("coins_by_category") or {}).items():
        normalized_category = clean_text(category_id).lower()
        if not normalized_category:
            continue
        for coin_id in coin_ids or []:
            normalized_coin_id = normalize_coin_id(coin_id)
            if normalized_coin_id:
                memberships[normalized_coin_id].append(normalized_category)
    return {coin_id: sorted(set(categories)) for coin_id, categories in memberships.items()}


def cache_is_fresh(fetched_at_value: str, ttl_hours: int) -> bool:
    fetched_at = parse_datetime(fetched_at_value)
    if not fetched_at:
        return False
    return fetched_at >= datetime.now(timezone.utc) - timedelta(hours=ttl_hours)


def fetch_rwa_xyz_public_directory(cache_path: Path, ttl_hours: int) -> dict:
    cached = load_json(cache_path, {"assets": [], "fetched_at": "", "source": RWA_XYZ_DIRECTORY_URL})
    if cached.get("assets") and cache_is_fresh(clean_text(cached.get("fetched_at")), ttl_hours):
        return cached

    response = requests.get(RWA_XYZ_DIRECTORY_URL, timeout=60)
    response.raise_for_status()
    html = response.text

    assets = []
    for match in RWA_XYZ_ROW_PATTERN.finditer(html):
        row = {key: clean_text(value) for key, value in match.groupdict().items()}
        row["url"] = f"https://app.rwa.xyz{row['url']}"
        assets.append(row)

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": RWA_XYZ_DIRECTORY_URL,
        "assets": assets,
    }
    save_json(cache_path, payload)
    log(f"Refreshed RWA.xyz public directory cache with {len(assets)} asset rows.")
    return payload


def lexical_tokens(text: str) -> set[str]:
    tokens = set()
    for part in re.split(r"[^a-z0-9]+", clean_text(text).lower()):
        if not part or len(part) <= 1 or part in GENERIC_NAME_STOPWORDS:
            continue
        tokens.add(part)
    return tokens


def build_rwa_xyz_index(cache_payload: dict) -> dict[str, list[dict]]:
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for asset in cache_payload.get("assets", []):
        ticker = clean_text(asset.get("ticker")).upper()
        if not ticker:
            continue
        by_ticker[ticker].append(asset)
    return by_ticker


def merge_cg_caches(primary_cache: dict, shadow_cache: dict) -> dict:
    merged = {"schema_version": 1, "coins": {}}
    merged["coins"].update(primary_cache.get("coins", {}))
    merged["coins"].update(shadow_cache.get("coins", {}))
    return merged


def cache_entry_has_detail(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    if clean_text(entry.get("name")):
        return True
    if clean_text(entry.get("description")):
        return True
    if entry.get("categories"):
        return True
    return False


def shadow_fetch_priority(row: dict, broad_categories: list[str]) -> tuple:
    targeted = any(category in {"rwa-protocol", "tokenized-products"} for category in broad_categories)
    return (
        0 if targeted else 1,
        -to_float(row.get("volume_24h_usd")),
        -to_float(row.get("market_cap_usd")),
        clean_text(row.get("token")).upper(),
    )


def refresh_shadow_detail_cache(
    pending_rows: list[dict],
    broad_category_map: dict[str, list[str]],
    merged_cache: dict,
    shadow_cache_path: Path,
    max_fetches: int,
    sleep_seconds: float,
) -> dict:
    shadow_cache = load_cache(shadow_cache_path)
    shadow_coin_rows = shadow_cache.setdefault("coins", {})

    candidates = []
    for row in pending_rows:
        coin_id = normalize_coin_id(row.get("coingecko_id"))
        if not coin_id:
            continue
        if cache_entry_has_detail(merged_cache.get("coins", {}).get(coin_id, {})):
            continue
        categories = broad_category_map.get(coin_id, [])
        candidates.append((shadow_fetch_priority(row, categories), row, categories))

    if not candidates:
        return shadow_cache

    refresh_queue = [row for _, row, _ in sorted(candidates)[:max_fetches]]
    log(f"Shadow detail refresh: attempting CoinGecko detail fetch for {len(refresh_queue)} stable-ID review_pending token(s).")

    for index, row in enumerate(refresh_queue):
        coin_id = normalize_coin_id(row.get("coingecko_id"))
        try:
            detail = coingecko_get_coin_detail(coin_id)
            shadow_coin_rows[coin_id] = build_cache_entry(
                coin_id=coin_id,
                detail=detail,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            shadow_coin_rows[coin_id]["last_error"] = ""
            log(f"Shadow detail cache refreshed for {coin_id}.")
        except Exception as exc:
            existing = shadow_coin_rows.get(coin_id, {"coingecko_id": coin_id})
            existing["last_error"] = clean_text(exc)
            existing["fetched_at"] = existing.get("fetched_at", datetime.now(timezone.utc).isoformat())
            shadow_coin_rows[coin_id] = existing
            log(f"Shadow detail fetch failed for {coin_id}; keeping evidence gap. Reason: {exc}")
            if "429" in str(exc):
                log("CoinGecko rate limit is active for the shadow workflow; stopping further shadow detail fetches for this run.")
                break

        if index < len(refresh_queue) - 1:
            time.sleep(sleep_seconds)

    save_cache(shadow_cache_path, shadow_cache)
    return shadow_cache


def choose_rwa_xyz_match(token: str, coingecko_id: str, cache_entry: dict, rwa_xyz_index: dict[str, list[dict]]) -> tuple[str, dict | None]:
    candidates = rwa_xyz_index.get(clean_text(token).upper(), [])
    if not candidates:
        return "none", None

    source_tokens = lexical_tokens(coingecko_id)
    source_tokens.update(lexical_tokens(clean_text(cache_entry.get("name"))))
    source_tokens.update(lexical_tokens(clean_text(cache_entry.get("description"))[:240]))

    best_row = None
    best_overlap: set[str] = set()
    for candidate in candidates:
        candidate_tokens = lexical_tokens(candidate.get("name", ""))
        candidate_tokens.update(lexical_tokens(candidate.get("protocol", "")))
        candidate_tokens.update(lexical_tokens(candidate.get("url", "")))
        overlap = source_tokens & candidate_tokens
        if len(overlap) > len(best_overlap):
            best_row = candidate
            best_overlap = overlap

    if best_row and best_overlap:
        return "confirmed_exact_ticker", {**best_row, "name_overlap": sorted(best_overlap)}

    return "ticker_collision_unconfirmed", candidates[0]


def compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def summarize_rwa_xyz_match(match_status: str, match_row: dict | None) -> str:
    if match_status == "none":
        return "none"
    if not match_row:
        return match_status

    summary = {
        "match_status": match_status,
        "ticker": clean_text(match_row.get("ticker")),
        "name": clean_text(match_row.get("name")),
        "protocol": clean_text(match_row.get("protocol")),
        "asset_class": clean_text(match_row.get("asset_class")),
        "url": clean_text(match_row.get("url")),
    }
    if match_row.get("name_overlap"):
        summary["name_overlap"] = match_row.get("name_overlap")
    return compact_json(summary)


def classify_cg_category_hits(cache_entry: dict) -> list[str]:
    categories = cache_entry.get("categories", [])
    hits: list[str] = []
    for _, matched in category_rule_matches(categories, CORE_CATEGORY_RULES):
        hits.append(f"core_category:{matched}")
    for _, matched in category_rule_matches(categories, RELATED_CATEGORY_RULES):
        hits.append(f"related_category:{matched}")
    text = detail_text(cache_entry)
    for _, matched in keyword_matches(text, CORE_STRONG_PATTERNS):
        hits.append(f"core_keyword:{matched}")
    for _, matched in keyword_matches(text, RELATED_STRONG_PATTERNS):
        hits.append(f"related_keyword:{matched}")
    return hits


def strong_shadow_core_from_cg(cache_entry: dict) -> tuple[str, list[str]] | None:
    categories = [clean_text(item).lower() for item in cache_entry.get("categories", []) if clean_text(item)]
    detail = detail_text(cache_entry).lower()
    checks = [
        ("tokenized-equity", ("tokenized stock", "tokenized stocks"), ("tokenized stock", "xstocks", "tokenized equities")),
        ("tokenized-real-estate", ("tokenized real estate",), ("tokenized real estate", "real estate-backed")),
        ("tokenized-gold", ("tokenized gold",), ("tokenized gold", "gold-backed token", "physical gold")),
        ("tokenized-treasury", ("tokenized treasury", "tokenized treasuries"), ("tokenized treasury", "u.s. treasuries", "treasury-backed")),
    ]
    for rwa_category, category_keywords, detail_keywords in checks:
        if any(keyword in category for category in categories for keyword in category_keywords):
            matched_detail = [keyword for keyword in detail_keywords if keyword in detail]
            if matched_detail:
                return rwa_category, matched_detail
    return None


def strong_shadow_related_from_cg(cache_entry: dict, broad_categories: list[str]) -> tuple[str, list[str]] | None:
    categories = [clean_text(item).lower() for item in cache_entry.get("categories", []) if clean_text(item)]
    detail = detail_text(cache_entry).lower()
    support_keywords = [
        "real world asset",
        "rwa",
        "tokenization",
        "tokenized",
        "institutional",
        "issuance",
        "payments infrastructure",
        "on-chain payments",
        "asset issuance",
        "real-world assets",
    ]

    if "rwa-protocol" not in broad_categories and not any("rwa protocol" in category for category in categories):
        return None

    matched_keywords = [keyword for keyword in support_keywords if keyword in detail]
    if matched_keywords:
        return "rwa-protocol", matched_keywords
    return None


def stablecoin_exclusion_match(token: str, coingecko_id: str, cache_entry: dict, rwa_xyz_row: dict | None) -> bool:
    token_upper = clean_text(token).upper()
    if token_upper in NON_RWA_CORE_EXCLUSION_TOKENS or normalize_coin_id(coingecko_id) in NON_RWA_CORE_EXCLUSION_IDS:
        return True

    if clean_text(rwa_xyz_row.get("asset_class")).lower() == "stablecoins":
        text = " ".join(
            part
            for part in (
                clean_text(cache_entry.get("name")),
                clean_text(cache_entry.get("description")),
                " ".join(clean_text(item) for item in cache_entry.get("categories", [])),
                clean_text(rwa_xyz_row.get("name")),
                clean_text(rwa_xyz_row.get("protocol")),
            )
            if part
        ).lower()
        if "yield" not in text and "treasury" not in text and "commodity" not in text:
            return True
    return False


def stablecoin_related_match(cache_entry: dict, rwa_xyz_row: dict | None) -> bool:
    if clean_text(rwa_xyz_row.get("asset_class")).lower() != "stablecoins":
        return False
    text = " ".join(
        part
        for part in (
            clean_text(cache_entry.get("name")),
            clean_text(cache_entry.get("description")),
            " ".join(clean_text(item) for item in cache_entry.get("categories", [])),
            clean_text(rwa_xyz_row.get("name")),
            clean_text(rwa_xyz_row.get("protocol")),
        )
        if part
    )
    return any(pattern.search(text) for pattern in RWA_STABLECOIN_RELATED_PATTERNS)


def asset_class_to_shadow_category(asset_class_name: str) -> str:
    normalized = clean_text(asset_class_name).lower()
    if normalized in CORE_RWA_ASSET_CLASSES:
        return CORE_RWA_ASSET_CLASSES[normalized]
    if normalized in RELATED_RWA_ASSET_CLASSES:
        return RELATED_RWA_ASSET_CLASSES[normalized]
    return clean_text(asset_class_name).lower().replace(" ", "-")


def messari_headers() -> dict:
    key = os.getenv("MESSARI_API_KEY", "").strip()
    return {"X-Messari-API-Key": key} if key else {}


def messari_status_stub() -> dict:
    if os.getenv("MESSARI_API_KEY", "").strip():
        return {"status": "configured_but_not_fetched", "category": "", "description_snippet": ""}
    return {"status": "unavailable_no_api_key", "category": "", "description_snippet": ""}


def try_fetch_messari(coin_id: str) -> dict:
    api_key = os.getenv("MESSARI_API_KEY", "").strip()
    if not api_key:
        return messari_status_stub()

    try:
        response = requests.get(
            MESSARI_ASSET_DETAILS_URL,
            params={"assetKeys": coin_id},
            headers=messari_headers(),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") or []
        if not rows:
            return {"status": "not_found", "category": "", "description_snippet": ""}
        row = rows[0]
        description = clean_text(row.get("description"))
        return {
            "status": "ok",
            "category": clean_text(row.get("category")),
            "description_snippet": description[:240],
        }
    except Exception as exc:
        return {"status": f"unavailable:{clean_text(exc)}", "category": "", "description_snippet": ""}


def format_float(value) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        numeric = float(text)
    except ValueError:
        return text
    if numeric != numeric:
        return ""
    return f"{numeric:.6f}".rstrip("0").rstrip(".")


def to_float(value) -> float:
    try:
        return float(clean_text(value) or 0)
    except ValueError:
        return 0.0


def make_shadow_row(
    row: dict,
    broad_universe: set[str],
    broad_category_map: dict[str, list[str]],
    cg_cache: dict,
    rwa_xyz_index: dict[str, list[dict]],
    messari_mode: dict,
) -> dict:
    token = clean_text(row.get("token"))
    coin_id = normalize_coin_id(row.get("coingecko_id"))
    cache_entry = cg_cache.get("coins", {}).get(coin_id, {}) if coin_id else {}
    broad_member = None if not coin_id else coin_id in broad_universe
    broad_categories = broad_category_map.get(coin_id, []) if coin_id else []
    broad_member_text = "unknown" if broad_member is None else ("yes" if broad_member else "no")
    cg_hits = classify_cg_category_hits(cache_entry) if cache_entry else []
    rwa_xyz_status, rwa_xyz_row = choose_rwa_xyz_match(token, coin_id, cache_entry, rwa_xyz_index)
    messari = messari_mode

    shadow_label = "review_pending_shadow"
    shadow_category = ""
    shadow_confidence = 0.20
    reason = "Mapping unresolved."
    decision_basis = "rule0_mapping_unresolved"
    recommended_action = "resolve_mapping_then_recheck"

    sources_used = [
        "local.current_label",
        "local.market_metrics",
        "coingecko.broad_universe",
        "coingecko.broad_universe_category_map" if broad_categories else "coingecko.broad_universe_category_map_missing",
        "coingecko.detail_cache" if cache_entry else "coingecko.detail_cache_missing",
        "rwa_xyz.public_directory",
        f"messari.{clean_text(messari.get('status')) or 'unavailable'}",
    ]

    if not coin_id:
        reason = "Mapping unresolved: no stable CoinGecko identity is available, so the token stays in shadow review."
    else:
        if stablecoin_exclusion_match(token, coin_id, cache_entry, rwa_xyz_row or {}):
            shadow_label = "non_rwa"
            shadow_category = "excluded-mainstream-stablecoin"
            shadow_confidence = 0.91 if rwa_xyz_row else 0.84
            reason = "Stablecoin-style asset is explicitly excluded from shadow RWA classification, so it is treated as non-RWA."
            decision_basis = "rule1_strong_non_rwa_stablecoin_exclusion"
            recommended_action = "candidate_non_rwa_manual_merge"
        elif broad_member is False and rwa_xyz_status == "none" and not cg_hits:
            shadow_label = "non_rwa"
            shadow_category = ""
            shadow_confidence = 0.84 if messari.get("status") != "ok" else 0.90
            reason = "Stable CoinGecko identity exists, but the asset is outside the broad CoinGecko RWA universe and has no supporting RWA.xyz or CoinGecko RWA evidence."
            decision_basis = "rule1_strong_non_rwa_negative_universe"
            recommended_action = "candidate_non_rwa_manual_merge"
        elif cache_entry and (strong_core := strong_shadow_core_from_cg(cache_entry)):
            shadow_label = "core"
            shadow_category = strong_core[0]
            shadow_confidence = 0.84
            reason = (
                "CoinGecko category/detail evidence points to a directly tokenized real-world asset, "
                "so the token is shadow-classified as core."
            )
            decision_basis = "rule2_strong_core_coingecko_detail"
            recommended_action = "candidate_core_manual_merge"
        elif cache_entry and (strong_related := strong_shadow_related_from_cg(cache_entry, broad_categories)):
            shadow_label = "related"
            shadow_category = strong_related[0]
            shadow_confidence = 0.82
            reason = (
                "CoinGecko broad-universe membership plus detail text indicate RWA protocol / tokenization infrastructure evidence, "
                "so the token is shadow-classified as related."
            )
            decision_basis = "rule3_strong_related_coingecko_detail"
            recommended_action = "candidate_related_manual_merge"
        elif rwa_xyz_status == "confirmed_exact_ticker" and rwa_xyz_row:
            asset_class_name = clean_text(rwa_xyz_row.get("asset_class"))
            normalized_class = asset_class_name.lower()
            if normalized_class in CORE_RWA_ASSET_CLASSES:
                shadow_label = "core"
                shadow_category = asset_class_to_shadow_category(asset_class_name)
                shadow_confidence = 0.92 if broad_member else 0.86
                reason = (
                    f"Exact RWA.xyz asset match is confirmed and aligns with the CoinGecko identity; "
                    f"asset class `{asset_class_name}` indicates a directly tokenized real-world asset."
                )
                decision_basis = "rule2_strong_core_rwa_xyz_exact_asset"
                recommended_action = "candidate_core_manual_merge"
            elif normalized_class in RELATED_RWA_ASSET_CLASSES and stablecoin_related_match(cache_entry, rwa_xyz_row):
                shadow_label = "related"
                shadow_category = "yield-bearing-stablecoin"
                shadow_confidence = 0.84
                reason = (
                    f"Exact RWA.xyz asset match is confirmed and points to a mapped stablecoin product with supporting treasury/yield language, "
                    f"so it is treated as RWA-related."
                )
                decision_basis = "rule3_strong_related_mapped_stablecoin"
                recommended_action = "candidate_related_manual_merge"
            else:
                reason = "RWA.xyz exact ticker match exists, but the asset type does not safely refine to core/related under the current high-precision shadow rules."
                decision_basis = "rule4_keep_pending_unrefined_rwa_xyz_match"
                recommended_action = "manual_review_rwa_xyz_match"
                shadow_confidence = 0.55
        elif rwa_xyz_status == "ticker_collision_unconfirmed":
            reason = "A same-ticker asset exists on RWA.xyz, but the asset name/protocol does not align with this token’s CoinGecko identity, so the match is treated as a ticker collision."
            decision_basis = "rule4_keep_pending_ticker_collision"
            recommended_action = "manual_review_ticker_collision"
            shadow_confidence = 0.35
        elif broad_member:
            reason = "Token is inside the broad CoinGecko RWA candidate universe, but external detail is still insufficient to safely refine it to core, related, or non-RWA."
            decision_basis = "rule4_keep_pending_broad_rwa_candidate"
            recommended_action = "manual_review_external_detail"
            shadow_confidence = 0.45 if cache_entry else 0.30
        else:
            reason = "Evidence remains insufficient for a high-confidence shadow classification."
            decision_basis = "rule4_keep_pending_insufficient_evidence"
            recommended_action = "manual_review_external_detail"
            shadow_confidence = 0.30

    return {
        "token": token,
        "coingecko_id": coin_id,
        "current_rwa_label": clean_text(row.get("current_rwa_label")),
        "current_label_source": clean_text(row.get("current_label_source")),
        "current_evidence_type": clean_text(row.get("current_evidence_type")),
        "shadow_rwa_label": shadow_label,
        "shadow_rwa_category": shadow_category,
        "shadow_confidence": format_float(shadow_confidence),
        "shadow_reason_summary": reason,
        "evidence_sources_used": "|".join(sources_used),
        "cg_broad_rwa_member": broad_member_text,
        "cg_category_hits": "|".join(filter(None, [",".join(broad_categories), "|".join(cg_hits)])) if (broad_categories or cg_hits) else ("detail_unavailable" if not cache_entry else ""),
        "rwa_xyz_match": summarize_rwa_xyz_match(rwa_xyz_status, rwa_xyz_row),
        "messari_category": clean_text(messari.get("category")) or clean_text(messari.get("status")),
        "messari_description_snippet": clean_text(messari.get("description_snippet")),
        "decision_basis": decision_basis,
        "recommended_action": recommended_action,
        "current_price_usd": format_float(row.get("current_price_usd")),
        "price_change_24h_pct": format_float(row.get("price_change_24h_pct")),
        "volume_24h_usd": format_float(row.get("volume_24h_usd")),
        "market_cap_usd": format_float(row.get("market_cap_usd")),
        "earliest_listing_time_sgt": clean_text(row.get("earliest_listing_time_sgt")),
        "match_status": clean_text(row.get("match_status")),
        "overview_visibility_count": clean_text(row.get("overview_visibility_count")),
    }


def write_csv(path: Path, rows: list[dict]):
    ensure_directory_layout()
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHADOW_REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    args = parse_args()
    snapshot_date = clean_text(args.snapshot_date) or latest_snapshot_date(args.db)
    pending_rows = load_pending_rows(args.db, snapshot_date)
    if not pending_rows:
        log(f"No review_pending tokens found for snapshot {snapshot_date}.")
        write_csv(args.output, [])
        return

    broad_universe_cache = load_broad_universe_cache(COINGECKO_RWA_UNIVERSE_CACHE_FILE)
    broad_universe = set(normalize_coin_id(coin_id) for coin_id in broad_universe_cache.get("coin_ids", []) if normalize_coin_id(coin_id))
    broad_category_map = build_broad_category_membership_map(broad_universe_cache)
    production_cg_cache = load_cache(COINGECKO_DETAIL_CACHE_FILE)
    shadow_cg_cache = refresh_shadow_detail_cache(
        pending_rows=pending_rows,
        broad_category_map=broad_category_map,
        merged_cache=production_cg_cache,
        shadow_cache_path=COINGECKO_SHADOW_DETAIL_CACHE_FILE,
        max_fetches=SHADOW_CG_DETAIL_FETCH_LIMIT,
        sleep_seconds=SHADOW_CG_FETCH_SLEEP_SECONDS,
    )
    cg_cache = merge_cg_caches(production_cg_cache, shadow_cg_cache)
    rwa_xyz_cache = fetch_rwa_xyz_public_directory(args.rwa_xyz_cache, args.rwa_xyz_cache_ttl_hours)
    rwa_xyz_index = build_rwa_xyz_index(rwa_xyz_cache)

    messari_probe = messari_status_stub()
    if os.getenv("MESSARI_API_KEY", "").strip():
        # Only fetch on demand when a key is configured.
        messari_probe = {"status": "api_key_configured", "category": "", "description_snippet": ""}

    shadow_rows = [
        make_shadow_row(
            row=row,
            broad_universe=broad_universe,
            broad_category_map=broad_category_map,
            cg_cache=cg_cache,
            rwa_xyz_index=rwa_xyz_index,
            messari_mode=messari_probe,
        )
        for row in pending_rows
    ]

    shadow_rows.sort(
        key=lambda row: (
            -to_float(row.get("volume_24h_usd")),
            -to_float(row.get("market_cap_usd")),
            -to_float(row.get("overview_visibility_count")),
            clean_text(row.get("token")).upper(),
        )
    )

    write_csv(args.output, shadow_rows)
    log(f"Wrote {len(shadow_rows)} shadow review rows to {args.output}")


if __name__ == "__main__":
    main()
