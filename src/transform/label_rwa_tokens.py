from __future__ import annotations

"""
Label tokens for RWA relevance using a production-minded V1 pipeline.

Priority order:
1. manual_override
2. seed_allowlist
3. cached_coingecko_categories
4. conservative_keyword_fallback

Important:
- Classification is keyed primarily by CoinGecko ID, not by token symbol alone.
- CoinGecko detail fetches are only used for tokens that were not resolved by
  manual override or the seed allowlist.
- Cache lives in data/cache/ and is refreshable; processed outputs remain clean.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import warnings
from dataclasses import dataclass
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

from src.common.paths import (
    COINGECKO_DETAIL_CACHE_FILE,
    COINGECKO_RWA_UNIVERSE_CACHE_FILE,
    ENV_FILE,
    RWA_ALLOWLIST_FILE,
    TOKEN_MARKET_FILE,
    TOKEN_RWA_LABELS_FILE,
    TOKEN_RWA_REVIEW_QUEUE_FILE,
    ensure_directory_layout,
)

load_dotenv(ENV_FILE)


COINGECKO_COIN_DETAIL_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}"
COINGECKO_CATEGORIES_LIST_URL = "https://api.coingecko.com/api/v3/coins/categories/list"
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
VALID_RWA_LABELS = {"core", "related", "non_rwa", "review_pending"}
CACHE_TTL_DAYS = 14
RWA_UNIVERSE_CACHE_TTL_DAYS = 7
REQUEST_SLEEP_SECONDS = 1.2
RETRY_ATTEMPTS = 3
MAX_DETAIL_REFRESH_PER_RUN = 10
STOP_AFTER_CONSECUTIVE_429 = 1
MARKETS_PER_PAGE = 250
UNIVERSE_REQUEST_SLEEP_SECONDS = 4.0
UNIVERSE_RETRY_ATTEMPTS = 5

TARGET_BROAD_RWA_CATEGORY_NAMES = [
    "Real World Assets (RWA)",
    "Tokenized Assets",
    "RWA Protocol",
]

TARGET_BROAD_RWA_CATEGORY_ALIASES = {
    "all real world assets (rwa)": "Real World Assets (RWA)",
}

RWA_OUTPUT_COLUMNS = [
    "token",
    "coingecko_id",
    "rwa_label",
    "rwa_category",
    "protocol",
    "confidence",
    "evidence_type",
    "evidence_detail_json",
    "label_source",
    "labeled_at",
]

REVIEW_QUEUE_COLUMNS = [
    "token",
    "coingecko_id",
    "current_price_usd",
    "volume_24h_usd",
    "market_cap_usd",
    "label_source",
    "evidence_type",
    "evidence_detail_json",
    "has_signal_evidence",
]

CORE_CATEGORY_RULES = [
    ("rwa-protocol", ("real world assets", "real-world assets")),
    ("tokenized-gold", ("tokenized gold", "gold-backed token")),
    ("tokenized-treasury", ("tokenized treasury", "tokenized treasuries", "tokenized bond", "tokenized bonds")),
]

RELATED_CATEGORY_RULES = [
    ("yield-bearing-stablecoin", ("yield-bearing stablecoin", "yield bearing stablecoin", "treasury-backed stablecoin")),
    ("asset-backed-stablecoin", ("asset-backed stablecoin", "commodity-backed stablecoin", "gold-backed stablecoin")),
]

CORE_STRONG_PATTERNS = [
    ("rwa-protocol", re.compile(r"\breal[- ]world assets?\b", re.IGNORECASE)),
    ("tokenized-treasury", re.compile(r"\btokenized (?:u\.?s\.? )?(?:treasur(?:y|ies)|t-?bill|bonds?)\b", re.IGNORECASE)),
    ("tokenized-treasury", re.compile(r"\bbacked by (?:short-term )?(?:u\.?s\.? )?treasur(?:y|ies)\b", re.IGNORECASE)),
    ("tokenized-gold", re.compile(r"\btokenized gold\b", re.IGNORECASE)),
    ("tokenized-gold", re.compile(r"\bbacked by physical gold\b", re.IGNORECASE)),
    ("tokenized-gold", re.compile(r"\bgold-backed token\b", re.IGNORECASE)),
]

RELATED_STRONG_PATTERNS = [
    ("yield-bearing-stablecoin", re.compile(r"\byield[- ]bearing stablecoin\b", re.IGNORECASE)),
    ("yield-bearing-stablecoin", re.compile(r"\btreasury-backed stablecoin\b", re.IGNORECASE)),
    ("asset-backed-stablecoin", re.compile(r"\basset-backed stablecoin\b", re.IGNORECASE)),
    ("asset-backed-stablecoin", re.compile(r"\bcommodity-backed stablecoin\b", re.IGNORECASE)),
    ("asset-backed-stablecoin", re.compile(r"\bgold-backed stablecoin\b", re.IGNORECASE)),
]

SOFT_AMBIGUOUS_PATTERNS = [
    re.compile(r"\brwa\b", re.IGNORECASE),
    re.compile(r"\breal[- ]world\b", re.IGNORECASE),
    re.compile(r"\btokenized securities?\b", re.IGNORECASE),
]


@dataclass
class AllowlistEntry:
    coingecko_id: str
    rwa_label: str
    rwa_category: str
    protocol: str
    force_override: bool
    notes: str


def log(message: str):
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[RWA Labels] [{timestamp}] {message}", flush=True)


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def normalize_coin_id(value) -> str:
    return clean_text(value).lower()


def format_number(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return ""
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return clean_text(value)


def parse_bool(value) -> bool:
    return clean_text(value).lower() in {"1", "true", "yes", "y"}


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


def coingecko_headers() -> dict:
    headers = {}
    pro_key = os.getenv("COINGECKO_PRO_API_KEY", "").strip()
    demo_key = os.getenv("COINGECKO_DEMO_API_KEY", "").strip()
    if pro_key:
        headers["x-cg-pro-api-key"] = pro_key
    elif demo_key:
        headers["x-cg-demo-api-key"] = demo_key
    return headers


def coingecko_get_coin_detail(coin_id: str) -> dict:
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "false",
        "community_data": "false",
        "developer_data": "false",
        "sparkline": "false",
    }
    last_error = None
    url = COINGECKO_COIN_DETAIL_URL.format(coin_id=coin_id)
    for attempt in range(RETRY_ATTEMPTS):
        try:
            response = requests.get(url, headers=coingecko_headers(), params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError(f"Unexpected CoinGecko detail response for {coin_id}")
            return data
        except Exception as exc:  # pragma: no cover - exercised in integration
            last_error = exc
            if attempt < RETRY_ATTEMPTS - 1:
                backoff = 10 * (attempt + 1) if "429" in str(exc) else 2**attempt
                log(f"CoinGecko detail fetch failed for {coin_id}; retrying in {backoff}s. Reason: {exc}")
                time.sleep(backoff)
    raise RuntimeError(f"CoinGecko detail fetch failed for {coin_id}: {last_error}")


def coingecko_get_json(url: str, params: dict | None = None) -> dict | list:
    last_error = None
    for attempt in range(UNIVERSE_RETRY_ATTEMPTS):
        try:
            response = requests.get(url, headers=coingecko_headers(), params=params or {}, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # pragma: no cover - integration path
            last_error = exc
            if attempt < UNIVERSE_RETRY_ATTEMPTS - 1:
                backoff = min(120, 15 * (2**attempt)) if "429" in str(exc) else 2**attempt
                log(f"CoinGecko request failed for {url}; retrying in {backoff}s. Reason: {exc}")
                time.sleep(backoff)
    raise RuntimeError(f"CoinGecko request failed for {url}: {last_error}")


def normalize_category_name(value: str) -> str:
    text = clean_text(value).lower()
    return TARGET_BROAD_RWA_CATEGORY_ALIASES.get(text, clean_text(value))


def load_universe_cache(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "target_categories": [], "coin_ids": [], "coins_by_category": {}}

    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if not isinstance(raw, dict):
        raise ValueError(f"Unexpected broad universe cache format: {path}")

    return {
        "schema_version": 1,
        "target_categories": raw.get("target_categories", []),
        "coin_ids": raw.get("coin_ids", []),
        "coins_by_category": raw.get("coins_by_category", {}),
        "fetched_at": raw.get("fetched_at", ""),
        "last_error": raw.get("last_error", ""),
    }


def cache_is_fresh(fetched_at_value: str, now_dt: datetime, ttl_days: int) -> bool:
    fetched_at = parse_datetime(fetched_at_value)
    if not fetched_at:
        return False
    return fetched_at >= now_dt - timedelta(days=ttl_days)


def resolve_broad_rwa_categories(category_rows: list[dict]) -> list[dict]:
    target_lookup = {normalize_category_name(name).lower(): name for name in TARGET_BROAD_RWA_CATEGORY_NAMES}
    resolved: list[dict] = []
    seen = set()
    for row in category_rows:
        if not isinstance(row, dict):
            continue
        name = clean_text(row.get("name"))
        category_id = clean_text(row.get("category_id"))
        if not name or not category_id:
            continue
        normalized_name = normalize_category_name(name).lower()
        requested_name = target_lookup.get(normalized_name)
        if not requested_name or category_id in seen:
            continue
        resolved.append({"requested_name": requested_name, "matched_name": name, "category_id": category_id})
        seen.add(category_id)
    return resolved


def fetch_category_coin_ids(category_id: str) -> list[str]:
    page = 1
    coin_ids: list[str] = []
    while True:
        rows = coingecko_get_json(
            COINGECKO_MARKETS_URL,
            params={
                "vs_currency": "usd",
                "category": category_id,
                "order": "market_cap_desc",
                "per_page": MARKETS_PER_PAGE,
                "page": page,
                "sparkline": "false",
            },
        )
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected category market response for {category_id}")
        if not rows:
            break

        page_ids = [normalize_coin_id(row.get("id")) for row in rows if normalize_coin_id(row.get("id"))]
        coin_ids.extend(page_ids)
        if len(rows) < MARKETS_PER_PAGE:
            break
        page += 1
        time.sleep(UNIVERSE_REQUEST_SLEEP_SECONDS)
    return sorted(set(coin_ids))


def refresh_broad_rwa_universe_cache(path: Path, now_dt: datetime, ttl_days: int) -> dict:
    cache = load_universe_cache(path)
    if cache_is_fresh(cache.get("fetched_at", ""), now_dt, ttl_days) and cache.get("coin_ids"):
        return cache

    try:
        category_rows = coingecko_get_json(COINGECKO_CATEGORIES_LIST_URL)
        if not isinstance(category_rows, list):
            raise RuntimeError("Unexpected CoinGecko category list response.")

        resolved_categories = resolve_broad_rwa_categories(category_rows)
        if len(resolved_categories) != len(TARGET_BROAD_RWA_CATEGORY_NAMES):
            missing = sorted(set(TARGET_BROAD_RWA_CATEGORY_NAMES) - {item["requested_name"] for item in resolved_categories})
            raise RuntimeError(f"Could not resolve broad RWA categories: {missing}")

        coins_by_category: dict[str, list[str]] = {}
        all_coin_ids: set[str] = set()
        for category in resolved_categories:
            category_id = category["category_id"]
            ids = fetch_category_coin_ids(category_id)
            coins_by_category[category_id] = ids
            all_coin_ids.update(ids)
            time.sleep(UNIVERSE_REQUEST_SLEEP_SECONDS)

        cache = {
            "schema_version": 1,
            "target_categories": resolved_categories,
            "coin_ids": sorted(all_coin_ids),
            "coins_by_category": coins_by_category,
            "fetched_at": now_dt.isoformat(),
            "last_error": "",
        }
        save_cache(path, cache)
        log(
            "Refreshed broad CoinGecko RWA universe cache with "
            f"{len(all_coin_ids)} coin ID(s) across {len(resolved_categories)} categories."
        )
        return cache
    except Exception as exc:  # pragma: no cover - integration path
        log(f"Broad CoinGecko RWA universe refresh failed; using existing cache when available. Reason: {exc}")
        cache["last_error"] = clean_text(exc)
        if cache.get("coin_ids"):
            save_cache(path, cache)
            return cache
        return cache


def cache_entry_is_fresh(entry: dict, now_dt: datetime, ttl_days: int) -> bool:
    fetched_at = parse_datetime(entry.get("fetched_at", ""))
    if not fetched_at:
        return False
    return fetched_at >= now_dt - timedelta(days=ttl_days)


def load_allowlist(path: Path) -> tuple[dict[str, AllowlistEntry], dict[str, AllowlistEntry]]:
    if not path.exists():
        raise FileNotFoundError(f"RWA allowlist not found: {path}")

    required_columns = {"coingecko_id", "rwa_label", "rwa_category", "protocol", "force_override", "notes"}
    manual_overrides: dict[str, AllowlistEntry] = {}
    seed_allowlist: dict[str, AllowlistEntry] = {}

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not required_columns.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"RWA allowlist is missing required columns: {sorted(required_columns)}")

        for row in reader:
            coingecko_id = normalize_coin_id(row.get("coingecko_id"))
            rwa_label = clean_text(row.get("rwa_label"))
            if not coingecko_id:
                continue
            if rwa_label not in VALID_RWA_LABELS:
                raise ValueError(f"Invalid rwa_label in allowlist for {coingecko_id}: {rwa_label}")

            entry = AllowlistEntry(
                coingecko_id=coingecko_id,
                rwa_label=rwa_label,
                rwa_category=clean_text(row.get("rwa_category")),
                protocol=clean_text(row.get("protocol")),
                force_override=parse_bool(row.get("force_override")),
                notes=clean_text(row.get("notes")),
            )
            if entry.force_override:
                manual_overrides[coingecko_id] = entry
            else:
                seed_allowlist[coingecko_id] = entry

    return manual_overrides, seed_allowlist


def load_token_market_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Token market metrics not found: {path}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "coins": {}}

    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if isinstance(raw, dict) and isinstance(raw.get("coins"), dict):
        return raw

    if isinstance(raw, dict):
        return {"schema_version": 1, "coins": raw}

    raise ValueError(f"Unexpected cache format: {path}")


def save_cache(path: Path, cache: dict):
    ensure_directory_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(cache)
    payload["schema_version"] = 1
    payload["saved_at"] = datetime.now(timezone.utc).isoformat()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def build_cache_entry(coin_id: str, detail: dict, fetched_at: str) -> dict:
    description = detail.get("description", {})
    categories = detail.get("categories", [])
    return {
        "coingecko_id": coin_id,
        "name": clean_text(detail.get("name")),
        "description": clean_text(description.get("en") if isinstance(description, dict) else ""),
        "categories": [clean_text(item) for item in categories if clean_text(item)] if isinstance(categories, list) else [],
        "fetched_at": fetched_at,
    }


def ids_needing_detail_refresh(
    token_rows: list[dict],
    manual_overrides: dict[str, AllowlistEntry],
    seed_allowlist: dict[str, AllowlistEntry],
    cache: dict,
    now_dt: datetime,
    ttl_days: int,
) -> list[str]:
    refresh_ids = set()
    cache_rows = cache.setdefault("coins", {})

    for row in token_rows:
        coin_id = normalize_coin_id(row.get("coingecko_id"))
        if not coin_id:
            continue
        if coin_id in manual_overrides or coin_id in seed_allowlist:
            continue
        if cache_entry_is_fresh(cache_rows.get(coin_id, {}), now_dt, ttl_days):
            continue
        refresh_ids.add(coin_id)

    return sorted(refresh_ids)


def refresh_detail_cache(cache: dict, coin_ids: list[str], max_refresh_per_run: int):
    if not coin_ids:
        return

    coin_rows = cache.setdefault("coins", {})
    refresh_queue = coin_ids[:max_refresh_per_run]
    if len(coin_ids) > len(refresh_queue):
        log(
            f"Refreshing CoinGecko detail cache for {len(refresh_queue)} coin ID(s) this run "
            f"({len(coin_ids) - len(refresh_queue)} deferred for later cache warm-up)."
        )
    else:
        log(f"Refreshing CoinGecko detail cache for {len(refresh_queue)} coin ID(s).")

    consecutive_429 = 0
    for index, coin_id in enumerate(refresh_queue):
        try:
            detail = coingecko_get_coin_detail(coin_id)
            cache_entry = build_cache_entry(
                coin_id=coin_id,
                detail=detail,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            cache_entry["last_error"] = ""
            coin_rows[coin_id] = cache_entry
            consecutive_429 = 0
        except Exception as exc:  # pragma: no cover - integration path
            log(f"CoinGecko detail fetch failed for {coin_id}; keeping existing cache when available. Reason: {exc}")
            existing = coin_rows.get(coin_id, {"coingecko_id": coin_id})
            existing["last_error"] = clean_text(exc)
            coin_rows[coin_id] = existing

            if "429" in str(exc):
                consecutive_429 += 1
                if consecutive_429 >= STOP_AFTER_CONSECUTIVE_429:
                    log("CoinGecko rate limit is still active; stopping further detail refreshes for this run.")
                    break
            else:
                consecutive_429 = 0

        if index < len(refresh_queue) - 1:
            time.sleep(REQUEST_SLEEP_SECONDS)


def category_rule_matches(categories: list[str], rules: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, str]]:
    normalized = [clean_text(item).lower() for item in categories if clean_text(item)]
    matches: list[tuple[str, str]] = []
    for rwa_category, keywords in rules:
        for category_text in normalized:
            if any(keyword in category_text for keyword in keywords):
                matches.append((rwa_category, category_text))
    return matches


def keyword_matches(text: str, patterns: list[tuple[str, re.Pattern]]) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for rwa_category, pattern in patterns:
        hit = pattern.search(text)
        if hit:
            matches.append((rwa_category, hit.group(0)))
    return matches


def ambiguous_keyword_matches(text: str) -> list[str]:
    hits = []
    for pattern in SOFT_AMBIGUOUS_PATTERNS:
        hit = pattern.search(text)
        if hit:
            hits.append(hit.group(0))
    return hits


def detail_text(entry: dict) -> str:
    name = clean_text(entry.get("name"))
    description = clean_text(entry.get("description"))
    categories = " ".join(clean_text(item) for item in entry.get("categories", []) if clean_text(item))
    return " ".join(part for part in (name, description, categories) if part).lower()


def build_result(
    token: str,
    coingecko_id: str,
    rwa_label: str,
    rwa_category: str,
    protocol: str,
    confidence: float,
    evidence_type: str,
    evidence_detail: dict,
    label_source: str,
    labeled_at: str,
) -> dict:
    return {
        "token": clean_text(token),
        "coingecko_id": normalize_coin_id(coingecko_id),
        "rwa_label": rwa_label,
        "rwa_category": clean_text(rwa_category),
        "protocol": clean_text(protocol),
        "confidence": format_number(confidence),
        "evidence_type": evidence_type,
        "evidence_detail_json": json.dumps(evidence_detail, ensure_ascii=False, sort_keys=True),
        "label_source": label_source,
        "labeled_at": labeled_at,
    }


def broad_rwa_membership(coin_id: str, broad_universe_cache: dict) -> bool | None:
    normalized_coin_id = normalize_coin_id(coin_id)
    if not normalized_coin_id:
        return None
    coin_ids = broad_universe_cache.get("coin_ids", [])
    if not coin_ids:
        return None
    coin_id_set = broad_universe_cache.setdefault("_coin_id_set", set(coin_ids))
    return normalized_coin_id in coin_id_set


def classify_as_non_rwa_from_broad_universe(
    token: str,
    coin_id: str,
    broad_universe_cache: dict,
    labeled_at: str,
) -> dict:
    categories_checked = [
        {
            "requested_name": clean_text(item.get("requested_name")),
            "matched_name": clean_text(item.get("matched_name")),
            "category_id": clean_text(item.get("category_id")),
        }
        for item in broad_universe_cache.get("target_categories", [])
    ]
    return build_result(
        token=token,
        coingecko_id=coin_id,
        rwa_label="non_rwa",
        rwa_category="",
        protocol="",
        confidence=0.88,
        evidence_type="not_in_broad_rwa_universe",
        evidence_detail={
            "broad_rwa_universe_fetched_at": clean_text(broad_universe_cache.get("fetched_at")),
            "categories_checked": categories_checked,
        },
        label_source="coingecko_rwa_universe_gate",
        labeled_at=labeled_at,
    )


def classify_from_allowlist(
    token: str,
    coin_id: str,
    entry: AllowlistEntry,
    source: str,
    labeled_at: str,
) -> dict:
    return build_result(
        token=token,
        coingecko_id=coin_id,
        rwa_label=entry.rwa_label,
        rwa_category=entry.rwa_category,
        protocol=entry.protocol,
        confidence=0.99 if source == "manual_override" else 0.95,
        evidence_type=source,
        evidence_detail={"notes": entry.notes, "force_override": entry.force_override},
        label_source=source,
        labeled_at=labeled_at,
    )


def classify_from_categories(
    token: str,
    coin_id: str,
    cache_entry: dict,
    labeled_at: str,
) -> dict | None:
    categories = cache_entry.get("categories", [])
    core_matches = category_rule_matches(categories, CORE_CATEGORY_RULES)
    related_matches = category_rule_matches(categories, RELATED_CATEGORY_RULES)

    if core_matches and related_matches:
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="review_pending",
            rwa_category="mixed-rwa-signals",
            protocol=clean_text(cache_entry.get("name")),
            confidence=0.55,
            evidence_type="coingecko_categories_conflict",
            evidence_detail={"core_matches": core_matches, "related_matches": related_matches, "categories": categories},
            label_source="cached_coingecko_categories",
            labeled_at=labeled_at,
        )

    if core_matches:
        rwa_category, matched_category = core_matches[0]
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="core",
            rwa_category=rwa_category,
            protocol=clean_text(cache_entry.get("name")),
            confidence=0.84,
            evidence_type="coingecko_categories_core",
            evidence_detail={"matched_category": matched_category, "categories": categories},
            label_source="cached_coingecko_categories",
            labeled_at=labeled_at,
        )

    if related_matches:
        rwa_category, matched_category = related_matches[0]
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="related",
            rwa_category=rwa_category,
            protocol=clean_text(cache_entry.get("name")),
            confidence=0.78,
            evidence_type="coingecko_categories_related",
            evidence_detail={"matched_category": matched_category, "categories": categories},
            label_source="cached_coingecko_categories",
            labeled_at=labeled_at,
        )

    return None


def classify_from_keywords(
    token: str,
    coin_id: str,
    cache_entry: dict | None,
    broad_rwa_candidate: bool,
    labeled_at: str,
) -> dict:
    if not coin_id:
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="review_pending",
            rwa_category="unresolved-identity",
            protocol="",
            confidence=0.15,
            evidence_type="missing_coingecko_id",
            evidence_detail={"reason": "No CoinGecko ID available; symbol-only classification is disallowed."},
            label_source="conservative_keyword_fallback",
            labeled_at=labeled_at,
        )

    if not cache_entry:
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="review_pending",
            rwa_category="missing-detail-cache",
            protocol="",
            confidence=0.20,
            evidence_type="missing_coingecko_detail",
            evidence_detail={"reason": "CoinGecko detail cache entry is unavailable."},
            label_source="conservative_keyword_fallback",
            labeled_at=labeled_at,
        )

    if clean_text(cache_entry.get("last_error")) and not any(
        clean_text(cache_entry.get(field)) for field in ("name", "description")
    ) and not cache_entry.get("categories"):
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="review_pending",
            rwa_category="missing-detail-cache",
            protocol="",
            confidence=0.20,
            evidence_type="missing_coingecko_detail",
            evidence_detail={"reason": clean_text(cache_entry.get("last_error"))},
            label_source="conservative_keyword_fallback",
            labeled_at=labeled_at,
        )

    text = detail_text(cache_entry)
    core_hits = keyword_matches(text, CORE_STRONG_PATTERNS)
    related_hits = keyword_matches(text, RELATED_STRONG_PATTERNS)
    ambiguous_hits = ambiguous_keyword_matches(text)

    if core_hits and related_hits:
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="review_pending",
            rwa_category="mixed-keyword-signals",
            protocol=clean_text(cache_entry.get("name")),
            confidence=0.45,
            evidence_type="keyword_conflict",
            evidence_detail={"core_hits": core_hits, "related_hits": related_hits},
            label_source="conservative_keyword_fallback",
            labeled_at=labeled_at,
        )

    if core_hits:
        rwa_category, matched_text = core_hits[0]
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="core",
            rwa_category=rwa_category,
            protocol=clean_text(cache_entry.get("name")),
            confidence=0.67,
            evidence_type="keyword_core",
            evidence_detail={"matched_text": matched_text},
            label_source="conservative_keyword_fallback",
            labeled_at=labeled_at,
        )

    if related_hits:
        rwa_category, matched_text = related_hits[0]
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="related",
            rwa_category=rwa_category,
            protocol=clean_text(cache_entry.get("name")),
            confidence=0.61,
            evidence_type="keyword_related",
            evidence_detail={"matched_text": matched_text},
            label_source="conservative_keyword_fallback",
            labeled_at=labeled_at,
        )

    if ambiguous_hits:
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="review_pending",
            rwa_category="ambiguous-rwa-language",
            protocol=clean_text(cache_entry.get("name")),
            confidence=0.35,
            evidence_type="keyword_ambiguous",
            evidence_detail={"matched_terms": ambiguous_hits},
            label_source="conservative_keyword_fallback",
            labeled_at=labeled_at,
        )

    if broad_rwa_candidate:
        return build_result(
            token=token,
            coingecko_id=coin_id,
            rwa_label="review_pending",
            rwa_category="broad-rwa-candidate-unresolved",
            protocol=clean_text(cache_entry.get("name")),
            confidence=0.40,
            evidence_type="broad_rwa_candidate_unresolved",
            evidence_detail={
                "categories": cache_entry.get("categories", []),
                "reason": "CoinGecko broad RWA universe includes this asset, but detailed logic could not safely refine it to core or related.",
            },
            label_source="conservative_keyword_fallback",
            labeled_at=labeled_at,
        )

    return build_result(
        token=token,
        coingecko_id=coin_id,
        rwa_label="non_rwa",
        rwa_category="",
        protocol=clean_text(cache_entry.get("name")),
        confidence=0.20,
        evidence_type="no_rwa_signals_found",
        evidence_detail={"categories": cache_entry.get("categories", [])},
        label_source="conservative_keyword_fallback",
        labeled_at=labeled_at,
    )


def classify_token_row(
    row: dict,
    manual_overrides: dict[str, AllowlistEntry],
    seed_allowlist: dict[str, AllowlistEntry],
    cache: dict,
    broad_universe_cache: dict,
    labeled_at: str,
) -> dict:
    token = clean_text(row.get("token"))
    coin_id = normalize_coin_id(row.get("coingecko_id"))

    if coin_id and coin_id in manual_overrides:
        return classify_from_allowlist(token, coin_id, manual_overrides[coin_id], "manual_override", labeled_at)

    if coin_id and coin_id in seed_allowlist:
        return classify_from_allowlist(token, coin_id, seed_allowlist[coin_id], "seed_allowlist", labeled_at)

    membership = broad_rwa_membership(coin_id, broad_universe_cache)
    if coin_id and membership is False:
        return classify_as_non_rwa_from_broad_universe(token, coin_id, broad_universe_cache, labeled_at)

    cache_entry = cache.get("coins", {}).get(coin_id, {}) if coin_id else {}
    category_result = classify_from_categories(token, coin_id, cache_entry, labeled_at) if cache_entry else None
    if category_result:
        return category_result

    return classify_from_keywords(
        token,
        coin_id,
        cache_entry if cache_entry else None,
        broad_rwa_candidate=bool(membership),
        labeled_at=labeled_at,
    )


def validate_output_rows(rows: list[dict]):
    for row in rows:
        rwa_label = clean_text(row.get("rwa_label"))
        if rwa_label not in VALID_RWA_LABELS:
            raise ValueError(f"Invalid rwa_label in output: {rwa_label}")
        if not clean_text(row.get("label_source")):
            raise ValueError(f"Missing label_source for token: {row.get('token')}")
        try:
            confidence = float(row.get("confidence"))
        except (TypeError, ValueError):
            raise ValueError(f"Invalid confidence for token: {row.get('token')}")
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"Confidence out of range for token: {row.get('token')}")


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    ensure_directory_layout()
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_float(value) -> float:
    text = clean_text(value)
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def has_signal_evidence(label_row: dict) -> bool:
    evidence_type = clean_text(label_row.get("evidence_type"))
    if evidence_type in {
        "coingecko_categories_conflict",
        "keyword_conflict",
        "keyword_ambiguous",
    }:
        return True

    detail_text_json = clean_text(label_row.get("evidence_detail_json"))
    if not detail_text_json:
        return False

    try:
        detail = json.loads(detail_text_json)
    except json.JSONDecodeError:
        return False

    if not isinstance(detail, dict):
        return False

    signal_keys = (
        "matched_terms",
        "matched_category",
        "matched_text",
        "core_matches",
        "related_matches",
        "core_hits",
        "related_hits",
    )
    for key in signal_keys:
        value = detail.get(key)
        if value:
            return True
    return False


def build_review_queue(output_rows: list[dict], token_market_rows: list[dict]) -> list[dict]:
    market_by_token = {clean_text(row.get("token")): row for row in token_market_rows}
    queue_rows = []

    for label_row in output_rows:
        if clean_text(label_row.get("rwa_label")) != "review_pending":
            continue

        market_row = market_by_token.get(clean_text(label_row.get("token")), {})
        signal_flag = has_signal_evidence(label_row)
        queue_rows.append(
            {
                "token": clean_text(label_row.get("token")),
                "coingecko_id": normalize_coin_id(label_row.get("coingecko_id")),
                "current_price_usd": clean_text(market_row.get("current_price_usd")),
                "volume_24h_usd": clean_text(market_row.get("volume_24h_usd")),
                "market_cap_usd": clean_text(market_row.get("market_cap_usd")),
                "label_source": clean_text(label_row.get("label_source")),
                "evidence_type": clean_text(label_row.get("evidence_type")),
                "evidence_detail_json": clean_text(label_row.get("evidence_detail_json")),
                "has_signal_evidence": "true" if signal_flag else "false",
            }
        )

    queue_rows.sort(
        key=lambda row: (
            -parse_float(row.get("volume_24h_usd")),
            -parse_float(row.get("market_cap_usd")),
            -int(clean_text(row.get("has_signal_evidence")).lower() == "true"),
            clean_text(row.get("token")).upper(),
        )
    )
    return queue_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Label watchboard tokens for RWA relevance.")
    parser.add_argument("--input", type=Path, default=TOKEN_MARKET_FILE, help=f"Token market metrics CSV (default: {TOKEN_MARKET_FILE})")
    parser.add_argument("--output", type=Path, default=TOKEN_RWA_LABELS_FILE, help=f"RWA labels CSV (default: {TOKEN_RWA_LABELS_FILE})")
    parser.add_argument(
        "--review-queue-output",
        type=Path,
        default=TOKEN_RWA_REVIEW_QUEUE_FILE,
        help=f"Review queue CSV (default: {TOKEN_RWA_REVIEW_QUEUE_FILE})",
    )
    parser.add_argument("--allowlist", type=Path, default=RWA_ALLOWLIST_FILE, help=f"RWA allowlist CSV (default: {RWA_ALLOWLIST_FILE})")
    parser.add_argument("--cache", type=Path, default=COINGECKO_DETAIL_CACHE_FILE, help=f"CoinGecko detail cache path (default: {COINGECKO_DETAIL_CACHE_FILE})")
    parser.add_argument(
        "--broad-universe-cache",
        type=Path,
        default=COINGECKO_RWA_UNIVERSE_CACHE_FILE,
        help=f"Broad CoinGecko RWA universe cache path (default: {COINGECKO_RWA_UNIVERSE_CACHE_FILE})",
    )
    parser.add_argument("--cache-ttl-days", type=int, default=CACHE_TTL_DAYS, help=f"Cache TTL in days (default: {CACHE_TTL_DAYS})")
    parser.add_argument(
        "--broad-universe-cache-ttl-days",
        type=int,
        default=RWA_UNIVERSE_CACHE_TTL_DAYS,
        help=f"Broad RWA universe cache TTL in days (default: {RWA_UNIVERSE_CACHE_TTL_DAYS})",
    )
    parser.add_argument(
        "--max-refresh-per-run",
        type=int,
        default=MAX_DETAIL_REFRESH_PER_RUN,
        help=f"Maximum unresolved CoinGecko detail records to refresh per run (default: {MAX_DETAIL_REFRESH_PER_RUN})",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    token_rows = load_token_market_rows(args.input)
    manual_overrides, seed_allowlist = load_allowlist(args.allowlist)
    cache = load_cache(args.cache)
    now_dt = datetime.now(timezone.utc)
    broad_universe_cache = refresh_broad_rwa_universe_cache(
        args.broad_universe_cache,
        now_dt=now_dt,
        ttl_days=args.broad_universe_cache_ttl_days,
    )

    refresh_ids = ids_needing_detail_refresh(
        token_rows=token_rows,
        manual_overrides=manual_overrides,
        seed_allowlist=seed_allowlist,
        cache=cache,
        now_dt=now_dt,
        ttl_days=args.cache_ttl_days,
    )
    refresh_detail_cache(cache, refresh_ids, args.max_refresh_per_run)
    save_cache(args.cache, cache)

    labeled_at = now_dt.isoformat()
    output_rows = [
        classify_token_row(
            row=row,
            manual_overrides=manual_overrides,
            seed_allowlist=seed_allowlist,
            cache=cache,
            broad_universe_cache=broad_universe_cache,
            labeled_at=labeled_at,
        )
        for row in token_rows
    ]
    output_rows.sort(key=lambda row: (clean_text(row.get("rwa_label")), clean_text(row.get("token")).upper()))
    review_queue_rows = build_review_queue(output_rows, token_rows)

    validate_output_rows(output_rows)
    write_csv(args.output, output_rows, RWA_OUTPUT_COLUMNS)
    write_csv(args.review_queue_output, review_queue_rows, REVIEW_QUEUE_COLUMNS)

    log(f"Wrote {len(output_rows)} token RWA labels to {args.output.name}")
    log(f"Wrote {len(review_queue_rows)} review queue rows to {args.review_queue_output.name}")
    log(f"Manual overrides: {len(manual_overrides)} | Seed allowlist: {len(seed_allowlist)}")
    log(f"CoinGecko detail refresh candidates this run: {len(refresh_ids)}")
    log(f"Broad CoinGecko RWA universe size: {len(broad_universe_cache.get('coin_ids', []))}")


if __name__ == "__main__":
    main()
