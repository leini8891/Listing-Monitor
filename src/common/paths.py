from __future__ import annotations

from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "config"
SRC_DIR = ROOT_DIR / "src"
DATA_DIR = ROOT_DIR / "data"

RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
MARTS_DIR = DATA_DIR / "marts"
AUDITS_DIR = DATA_DIR / "audits"
HISTORY_DIR = DATA_DIR / "history"
DB_DIR = DATA_DIR / "db"

ENV_FILE = ROOT_DIR / ".env"

STATE_FILE = RAW_DIR / "known_listings.json"
RAW_WATCHBOARD_FILE = RAW_DIR / "listing_watchboard.csv"

CLEAN_WATCHBOARD_FILE = PROCESSED_DIR / "listing_watchboard_clean.csv"
LEGACY_ENRICHED_FILE = PROCESSED_DIR / "listing_watchboard_enriched.csv"
VENUE_TICKER_FILE = PROCESSED_DIR / "venue_ticker_metrics.csv"
TOKEN_MARKET_FILE = PROCESSED_DIR / "token_market_metrics.csv"
TOKEN_METRICS_FILE = PROCESSED_DIR / "listing_watchboard_token_metrics.csv"

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

SNAPSHOT_SOURCE_FILES = [
    CLEAN_WATCHBOARD_FILE,
    TOKEN_MARKET_FILE,
    VENUE_TICKER_FILE,
    TOKEN_METRICS_FILE,
    TOP_VOLUME_FILE,
    TOP_GAINERS_FILE,
    TOP_LOSERS_FILE,
    HOT_NEW_FILE,
]


def ensure_directory_layout():
    for path in (
        CONFIG_DIR,
        SRC_DIR,
        RAW_DIR,
        PROCESSED_DIR,
        MARTS_DIR,
        AUDITS_DIR,
        HISTORY_DIR,
        DB_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
