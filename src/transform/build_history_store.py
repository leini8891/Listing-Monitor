from __future__ import annotations

"""
Build a lightweight SQLite history store from archived daily watchboard snapshots.

Modeling distinction:
  - listing_snapshots stores listing facts only.
  - token_market_metrics_daily stores CoinGecko token-level aggregated market data.
  - venue_ticker_metrics_daily stores venue-specific ticker snapshots from exchange APIs.
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.paths import (
    CLEAN_WATCHBOARD_FILE,
    HISTORY_DB_FILE,
    HISTORY_DIR,
    HOT_NEW_FILE,
    TOKEN_MARKET_FILE,
    TOKEN_METRICS_FILE,
    TOP_GAINERS_FILE,
    TOP_LOSERS_FILE,
    TOP_VOLUME_FILE,
    VENUE_TICKER_FILE,
    ensure_directory_layout,
)


HISTORY_ROOT = HISTORY_DIR
DB_FILE = HISTORY_DB_FILE


def log(message: str):
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[History Store] [{timestamp}] {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the SQLite history store from archived snapshots.")
    parser.add_argument(
        "--history-root",
        type=Path,
        default=HISTORY_ROOT,
        help=f"History root directory (default: {HISTORY_ROOT})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_FILE,
        help=f"SQLite output path (default: {DB_FILE})",
    )
    return parser.parse_args()


def numeric_value(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer_value(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def list_snapshot_dirs(history_root: Path) -> list[Path]:
    if not history_root.exists():
        return []
    return sorted(
        [
            path
            for path in history_root.iterdir()
            if path.is_dir() and len(path.name) == 10 and path.name[4] == "-" and path.name[7] == "-"
        ]
    )


def read_csv_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def connect_db(db_path: Path) -> sqlite3.Connection:
    ensure_directory_layout()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def create_schema(connection: sqlite3.Connection):
    connection.executescript(
        """
        DROP VIEW IF EXISTS token_venue_history;
        DROP VIEW IF EXISTS latest_token_market_metrics;
        DROP VIEW IF EXISTS latest_venue_ticker_metrics;
        DROP VIEW IF EXISTS latest_token_metrics;

        DROP TABLE IF EXISTS leaderboard_daily;
        DROP TABLE IF EXISTS token_metrics_daily;
        DROP TABLE IF EXISTS venue_ticker_metrics_daily;
        DROP TABLE IF EXISTS token_market_metrics_daily;
        DROP TABLE IF EXISTS listing_snapshots;
        DROP TABLE IF EXISTS snapshot_runs;

        CREATE TABLE IF NOT EXISTS snapshot_runs (
            snapshot_date TEXT PRIMARY KEY,
            snapshot_dir TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS listing_snapshots (
            snapshot_date TEXT NOT NULL,
            venue TEXT NOT NULL,
            symbol_raw TEXT NOT NULL,
            symbol_display TEXT,
            base_asset TEXT,
            quote_asset TEXT,
            settle_ccy TEXT,
            contract_type TEXT,
            listing_time_utc TEXT,
            listing_time_sgt TEXT,
            first_seen_at TEXT,
            metadata_json TEXT,
            PRIMARY KEY (snapshot_date, venue, symbol_raw)
        );

        CREATE TABLE IF NOT EXISTS token_market_metrics_daily (
            snapshot_date TEXT NOT NULL,
            token TEXT NOT NULL,
            coingecko_id TEXT,
            current_price_usd REAL,
            price_change_24h_pct REAL,
            volume_24h_usd REAL,
            market_cap_usd REAL,
            market_data_as_of TEXT,
            match_status TEXT,
            PRIMARY KEY (snapshot_date, token)
        );

        CREATE TABLE IF NOT EXISTS venue_ticker_metrics_daily (
            snapshot_date TEXT NOT NULL,
            venue TEXT NOT NULL,
            symbol_raw TEXT NOT NULL,
            base_token TEXT,
            quote_asset TEXT,
            last_price REAL,
            price_change_24h_pct REAL,
            volume_24h_base REAL,
            volume_24h_quote REAL,
            turnover_24h_usd REAL,
            open_interest REAL,
            snapshot_time TEXT,
            PRIMARY KEY (snapshot_date, venue, symbol_raw)
        );

        CREATE TABLE IF NOT EXISTS token_metrics_daily (
            snapshot_date TEXT NOT NULL,
            token TEXT NOT NULL,
            venue_count INTEGER,
            venues TEXT,
            earliest_listing_time_utc TEXT,
            earliest_listing_time_sgt TEXT,
            listing_age_days REAL,
            coingecko_id TEXT,
            current_price_usd REAL,
            price_change_24h_pct REAL,
            volume_24h_usd REAL,
            market_cap_usd REAL,
            match_status TEXT,
            PRIMARY KEY (snapshot_date, token)
        );

        CREATE TABLE IF NOT EXISTS leaderboard_daily (
            snapshot_date TEXT NOT NULL,
            leaderboard_name TEXT NOT NULL,
            rank INTEGER NOT NULL,
            token TEXT NOT NULL,
            venue_count INTEGER,
            venues TEXT,
            earliest_listing_time_utc TEXT,
            earliest_listing_time_sgt TEXT,
            listing_age_days REAL,
            coingecko_id TEXT,
            current_price_usd REAL,
            price_change_24h_pct REAL,
            volume_24h_usd REAL,
            market_cap_usd REAL,
            match_status TEXT,
            PRIMARY KEY (snapshot_date, leaderboard_name, rank, token)
        );

        CREATE INDEX IF NOT EXISTS idx_listing_snapshots_token
            ON listing_snapshots (base_asset, snapshot_date, venue);

        CREATE INDEX IF NOT EXISTS idx_listing_snapshots_venue
            ON listing_snapshots (venue, snapshot_date, base_asset);

        CREATE INDEX IF NOT EXISTS idx_token_market_metrics_daily_token
            ON token_market_metrics_daily (token, snapshot_date);

        CREATE INDEX IF NOT EXISTS idx_venue_ticker_metrics_daily_lookup
            ON venue_ticker_metrics_daily (venue, snapshot_date, base_token);

        CREATE INDEX IF NOT EXISTS idx_token_metrics_daily_token
            ON token_metrics_daily (token, snapshot_date);

        CREATE INDEX IF NOT EXISTS idx_leaderboard_daily_lookup
            ON leaderboard_daily (snapshot_date, leaderboard_name, rank);
        """
    )
    connection.executescript(
        """
        CREATE VIEW token_venue_history AS
        SELECT
            snapshot_date,
            base_asset AS token,
            venue,
            symbol_raw,
            symbol_display,
            quote_asset,
            settle_ccy,
            contract_type,
            COALESCE(NULLIF(listing_time_utc, ''), first_seen_at) AS known_listing_time_utc,
            listing_time_utc,
            first_seen_at
        FROM listing_snapshots;

        CREATE VIEW latest_token_market_metrics AS
        SELECT *
        FROM token_market_metrics_daily
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM token_market_metrics_daily);

        CREATE VIEW latest_venue_ticker_metrics AS
        SELECT *
        FROM venue_ticker_metrics_daily
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM venue_ticker_metrics_daily);

        CREATE VIEW latest_token_metrics AS
        SELECT *
        FROM token_metrics_daily
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM token_metrics_daily);
        """
    )
    connection.commit()


def clear_snapshot_date(connection: sqlite3.Connection, snapshot_date: str):
    connection.execute("DELETE FROM listing_snapshots WHERE snapshot_date = ?", (snapshot_date,))
    connection.execute("DELETE FROM token_market_metrics_daily WHERE snapshot_date = ?", (snapshot_date,))
    connection.execute("DELETE FROM venue_ticker_metrics_daily WHERE snapshot_date = ?", (snapshot_date,))
    connection.execute("DELETE FROM token_metrics_daily WHERE snapshot_date = ?", (snapshot_date,))
    connection.execute("DELETE FROM leaderboard_daily WHERE snapshot_date = ?", (snapshot_date,))
    connection.execute("DELETE FROM snapshot_runs WHERE snapshot_date = ?", (snapshot_date,))


def ingest_listing_snapshot(connection: sqlite3.Connection, snapshot_date: str, rows: list[dict]):
    connection.executemany(
        """
        INSERT OR REPLACE INTO listing_snapshots (
            snapshot_date, venue, symbol_raw, symbol_display, base_asset, quote_asset,
            settle_ccy, contract_type, listing_time_utc, listing_time_sgt, first_seen_at,
            metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_date,
                row.get("venue", ""),
                row.get("symbol_raw", ""),
                row.get("symbol_display", ""),
                row.get("base_asset", ""),
                row.get("quote_asset", ""),
                row.get("settle_ccy", ""),
                row.get("contract_type", ""),
                row.get("listing_time_utc", ""),
                row.get("listing_time_sgt", ""),
                row.get("first_seen_at", ""),
                row.get("metadata_json", ""),
            )
            for row in rows
        ],
    )


def ingest_token_market_metrics(connection: sqlite3.Connection, snapshot_date: str, rows: list[dict]):
    connection.executemany(
        """
        INSERT OR REPLACE INTO token_market_metrics_daily (
            snapshot_date, token, coingecko_id, current_price_usd, price_change_24h_pct,
            volume_24h_usd, market_cap_usd, market_data_as_of, match_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_date,
                row.get("token", ""),
                row.get("coingecko_id", ""),
                numeric_value(row.get("current_price_usd")),
                numeric_value(row.get("price_change_24h_pct")),
                numeric_value(row.get("volume_24h_usd")),
                numeric_value(row.get("market_cap_usd")),
                row.get("market_data_as_of", ""),
                row.get("match_status", ""),
            )
            for row in rows
        ],
    )


def ingest_venue_ticker_metrics(connection: sqlite3.Connection, snapshot_date: str, rows: list[dict]):
    connection.executemany(
        """
        INSERT OR REPLACE INTO venue_ticker_metrics_daily (
            snapshot_date, venue, symbol_raw, base_token, quote_asset, last_price,
            price_change_24h_pct, volume_24h_base, volume_24h_quote, turnover_24h_usd,
            open_interest, snapshot_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_date,
                row.get("venue", ""),
                row.get("symbol_raw", ""),
                row.get("base_token", ""),
                row.get("quote_asset", ""),
                numeric_value(row.get("last_price")),
                numeric_value(row.get("price_change_24h_pct")),
                numeric_value(row.get("volume_24h_base")),
                numeric_value(row.get("volume_24h_quote")),
                numeric_value(row.get("turnover_24h_usd")),
                numeric_value(row.get("open_interest")),
                row.get("snapshot_time", ""),
            )
            for row in rows
        ],
    )


def ingest_token_metrics(connection: sqlite3.Connection, snapshot_date: str, rows: list[dict]):
    connection.executemany(
        """
        INSERT OR REPLACE INTO token_metrics_daily (
            snapshot_date, token, venue_count, venues, earliest_listing_time_utc,
            earliest_listing_time_sgt, listing_age_days, coingecko_id, current_price_usd,
            price_change_24h_pct, volume_24h_usd, market_cap_usd, match_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_date,
                row.get("token", ""),
                integer_value(row.get("venue_count")),
                row.get("venues", ""),
                row.get("earliest_listing_time_utc", ""),
                row.get("earliest_listing_time_sgt", ""),
                numeric_value(row.get("listing_age_days")),
                row.get("coingecko_id", ""),
                numeric_value(row.get("current_price_usd")),
                numeric_value(row.get("price_change_24h_pct")),
                numeric_value(row.get("volume_24h_usd")),
                numeric_value(row.get("market_cap_usd")),
                row.get("match_status", ""),
            )
            for row in rows
        ],
    )


def ingest_leaderboard(connection: sqlite3.Connection, snapshot_date: str, leaderboard_name: str, rows: list[dict]):
    connection.executemany(
        """
        INSERT OR REPLACE INTO leaderboard_daily (
            snapshot_date, leaderboard_name, rank, token, venue_count, venues,
            earliest_listing_time_utc, earliest_listing_time_sgt, listing_age_days,
            coingecko_id, current_price_usd, price_change_24h_pct, volume_24h_usd,
            market_cap_usd, match_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_date,
                leaderboard_name,
                rank,
                row.get("token", ""),
                integer_value(row.get("venue_count")),
                row.get("venues", ""),
                row.get("earliest_listing_time_utc", ""),
                row.get("earliest_listing_time_sgt", ""),
                numeric_value(row.get("listing_age_days")),
                row.get("coingecko_id", ""),
                numeric_value(row.get("current_price_usd")),
                numeric_value(row.get("price_change_24h_pct")),
                numeric_value(row.get("volume_24h_usd")),
                numeric_value(row.get("market_cap_usd")),
                row.get("match_status", ""),
            )
            for rank, row in enumerate(rows, start=1)
        ],
    )


def ingest_snapshot_dir(connection: sqlite3.Connection, snapshot_dir: Path):
    snapshot_date = snapshot_dir.name
    clean_file = snapshot_dir / CLEAN_WATCHBOARD_FILE.name
    token_market_file = snapshot_dir / TOKEN_MARKET_FILE.name
    venue_ticker_file = snapshot_dir / VENUE_TICKER_FILE.name
    token_metrics_file = snapshot_dir / TOKEN_METRICS_FILE.name

    if not clean_file.exists():
        log(f"Skipping {snapshot_date}: missing listing_watchboard_clean.csv.")
        return

    clear_snapshot_date(connection, snapshot_date)

    listing_rows = read_csv_rows(clean_file)
    ingest_listing_snapshot(connection, snapshot_date, listing_rows)

    token_market_rows = []
    if token_market_file.exists():
        token_market_rows = read_csv_rows(token_market_file)
        ingest_token_market_metrics(connection, snapshot_date, token_market_rows)

    venue_ticker_rows = []
    if venue_ticker_file.exists():
        venue_ticker_rows = read_csv_rows(venue_ticker_file)
        ingest_venue_ticker_metrics(connection, snapshot_date, venue_ticker_rows)

    token_metric_rows = []
    if token_metrics_file.exists():
        token_metric_rows = read_csv_rows(token_metrics_file)
        ingest_token_metrics(connection, snapshot_date, token_metric_rows)

    leaderboard_files = {
        "top_volume": snapshot_dir / TOP_VOLUME_FILE.name,
        "top_gainers": snapshot_dir / TOP_GAINERS_FILE.name,
        "top_losers": snapshot_dir / TOP_LOSERS_FILE.name,
        "hot_new": snapshot_dir / HOT_NEW_FILE.name,
    }

    for leaderboard_name, path in leaderboard_files.items():
        if path.exists():
            ingest_leaderboard(connection, snapshot_date, leaderboard_name, read_csv_rows(path))

    connection.execute(
        """
        INSERT OR REPLACE INTO snapshot_runs (snapshot_date, snapshot_dir, imported_at)
        VALUES (?, ?, ?)
        """,
        (
            snapshot_date,
            str(snapshot_dir),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    connection.commit()
    log(
        f"Ingested {snapshot_date}: "
        f"{len(listing_rows)} listing rows, "
        f"{len(token_market_rows)} token market rows, "
        f"{len(venue_ticker_rows)} venue ticker rows, "
        f"{len(token_metric_rows)} token rows."
    )


def main():
    args = parse_args()
    snapshot_dirs = list_snapshot_dirs(args.history_root)
    if not snapshot_dirs:
        raise SystemExit(f"No dated snapshot folders found under {args.history_root}")

    connection = connect_db(args.db)
    try:
        create_schema(connection)
        for snapshot_dir in snapshot_dirs:
            ingest_snapshot_dir(connection, snapshot_dir)
    finally:
        connection.close()

    log(f"Wrote SQLite history store to {args.db}")


if __name__ == "__main__":
    main()
