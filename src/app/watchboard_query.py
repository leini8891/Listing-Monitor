from __future__ import annotations

"""
Lightweight SQLite query helpers for the perp listing watchboard dashboard.

The SQLite database is a query layer built from archived daily CSV snapshots.
It is not the single source of truth for listing detection or enrichment.
"""

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import csv

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.paths import (
    ARCHIVE_SNAPSHOT_SCRIPT,
    BUILD_HISTORY_SCRIPT,
    HISTORY_DB_FILE as DB_FILE,
    HISTORY_DIR as HISTORY_ROOT,
    SNAPSHOT_SOURCE_FILES as CURRENT_PIPELINE_FILES,
    TOKEN_MARKET_FILE,
)

SGT_TZ = timezone(timedelta(hours=8), name="SGT")
MOVER_MIN_VOLUME_USD = 1_000_000
HOT_NEW_MAX_AGE_DAYS = 30


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def history_csv_files(history_root: Path = HISTORY_ROOT) -> list[Path]:
    if not history_root.exists():
        return []
    return sorted(path for path in history_root.glob("*/*.csv") if path.is_file())


def current_pipeline_files(project_root: Path = PROJECT_ROOT) -> list[Path]:
    return [path for path in CURRENT_PIPELINE_FILES if path.exists()]


def latest_modified_at(paths: list[Path]) -> datetime | None:
    if not paths:
        return None
    return max(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) for path in paths)


def current_market_data_as_of(token_market_file: Path = TOKEN_MARKET_FILE) -> datetime | None:
    if not token_market_file.exists():
        return None

    latest = None
    with token_market_file.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            text = clean_text(row.get("market_data_as_of"))
            if not text:
                continue
            try:
                value = datetime.fromisoformat(text)
            except ValueError:
                continue
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
            if latest is None or value > latest:
                latest = value
    return latest


def query_layer_status(db_path: Path = DB_FILE, history_root: Path = HISTORY_ROOT) -> dict:
    db_exists = db_path.exists()
    db_mtime = datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc) if db_exists else None
    history_files = history_csv_files(history_root)
    latest_history_mtime = None
    if history_files:
        latest_history_mtime = max(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) for path in history_files)
    current_files = current_pipeline_files()
    latest_current_pipeline_mtime = latest_modified_at(current_files)

    needs_refresh = False
    if latest_history_mtime and (not db_mtime or db_mtime < latest_history_mtime):
        needs_refresh = True
    if latest_current_pipeline_mtime and (not db_mtime or db_mtime < latest_current_pipeline_mtime):
        needs_refresh = True

    return {
        "db_path": db_path,
        "db_exists": db_exists,
        "db_updated_at": db_mtime,
        "latest_history_updated_at": latest_history_mtime,
        "latest_current_pipeline_updated_at": latest_current_pipeline_mtime,
        "current_market_data_as_of": current_market_data_as_of(),
        "needs_refresh": needs_refresh,
    }


def rebuild_query_layer(db_path: Path = DB_FILE):
    subprocess.run([sys.executable, str(ARCHIVE_SNAPSHOT_SCRIPT), "--overwrite"], cwd=str(PROJECT_ROOT), check=True)
    subprocess.run([sys.executable, str(BUILD_HISTORY_SCRIPT), "--db", str(db_path)], cwd=str(PROJECT_ROOT), check=True)


def connect_db(db_path: Path = DB_FILE) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def read_sql(sql: str, params: list | tuple | None = None, db_path: Path = DB_FILE) -> pd.DataFrame:
    with connect_db(db_path) as connection:
        return pd.read_sql_query(sql, connection, params=params or [])


def snapshot_dates(db_path: Path = DB_FILE) -> list[str]:
    df = read_sql("SELECT snapshot_date FROM snapshot_runs ORDER BY snapshot_date DESC", db_path=db_path)
    if df.empty:
        return []
    return df["snapshot_date"].tolist()


def latest_snapshot_date(db_path: Path = DB_FILE) -> str | None:
    dates = snapshot_dates(db_path)
    return dates[0] if dates else None


def snapshot_summary(snapshot_date: str, db_path: Path = DB_FILE) -> dict:
    df = read_sql(
        """
        SELECT
            COUNT(*) AS listing_rows,
            COUNT(DISTINCT venue) AS monitored_venues,
            COUNT(DISTINCT base_asset) AS tracked_tokens
        FROM listing_snapshots
        WHERE snapshot_date = ?
        """,
        [snapshot_date],
        db_path=db_path,
    )
    if df.empty:
        return {"listing_rows": 0, "monitored_venues": 0, "tracked_tokens": 0}
    row = df.iloc[0].to_dict()
    return {key: int(value or 0) for key, value in row.items()}


def _to_datetime(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def recent_listings(snapshot_date: str, limit: int = 25, lookback_hours: int = 24, db_path: Path = DB_FILE):
    df = read_sql(
        """
        SELECT
            snapshot_date,
            token,
            venue,
            symbol_display,
            quote_asset,
            settle_ccy,
            contract_type,
            known_listing_time_utc
        FROM token_venue_history
        WHERE snapshot_date = ?
        """,
        [snapshot_date],
        db_path=db_path,
    )
    if df.empty:
        return df, 0, False

    df["known_listing_time_utc"] = _to_datetime(df["known_listing_time_utc"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    recent = df[df["known_listing_time_utc"] >= pd.Timestamp(cutoff)].copy()
    if not recent.empty:
        recent = recent.sort_values(["known_listing_time_utc", "token", "venue"], ascending=[False, True, True])
        return recent.head(limit), int(len(recent)), False

    fallback = df[df["known_listing_time_utc"].notna()].copy()
    fallback = fallback.sort_values(["known_listing_time_utc", "token", "venue"], ascending=[False, True, True])
    return fallback.head(limit), 0, True


def leaderboard(leaderboard_name: str, snapshot_date: str, limit: int = 20, db_path: Path = DB_FILE) -> pd.DataFrame:
    return read_sql(
        """
        SELECT *
        FROM leaderboard_daily
        WHERE snapshot_date = ? AND leaderboard_name = ?
        ORDER BY rank
        LIMIT ?
        """,
        [snapshot_date, leaderboard_name, limit],
        db_path=db_path,
    )


def top_movers(snapshot_date: str, limit: int = 20, min_volume_usd: float = MOVER_MIN_VOLUME_USD, db_path: Path = DB_FILE) -> pd.DataFrame:
    return read_sql(
        """
        SELECT *
        FROM token_metrics_daily
        WHERE snapshot_date = ?
          AND price_change_24h_pct IS NOT NULL
          AND volume_24h_usd IS NOT NULL
          AND volume_24h_usd >= ?
        ORDER BY ABS(price_change_24h_pct) DESC, volume_24h_usd DESC, token ASC
        LIMIT ?
        """,
        [snapshot_date, min_volume_usd, limit],
        db_path=db_path,
    )


def snapshot_market_data_as_of(snapshot_date: str, db_path: Path = DB_FILE) -> str:
    df = read_sql(
        """
        SELECT MAX(market_data_as_of) AS market_data_as_of
        FROM token_market_metrics_daily
        WHERE snapshot_date = ?
          AND NULLIF(market_data_as_of, '') IS NOT NULL
        """,
        [snapshot_date],
        db_path=db_path,
    )
    if df.empty:
        return ""
    return clean_text(df.iloc[0].get("market_data_as_of"))


def token_options(snapshot_date: str, db_path: Path = DB_FILE) -> list[str]:
    df = read_sql(
        """
        SELECT token
        FROM token_metrics_daily
        WHERE snapshot_date = ?
        ORDER BY token
        """,
        [snapshot_date],
        db_path=db_path,
    )
    return df["token"].tolist() if not df.empty else []


def token_profile(token: str, snapshot_date: str, db_path: Path = DB_FILE) -> pd.DataFrame:
    return read_sql(
        """
        SELECT
            tm.snapshot_date,
            tm.token,
            tm.venue_count,
            tm.venues,
            tm.earliest_listing_time_utc,
            tm.earliest_listing_time_sgt,
            tm.listing_age_days,
            mm.coingecko_id,
            mm.current_price_usd,
            mm.price_change_24h_pct,
            mm.volume_24h_usd,
            mm.market_cap_usd,
            mm.market_data_as_of,
            COALESCE(mm.match_status, tm.match_status) AS match_status
        FROM token_metrics_daily tm
        LEFT JOIN token_market_metrics_daily mm
          ON tm.snapshot_date = mm.snapshot_date
         AND tm.token = mm.token
        WHERE tm.snapshot_date = ?
          AND tm.token = ?
        """,
        [snapshot_date, token],
        db_path=db_path,
    )


def token_venue_coverage(token: str, db_path: Path = DB_FILE) -> pd.DataFrame:
    df = read_sql(
        """
        SELECT
            token,
            venue,
            symbol_raw,
            symbol_display,
            quote_asset,
            settle_ccy,
            contract_type,
            MIN(known_listing_time_utc) AS earliest_listing_time_utc,
            MIN(snapshot_date) AS first_seen_snapshot,
            MAX(snapshot_date) AS latest_seen_snapshot
        FROM token_venue_history
        WHERE token = ?
        GROUP BY token, venue, symbol_raw, symbol_display, quote_asset, settle_ccy, contract_type
        ORDER BY earliest_listing_time_utc ASC, venue ASC, symbol_raw ASC
        """,
        [token],
        db_path=db_path,
    )
    if not df.empty:
        df["earliest_listing_time_utc"] = _to_datetime(df["earliest_listing_time_utc"])
    return df


def token_venue_metrics(token: str, snapshot_date: str, db_path: Path = DB_FILE) -> pd.DataFrame:
    return read_sql(
        """
        SELECT
            venue,
            symbol_raw,
            quote_asset,
            last_price,
            price_change_24h_pct,
            volume_24h_base,
            volume_24h_quote,
            turnover_24h_usd,
            open_interest,
            snapshot_time
        FROM venue_ticker_metrics_daily
        WHERE snapshot_date = ?
          AND base_token = ?
        ORDER BY COALESCE(turnover_24h_usd, 0) DESC, venue ASC, symbol_raw ASC
        """,
        [snapshot_date, token],
        db_path=db_path,
    )


def token_expansion_history(token: str, db_path: Path = DB_FILE) -> pd.DataFrame:
    return read_sql(
        """
        SELECT
            snapshot_date,
            COUNT(DISTINCT venue) AS venue_count
        FROM token_venue_history
        WHERE token = ?
        GROUP BY snapshot_date
        ORDER BY snapshot_date
        """,
        [token],
        db_path=db_path,
    )


def venue_options(snapshot_date: str, db_path: Path = DB_FILE) -> list[str]:
    df = read_sql(
        """
        SELECT DISTINCT venue
        FROM listing_snapshots
        WHERE snapshot_date = ?
        ORDER BY venue
        """,
        [snapshot_date],
        db_path=db_path,
    )
    return df["venue"].tolist() if not df.empty else []


def venue_listings(venue: str, snapshot_date: str, db_path: Path = DB_FILE) -> pd.DataFrame:
    df = read_sql(
        """
        SELECT
            snapshot_date,
            venue,
            symbol_raw,
            symbol_display,
            base_asset AS token,
            quote_asset,
            settle_ccy,
            contract_type,
            COALESCE(NULLIF(listing_time_utc, ''), first_seen_at) AS known_listing_time_utc
        FROM listing_snapshots
        WHERE snapshot_date = ?
          AND venue = ?
        ORDER BY token ASC, symbol_raw ASC
        """,
        [snapshot_date, venue],
        db_path=db_path,
    )
    if not df.empty:
        df["known_listing_time_utc"] = _to_datetime(df["known_listing_time_utc"])
    return df


def venue_recent_additions(venue: str, snapshot_date: str, limit: int = 25, db_path: Path = DB_FILE) -> pd.DataFrame:
    df = venue_listings(venue, snapshot_date, db_path=db_path)
    if df.empty:
        return df
    df = df[df["known_listing_time_utc"].notna()].copy()
    return df.sort_values(["known_listing_time_utc", "token"], ascending=[False, True]).head(limit)


def venue_ticker_metrics(venue: str, snapshot_date: str, db_path: Path = DB_FILE) -> pd.DataFrame:
    return read_sql(
        """
        SELECT
            venue,
            symbol_raw,
            base_token,
            quote_asset,
            last_price,
            price_change_24h_pct,
            volume_24h_base,
            volume_24h_quote,
            turnover_24h_usd,
            open_interest,
            snapshot_time
        FROM venue_ticker_metrics_daily
        WHERE snapshot_date = ?
          AND venue = ?
        ORDER BY COALESCE(turnover_24h_usd, 0) DESC, base_token ASC, symbol_raw ASC
        """,
        [snapshot_date, venue],
        db_path=db_path,
    )


def daily_change_counts(db_path: Path = DB_FILE) -> pd.DataFrame:
    return read_sql(
        """
        WITH first_observed AS (
            SELECT
                venue,
                symbol_raw,
                base_asset AS token,
                MIN(snapshot_date) AS first_snapshot_date
            FROM listing_snapshots
            GROUP BY venue, symbol_raw, base_asset
        )
        SELECT
            first_snapshot_date AS snapshot_date,
            COUNT(*) AS new_listing_rows,
            COUNT(DISTINCT token) AS new_tokens,
            COUNT(DISTINCT venue) AS venues_touched
        FROM first_observed
        GROUP BY first_snapshot_date
        ORDER BY first_snapshot_date DESC
        """,
        db_path=db_path,
    )


def token_expansion_summary(db_path: Path = DB_FILE, limit: int = 50) -> pd.DataFrame:
    return read_sql(
        """
        WITH per_day AS (
            SELECT
                snapshot_date,
                base_asset AS token,
                COUNT(DISTINCT venue) AS venue_count
            FROM listing_snapshots
            GROUP BY snapshot_date, base_asset
        ),
        first_last AS (
            SELECT
                token,
                MIN(snapshot_date) AS first_snapshot_date,
                MAX(snapshot_date) AS latest_snapshot_date
            FROM per_day
            GROUP BY token
        )
        SELECT
            fl.token,
            fl.first_snapshot_date,
            fl.latest_snapshot_date,
            first_day.venue_count AS first_venue_count,
            latest_day.venue_count AS latest_venue_count,
            latest_day.venue_count - first_day.venue_count AS venue_expansion
        FROM first_last fl
        JOIN per_day first_day
          ON first_day.token = fl.token
         AND first_day.snapshot_date = fl.first_snapshot_date
        JOIN per_day latest_day
          ON latest_day.token = fl.token
         AND latest_day.snapshot_date = fl.latest_snapshot_date
        ORDER BY venue_expansion DESC, latest_venue_count DESC, fl.token ASC
        LIMIT ?
        """,
        [limit],
        db_path=db_path,
    )


def previous_snapshot_date(snapshot_date: str, db_path: Path = DB_FILE) -> str | None:
    dates = snapshot_dates(db_path)
    try:
        index = dates.index(snapshot_date)
    except ValueError:
        return None
    previous_index = index + 1
    if previous_index >= len(dates):
        return None
    return dates[previous_index]


def snapshot_diff(snapshot_date: str, db_path: Path = DB_FILE):
    previous_date = previous_snapshot_date(snapshot_date, db_path=db_path)
    current_df = venue_listings_for_diff(snapshot_date, db_path=db_path)
    if not previous_date:
        empty_df = current_df.iloc[0:0].copy()
        return previous_date, empty_df, empty_df

    previous_df = venue_listings_for_diff(previous_date, db_path=db_path)
    key_columns = ["venue", "symbol_raw"]

    added = current_df.merge(previous_df[key_columns], on=key_columns, how="left", indicator=True)
    added = added[added["_merge"] == "left_only"].drop(columns=["_merge"])
    removed = previous_df.merge(current_df[key_columns], on=key_columns, how="left", indicator=True)
    removed = removed[removed["_merge"] == "left_only"].drop(columns=["_merge"])

    return previous_date, added.sort_values(["venue", "token", "symbol_raw"]), removed.sort_values(["venue", "token", "symbol_raw"])


def venue_listings_for_diff(snapshot_date: str, db_path: Path = DB_FILE) -> pd.DataFrame:
    df = read_sql(
        """
        SELECT
            snapshot_date,
            venue,
            symbol_raw,
            symbol_display,
            base_asset AS token,
            quote_asset,
            settle_ccy,
            contract_type,
            COALESCE(NULLIF(listing_time_utc, ''), first_seen_at) AS known_listing_time_utc
        FROM listing_snapshots
        WHERE snapshot_date = ?
        """,
        [snapshot_date],
        db_path=db_path,
    )
    if not df.empty:
        df["known_listing_time_utc"] = _to_datetime(df["known_listing_time_utc"])
    return df
