from __future__ import annotations

from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "config"
SRC_DIR = ROOT_DIR / "src"
DATA_DIR = ROOT_DIR / "data"

RAW_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"
PROCESSED_DIR = DATA_DIR / "processed"
MARTS_DIR = DATA_DIR / "marts"
AUDITS_DIR = DATA_DIR / "audits"
HISTORY_DIR = DATA_DIR / "history"
DB_DIR = DATA_DIR / "db"

ENV_FILE = ROOT_DIR / ".env"
RWA_ALLOWLIST_FILE = CONFIG_DIR / "rwa_allowlist.csv"

STATE_FILE = RAW_DIR / "known_listings.json"
RAW_WATCHBOARD_FILE = RAW_DIR / "listing_watchboard.csv"

CLEAN_WATCHBOARD_FILE = PROCESSED_DIR / "listing_watchboard_clean.csv"
LEGACY_ENRICHED_FILE = PROCESSED_DIR / "listing_watchboard_enriched.csv"
VENUE_TICKER_FILE = PROCESSED_DIR / "venue_ticker_metrics.csv"
TOKEN_MARKET_FILE = PROCESSED_DIR / "token_market_metrics.csv"
TOKEN_METRICS_FILE = PROCESSED_DIR / "listing_watchboard_token_metrics.csv"
TOKEN_RWA_LABELS_FILE = PROCESSED_DIR / "token_rwa_labels.csv"
TOKEN_RWA_REVIEW_QUEUE_FILE = PROCESSED_DIR / "token_rwa_review_queue.csv"
TOKEN_RWA_SHADOW_REVIEW_FILE = PROCESSED_DIR / "token_rwa_shadow_review.csv"
TOKEN_COINGECKO_MAPPING_REVIEW_FILE = PROCESSED_DIR / "token_coingecko_mapping_review.csv"
COINGECKO_DETAIL_CACHE_FILE = CACHE_DIR / "coingecko_coin_details_cache.json"
COINGECKO_SHADOW_DETAIL_CACHE_FILE = CACHE_DIR / "coingecko_shadow_detail_cache.json"
COINGECKO_RWA_UNIVERSE_CACHE_FILE = CACHE_DIR / "coingecko_rwa_universe_cache.json"
COINGECKO_SEARCH_CACHE_FILE = CACHE_DIR / "coingecko_search_cache.json"
RWA_XYZ_PUBLIC_DIRECTORY_CACHE_FILE = CACHE_DIR / "rwa_xyz_public_directory_cache.json"

TOP_VOLUME_FILE = MARTS_DIR / "top_volume_tokens.csv"
TOP_GAINERS_FILE = MARTS_DIR / "top_gainers_tokens.csv"
TOP_LOSERS_FILE = MARTS_DIR / "top_losers_tokens.csv"
HOT_NEW_FILE = MARTS_DIR / "hot_new_tokens.csv"

LISTING_COVERAGE_AUDIT_FILE = AUDITS_DIR / "listing_coverage_audit.csv"
TOKEN_MATCH_AUDIT_FILE = AUDITS_DIR / "token_market_match_audit.csv"
TOKEN_METRICS_AUDIT_FILE = AUDITS_DIR / "token_market_metrics_audit.csv"

HISTORY_DB_FILE = DB_DIR / "listing_watchboard_history.sqlite"

ARCHIVE_SNAPSHOT_SCRIPT = SRC_DIR / "transform" / "archive_daily_snapshot.py"
BUILD_HISTORY_SCRIPT = SRC_DIR / "transform" / "build_history_store.py"
ENRICHMENT_SCRIPT = SRC_DIR / "transform" / "enrich_watchboard_coingecko.py"
RWA_LABELING_SCRIPT = SRC_DIR / "transform" / "label_rwa_tokens.py"

SNAPSHOT_SOURCE_FILES = [
    CLEAN_WATCHBOARD_FILE,
    TOKEN_MARKET_FILE,
    VENUE_TICKER_FILE,
    TOKEN_METRICS_FILE,
    TOP_VOLUME_FILE,
    TOP_GAINERS_FILE,
    TOP_LOSERS_FILE,
    HOT_NEW_FILE,
    TOKEN_RWA_LABELS_FILE,
    TOKEN_RWA_REVIEW_QUEUE_FILE,
]


def ensure_directory_layout():
    for path in (
        CONFIG_DIR,
        SRC_DIR,
        RAW_DIR,
        CACHE_DIR,
        PROCESSED_DIR,
        MARTS_DIR,
        AUDITS_DIR,
        HISTORY_DIR,
        DB_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
