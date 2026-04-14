from __future__ import annotations

"""
Lark presentation layer for the cleaned perp listing watchboard.

Primary input:
  - listing_watchboard_clean.csv

This script is intentionally a push/presentation layer only.
It does not own new-listing detection state and does not read
known_listings.json. For now, "recent listings" are based on
listing timestamps in the cleaned CSV, with a fallback to the most
recent known rows when no listing_time_utc values fall inside the
lookback window.

Market-data note:
  - listing_watchboard_token_metrics.csv contains token-level aggregated
    CoinGecko market data used for token ranking.
  - It must not be interpreted as venue-specific exchange volume.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import warnings
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
    CLEAN_WATCHBOARD_FILE,
    ENRICHMENT_SCRIPT,
    ENV_FILE,
    HOT_NEW_FILE,
    TOKEN_MARKET_FILE,
    TOKEN_METRICS_FILE,
    TOP_GAINERS_FILE,
    TOP_LOSERS_FILE,
    TOP_VOLUME_FILE,
)

load_dotenv(ENV_FILE)

INPUT_FILE = CLEAN_WATCHBOARD_FILE
LOOKBACK_HOURS = 24
RECENT_LISTINGS_LIMIT = 4
DAILY_SECTION_LIMIT = 5
DASHBOARD_TITLE = "Perp Listing Watchboard"
MOVER_MIN_VOLUME_USD = 1_000_000
HOT_NEW_MAX_AGE_DAYS = 30

LARK_WEBHOOK_URL = os.getenv("LARK_WEBHOOK_URL", "").strip()
DASHBOARD_URL = os.getenv("WATCHBOARD_DASHBOARD_URL", "").strip()
HISTORY_DIFF_URL = (
    os.getenv("WATCHBOARD_HISTORY_DIFF_URL", "").strip()
    or os.getenv("WATCHBOARD_HISTORY_URL", "").strip()
)
SGT_TZ = timezone(timedelta(hours=8), name="SGT")

COMMON_DEFAULT_CONTRACT_TYPES = {"linear_perpetual", "perpetual"}
COMMON_SETTLE_CURRENCIES = {"USDT", "USDC", "USD"}

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
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{timestamp}] {message}")


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def parse_datetime(value: str) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_sgt(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(SGT_TZ).strftime("%Y-%m-%d %H:%M:%S SGT")


def format_short_sgt_date(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(SGT_TZ).strftime("%y/%m/%d")


def file_modified_at(path: Path) -> datetime | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def load_rows(csv_path: Path) -> list[dict]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def leaderboard_dependency_files(token_metrics_csv: Path) -> list[Path]:
    return [
        TOKEN_MARKET_FILE,
        token_metrics_csv,
        TOP_VOLUME_FILE,
        TOP_GAINERS_FILE,
        TOP_LOSERS_FILE,
        HOT_NEW_FILE,
    ]


def freshness_status(files: list[Path], now_dt: datetime) -> dict:
    today_sgt = now_dt.astimezone(SGT_TZ).date()
    missing_files = [path.name for path in files if not path.exists()]
    modified_times = {
        path.name: file_modified_at(path)
        for path in files
        if file_modified_at(path) is not None
    }
    stale_files = [
        name
        for name, modified_at in modified_times.items()
        if modified_at.astimezone(SGT_TZ).date() < today_sgt
    ]

    market_data_as_of = None
    if modified_times:
        market_data_as_of = min(modified_times.values())

    return {
        "missing_files": missing_files,
        "stale_files": stale_files,
        "market_data_as_of": market_data_as_of,
        "is_stale": bool(missing_files or stale_files),
    }


def token_market_data_as_of(rows: list[dict]) -> datetime | None:
    latest = None
    for row in rows:
        dt = parse_datetime(row.get("market_data_as_of", ""))
        if dt and (latest is None or dt > latest):
            latest = dt
    return latest


def ensure_fresh_leaderboards(token_metrics_csv: Path, now_dt: datetime) -> dict:
    files = leaderboard_dependency_files(token_metrics_csv)
    status = freshness_status(files, now_dt)
    status["regenerated"] = False
    status["refresh_error"] = ""

    if not status["is_stale"]:
        return status

    log("Leaderboard data is stale or missing; regenerating enrichment pipeline.")
    try:
        subprocess.run(
            [sys.executable, str(ENRICHMENT_SCRIPT)],
            cwd=str(ENRICHMENT_SCRIPT.parent),
            check=True,
        )
        status = freshness_status(files, datetime.now(timezone.utc))
        status["regenerated"] = True
        status["refresh_error"] = ""
        return status
    except subprocess.CalledProcessError as exc:
        status["regenerated"] = False
        status["refresh_error"] = f"refresh_failed_exit_{exc.returncode}"
        return status
    except OSError as exc:
        status["regenerated"] = False
        status["refresh_error"] = f"refresh_failed_os_error_{exc}"
        return status


def venue_label(venue: str) -> str:
    return VENUE_LABELS.get(venue, venue.title())


def row_token(row: dict) -> str:
    return clean_text(row.get("base_asset")).upper()


def row_symbol_display(row: dict) -> str:
    return clean_text(row.get("symbol_display")) or clean_text(row.get("symbol_raw")) or row_token(row)


def row_listing_dt(row: dict) -> datetime | None:
    return parse_datetime(row.get("listing_time_utc", ""))


def row_first_seen_dt(row: dict) -> datetime | None:
    return parse_datetime(row.get("first_seen_at", ""))


def row_sort_dt(row: dict) -> datetime | None:
    return row_listing_dt(row) or row_first_seen_dt(row)


def row_date_display(row: dict) -> str:
    fallback_dt = row_sort_dt(row)
    if fallback_dt:
        return format_short_sgt_date(fallback_dt)

    return ""


def sort_rows_desc(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: (
            row_sort_dt(row) or datetime.min.replace(tzinfo=timezone.utc),
            row_symbol_display(row),
            clean_text(row.get("venue")).lower(),
        ),
        reverse=True,
    )


def recent_listings_24h(rows: list[dict], lookback_hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    recent = []

    for row in rows:
        listing_dt = row_listing_dt(row)
        if listing_dt and listing_dt >= cutoff:
            recent.append(row)

    return sort_rows_desc(recent)


def recent_listing_section_rows(
    rows: list[dict],
    lookback_hours: int,
    limit: int,
) -> tuple[list[dict], int, bool]:
    recent_rows = recent_listings_24h(rows, lookback_hours)
    if recent_rows:
        return recent_rows[:limit], len(recent_rows), False

    fallback_listing_rows = [row for row in rows if row_listing_dt(row)]
    if fallback_listing_rows:
        return sort_rows_desc(fallback_listing_rows)[:limit], 0, True

    fallback_rows = [row for row in rows if row_sort_dt(row)]
    return sort_rows_desc(fallback_rows)[:limit], 0, True


def compute_summary(rows: list[dict], lookback_hours: int) -> dict:
    venues = {
        clean_text(row.get("venue")).lower()
        for row in rows
        if clean_text(row.get("venue"))
    }
    tokens = {
        row_token(row)
        for row in rows
        if row_token(row)
    }
    recent_rows = recent_listings_24h(rows, lookback_hours)

    return {
        "recent_listing_count": len(recent_rows),
        "monitored_venue_count": len(venues),
        "tracked_token_count": len(tokens),
    }


def numeric_value(value) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def format_compact_usd(value) -> str:
    number = numeric_value(value)
    if number is None:
        return "n/a"

    abs_number = abs(number)
    if abs_number >= 1_000_000_000:
        return f"${number / 1_000_000_000:.1f}B"
    if abs_number >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if abs_number >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:.0f}"


def format_pct(value) -> str:
    number = numeric_value(value)
    if number is None:
        return "n/a"
    return f"{number:+.1f}%"


def token_metrics_summary(token_rows: list[dict], recent_listing_count: int, monitored_venue_count: int) -> dict:
    tokens = {
        clean_text(row.get("token")).upper()
        for row in token_rows
        if clean_text(row.get("token"))
    }
    return {
        "recent_listing_count": recent_listing_count,
        "monitored_venue_count": monitored_venue_count,
        "tracked_token_count": len(tokens),
    }


def top_volume_tokens(token_rows: list[dict], limit: int) -> list[dict]:
    eligible = [row for row in token_rows if (numeric_value(row.get("volume_24h_usd")) or 0) > 0]
    ranked = sorted(
        eligible,
        key=lambda row: (
            -(numeric_value(row.get("volume_24h_usd")) or 0),
            clean_text(row.get("token")).upper(),
        ),
    )
    return ranked[:limit]


def top_movers_tokens(token_rows: list[dict], limit: int, min_volume_usd: float) -> list[dict]:
    eligible = []
    for row in token_rows:
        volume = numeric_value(row.get("volume_24h_usd"))
        change = numeric_value(row.get("price_change_24h_pct"))
        if volume is None or change is None:
            continue
        if volume < min_volume_usd:
            continue
        eligible.append(row)

    ranked = sorted(
        eligible,
        key=lambda row: (
            -abs(numeric_value(row.get("price_change_24h_pct")) or 0),
            -(numeric_value(row.get("volume_24h_usd")) or 0),
            clean_text(row.get("token")).upper(),
        ),
    )
    return ranked[:limit]


def hot_new_tokens(token_rows: list[dict], limit: int, max_age_days: float) -> list[dict]:
    eligible = []
    for row in token_rows:
        age_days = numeric_value(row.get("listing_age_days"))
        if age_days is None or age_days > max_age_days:
            continue
        eligible.append(row)

    ranked = sorted(
        eligible,
        key=lambda row: (
            -(numeric_value(row.get("venue_count")) or 0),
            -(numeric_value(row.get("volume_24h_usd")) or 0),
            clean_text(row.get("token")).upper(),
        ),
    )
    return ranked[:limit]


def build_recent_listing_lines(rows: list[dict]) -> str:
    if not rows:
        return "> No recent listings available."

    lines = []
    for row in rows:
        symbol_display = row_symbol_display(row)
        venue = venue_label(clean_text(row.get("venue")).lower())
        date_label = row_date_display(row) or "n/a"

        parts = [f"`{symbol_display}`", venue, date_label]

        contract_type = clean_text(row.get("contract_type"))
        if contract_type and contract_type not in COMMON_DEFAULT_CONTRACT_TYPES:
            parts.append(contract_type)

        settle_ccy = clean_text(row.get("settle_ccy")).upper()
        quote_asset = clean_text(row.get("quote_asset")).upper()
        settle_is_unusual = settle_ccy and settle_ccy not in COMMON_SETTLE_CURRENCIES
        should_show_settle = bool(
            settle_ccy
            and (
                contract_type == "inverse_perpetual"
                or settle_is_unusual
                or (quote_asset and settle_ccy != quote_asset)
            )
        )
        if should_show_settle:
            parts.append(f"settle {settle_ccy}")

        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def build_top_volume_lines(rows: list[dict]) -> str:
    if not rows:
        return "> No volume data available."

    lines = []
    for row in rows:
        token = clean_text(row.get("token")) or "n/a"
        volume = format_compact_usd(row.get("volume_24h_usd"))
        change = format_pct(row.get("price_change_24h_pct"))
        lines.append(f"- `{token}` | {volume} | {change}")
    return "\n".join(lines)


def build_top_movers_lines(rows: list[dict]) -> str:
    if not rows:
        return f"> No mover data above ${MOVER_MIN_VOLUME_USD:,.0f} daily volume."

    lines = []
    for row in rows:
        token = clean_text(row.get("token")) or "n/a"
        change = format_pct(row.get("price_change_24h_pct"))
        volume = format_compact_usd(row.get("volume_24h_usd"))
        lines.append(f"- `{token}` | {change} | {volume}")
    return "\n".join(lines)


def build_hot_new_lines(rows: list[dict]) -> str:
    if not rows:
        return f"> No tokens listed within the last {HOT_NEW_MAX_AGE_DAYS} days."

    lines = []
    for row in rows:
        token = clean_text(row.get("token")) or "n/a"
        venue_count = clean_text(row.get("venue_count")) or "0"
        age_days = numeric_value(row.get("listing_age_days"))
        age_label = f"{age_days:.1f}d" if age_days is not None else "n/a"
        volume = numeric_value(row.get("volume_24h_usd"))

        parts = [f"`{token}`", f"{venue_count} venues", age_label]
        if volume is not None and volume > 0:
            parts.append(format_compact_usd(volume))
        lines.append("- " + " | ".join(parts))

    return "\n".join(lines)


def with_section_columns(header_row: str, body: str) -> str:
    return f"**{header_row}**\n{body}"


def build_footer_actions() -> list[dict]:
    actions = []
    if DASHBOARD_URL:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Open Dashboard"},
                "url": DASHBOARD_URL,
                "type": "default",
            }
        )
    if HISTORY_DIFF_URL:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Open History / Diff"},
                "url": HISTORY_DIFF_URL,
                "type": "default",
            }
        )
    return actions


def build_lark_card(
    rows: list[dict],
    token_rows: list[dict],
    lookback_hours: int,
    recent_limit: int,
    section_limit: int,
    generated_at: datetime,
    market_data_as_of: datetime | None,
    stale_data_note: str,
) -> dict:
    """
    Build a concise Lark message card.

    Docs:
    https://open.larksuite.com/document/common-capabilities/message-card/introduction-of-message-cards
    """
    watchboard_summary = compute_summary(rows, lookback_hours)
    recent_rows, recent_count, used_fallback = recent_listing_section_rows(
        rows,
        lookback_hours,
        recent_limit,
    )
    summary = token_metrics_summary(
        token_rows,
        recent_listing_count=watchboard_summary["recent_listing_count"],
        monitored_venue_count=watchboard_summary["monitored_venue_count"],
    )
    volume_rows = top_volume_tokens(token_rows, section_limit)
    mover_rows = top_movers_tokens(token_rows, section_limit, MOVER_MIN_VOLUME_USD)
    hot_new_rows = hot_new_tokens(token_rows, section_limit, HOT_NEW_MAX_AGE_DAYS)

    header_suffix = (
        f"{summary['recent_listing_count']} listings in last {lookback_hours}h"
        if summary["recent_listing_count"]
        else f"No listings in last {lookback_hours}h"
    )

    elements = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**New Listings 24h**\n{summary['recent_listing_count']}",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Monitored Venues**\n{summary['monitored_venue_count']}",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Tracked Tokens**\n{summary['tracked_token_count']}",
                    },
                },
            ],
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**New Listings 24h**"},
        },
    ]

    if used_fallback:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            f"No rows have listing_time_utc inside the last {lookback_hours}h. "
                            "Showing the most recent known listings instead."
                        ),
                    }
                ],
            }
        )

    if recent_count > recent_limit:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            f"Showing {recent_limit} of {recent_count} recent listings from the last {lookback_hours}h."
                        ),
                    }
                ],
            }
        )

    elements.extend(
        [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": with_section_columns(
                        "Token | Venue | Listed",
                        build_recent_listing_lines(recent_rows),
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "CoinGecko token market view below: token-level aggregated market data, not venue-specific exchange volume.",
                    }
                ],
            },
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**Hot New Tokens**"},
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": with_section_columns(
                        "Token | Venues | Age | Token 24h Volume",
                        build_hot_new_lines(hot_new_rows),
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**Top Volume 24h**"},
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": with_section_columns(
                        "Token | 24h Volume | 24h Price Chg",
                        build_top_volume_lines(volume_rows),
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**Top Movers 24h**"},
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": with_section_columns(
                        "Token | 24h Price Chg | 24h Volume",
                        build_top_movers_lines(mover_rows),
                    ),
                },
            },
            {"tag": "hr"},
        ]
    )

    if stale_data_note:
        elements.extend(
            [
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": stale_data_note,
                        }
                    ],
                },
                {"tag": "hr"},
            ]
        )

    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"Message generated: {format_sgt(generated_at)}",
                },
                {
                    "tag": "plain_text",
                    "content": f"Market data as of: {format_sgt(market_data_as_of) or 'n/a'}",
                },
            ],
        }
    )

    footer_actions = build_footer_actions()
    if footer_actions:
        elements.extend(
            [
                {"tag": "hr"},
                {"tag": "action", "actions": footer_actions},
            ]
        )

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{DASHBOARD_TITLE} | {header_suffix}",
                },
                "template": "red" if summary["recent_listing_count"] else "blue",
            },
            "elements": elements,
        },
    }


def push_to_lark(webhook_url: str, payload: dict) -> bool:
    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log(f"Lark push request failed: {exc}")
        return False

    try:
        result = response.json()
    except ValueError:
        log(f"Lark push failed: non-JSON response with status {response.status_code}")
        return False

    success = (
        result.get("code") == 0
        or result.get("StatusCode") == 0
        or clean_text(result.get("msg")).lower() == "success"
    )
    if success:
        log("Lark push confirmed successful.")
        return True

    log(f"Lark push failed: {json.dumps(result, ensure_ascii=False)}")
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push the cleaned listing watchboard to Lark.")
    parser.add_argument(
        "--csv",
        type=Path,
        default=INPUT_FILE,
        help=f"Cleaned watchboard CSV path (default: {INPUT_FILE.name})",
    )
    parser.add_argument(
        "--token-metrics-csv",
        type=Path,
        default=TOKEN_METRICS_FILE,
        help=f"Token metrics CSV path (default: {TOKEN_METRICS_FILE.name})",
    )
    parser.add_argument(
        "--webhook",
        default="",
        help="Lark webhook URL. Overrides LARK_WEBHOOK_URL from .env when provided.",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=LOOKBACK_HOURS,
        help=f"Lookback window for recent listings (default: {LOOKBACK_HOURS})",
    )
    parser.add_argument(
        "--recent-limit",
        type=int,
        default=RECENT_LISTINGS_LIMIT,
        help=f"Max recent listings to show (default: {RECENT_LISTINGS_LIMIT})",
    )
    parser.add_argument(
        "--section-limit",
        type=int,
        default=DAILY_SECTION_LIMIT,
        help=f"Max rows to show in each daily monitoring section (default: {DAILY_SECTION_LIMIT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the card payload instead of pushing to Lark.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.csv.exists():
        raise FileNotFoundError(f"CSV not found: {args.csv}")

    freshness = ensure_fresh_leaderboards(args.token_metrics_csv, datetime.now(timezone.utc))

    if not args.token_metrics_csv.exists():
        raise FileNotFoundError(f"Token metrics CSV not found: {args.token_metrics_csv}")

    rows = load_rows(args.csv)
    token_rows = load_rows(args.token_metrics_csv)
    token_market_rows = load_rows(TOKEN_MARKET_FILE) if TOKEN_MARKET_FILE.exists() else []
    log(f"Loaded {len(rows)} cleaned watchboard rows from {args.csv}")
    log(f"Loaded {len(token_rows)} token metric rows from {args.token_metrics_csv}")
    if token_market_rows:
        log(f"Loaded {len(token_market_rows)} token market rows from {TOKEN_MARKET_FILE}")

    generated_at = datetime.now(timezone.utc)
    stale_data_note = ""
    if freshness["is_stale"]:
        stale_data_note = "Warning: using stale leaderboard data."
        if freshness.get("refresh_error"):
            stale_data_note += f" Refresh failed: {freshness['refresh_error']}."
    market_data_as_of = token_market_data_as_of(token_market_rows) or freshness.get("market_data_as_of")

    payload = build_lark_card(
        rows=rows,
        token_rows=token_rows,
        lookback_hours=args.lookback_hours,
        recent_limit=args.recent_limit,
        section_limit=args.section_limit,
        generated_at=generated_at,
        market_data_as_of=market_data_as_of,
        stale_data_note=stale_data_note,
    )

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    webhook_url = clean_text(args.webhook) or LARK_WEBHOOK_URL
    if not webhook_url:
        raise SystemExit(
            "Missing webhook. Pass --webhook or set LARK_WEBHOOK_URL in .env."
        )

    push_to_lark(webhook_url, payload)


if __name__ == "__main__":
    main()
