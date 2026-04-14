from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import watchboard_query as wq
from config.coingecko_overrides import override_rows
from src.common.paths import (
    LISTING_COVERAGE_AUDIT_FILE,
    TOKEN_MATCH_AUDIT_FILE as TOKEN_MATCH_AUDIT_PATH,
    TOKEN_METRICS_AUDIT_FILE,
)


st.set_page_config(page_title="Perp Listing Watchboard", layout="wide")

PAGE_LABELS = {
    "overview": "Overview",
    "token": "Token Drill-down",
    "venue": "Venue View",
    "history": "History / Diff",
    "quality": "Data Quality",
}
LABEL_TO_PAGE = {label: key for key, label in PAGE_LABELS.items()}
LOOKBACK_HOURS = 24
TOKEN_MARKET_SCOPE_NOTE = "CoinGecko token-level aggregated market data. Use this for token ranking, not venue-specific exchange volume."
VENUE_PERP_SCOPE_NOTE = "Exchange-specific perp / swap / futures metrics. Use this for venue drill-down and venue market activity."
LISTING_AUDIT_FILE = LISTING_COVERAGE_AUDIT_FILE
TOKEN_AUDIT_FILE = TOKEN_METRICS_AUDIT_FILE
TOKEN_MATCH_AUDIT_FILE = TOKEN_MATCH_AUDIT_PATH


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def read_query_params() -> dict[str, str]:
    try:
        params = dict(st.query_params)
    except Exception:
        params = st.experimental_get_query_params()
    return {key: value[0] if isinstance(value, list) else str(value) for key, value in params.items()}


def write_query_params(**kwargs):
    payload = {key: str(value) for key, value in kwargs.items() if value not in (None, "", [])}
    try:
        st.query_params.clear()
        for key, value in payload.items():
            st.query_params[key] = value
    except Exception:
        st.experimental_set_query_params(**payload)


def format_compact_usd(value) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(numeric):
        return ""
    abs_value = abs(numeric)
    if abs_value >= 1_000_000_000:
        return f"${numeric / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"${numeric / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"${numeric / 1_000:.1f}K"
    return f"${numeric:,.0f}"


def format_number(value, decimals: int = 2) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(numeric):
        return ""
    return f"{numeric:,.{decimals}f}"


def format_pct(value) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(numeric):
        return ""
    return f"{numeric:+.1f}%"


def to_sgt(dt_value) -> pd.Timestamp | None:
    if dt_value in (None, "", pd.NaT):
        return None
    timestamp = pd.to_datetime(dt_value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.tz_convert("Asia/Singapore")


def format_sgt_datetime(dt_value) -> str:
    timestamp = to_sgt(dt_value)
    if timestamp is None:
        return ""
    return timestamp.strftime("%Y-%m-%d %H:%M:%S SGT")


def format_sgt_date(dt_value) -> str:
    timestamp = to_sgt(dt_value)
    if timestamp is None:
        return ""
    return timestamp.strftime("%Y-%m-%d")


def route_caption(page: str, snapshot_date: str, token: str = "", venue: str = ""):
    parts = [f"page={page}", f"snapshot={snapshot_date}"]
    if token:
        parts.append(f"token={token}")
    if venue:
        parts.append(f"venue={venue}")
    st.caption(f"Stable deep-link params for Lark links: `?{'&'.join(parts)}`")


def prepare_recent_listings_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["Listed (SGT)"] = table["known_listing_time_utc"].apply(format_sgt_datetime)
    return table.rename(
        columns={
            "token": "Token",
            "venue": "Venue",
            "symbol_display": "Symbol",
            "quote_asset": "Quote",
            "settle_ccy": "Settle",
            "contract_type": "Contract",
        }
    )[
        ["Token", "Venue", "Symbol", "Listed (SGT)", "Quote", "Settle", "Contract"]
    ]


def prepare_leaderboard_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["24h Price Chg"] = table["price_change_24h_pct"].apply(format_pct)
    table["24h Volume"] = table["volume_24h_usd"].apply(format_compact_usd)
    table["Market Cap"] = table["market_cap_usd"].apply(format_compact_usd)
    table["Earliest Listing (SGT)"] = table["earliest_listing_time_utc"].apply(format_sgt_date)
    table["Venue Count"] = table["venue_count"].fillna(0).astype(int)
    return table.rename(columns={"token": "Token"})[
        ["Token", "Venue Count", "24h Volume", "24h Price Chg", "Market Cap", "Earliest Listing (SGT)"]
    ]


def prepare_token_coverage_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["Earliest Listing (SGT)"] = table["earliest_listing_time_utc"].apply(format_sgt_datetime)
    return table.rename(
        columns={
            "venue": "Venue",
            "symbol_raw": "Symbol",
            "quote_asset": "Quote",
            "settle_ccy": "Settle",
            "contract_type": "Contract",
            "first_seen_snapshot": "First Snapshot",
            "latest_seen_snapshot": "Latest Snapshot",
        }
    )[
        ["Venue", "Symbol", "Quote", "Settle", "Contract", "Earliest Listing (SGT)", "First Snapshot", "Latest Snapshot"]
    ]


def prepare_token_venue_metrics_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["Last Price"] = table["last_price"].apply(format_number)
    table["24h Price Chg"] = table["price_change_24h_pct"].apply(format_pct)
    table["24h Turnover (USD)"] = table["turnover_24h_usd"].apply(format_compact_usd)
    table["Open Interest"] = table["open_interest"].apply(format_number)
    table["Snapshot (SGT)"] = table["snapshot_time"].apply(format_sgt_datetime)
    return table.rename(
        columns={
            "venue": "Venue",
            "symbol_raw": "Symbol",
            "quote_asset": "Quote",
            "volume_24h_base": "24h Volume Base",
            "volume_24h_quote": "24h Volume Quote",
        }
    )[
        ["Venue", "Symbol", "Quote", "Last Price", "24h Price Chg", "24h Volume Base", "24h Volume Quote", "24h Turnover (USD)", "Open Interest", "Snapshot (SGT)"]
    ]


def prepare_venue_listings_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["Listed (SGT)"] = table["known_listing_time_utc"].apply(format_sgt_datetime)
    return table.rename(
        columns={
            "token": "Token",
            "symbol_display": "Symbol",
            "quote_asset": "Quote",
            "settle_ccy": "Settle",
            "contract_type": "Contract",
        }
    )[
        ["Token", "Symbol", "Quote", "Settle", "Contract", "Listed (SGT)"]
    ]


def prepare_change_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.rename(
        columns={
            "snapshot_date": "Snapshot Date",
            "new_listing_rows": "New Listing Rows",
            "new_tokens": "New Tokens",
            "venues_touched": "Venues Touched",
        }
    )


def load_csv_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def top_row_by_numeric(df: pd.DataFrame, field: str, absolute: bool = False) -> pd.Series | None:
    if df.empty or field not in df.columns:
        return None
    table = df.copy()
    table[field] = pd.to_numeric(table[field], errors="coerce")
    table = table[table[field].notna()].copy()
    if table.empty:
        return None
    sort_key = table[field].abs() if absolute else table[field]
    return table.loc[sort_key.sort_values(ascending=False).index[0]]


def show_query_layer_status():
    status = wq.query_layer_status()
    st.sidebar.subheader("Query Layer")
    if status["db_exists"]:
        st.sidebar.caption(f"SQLite: `{status['db_path'].name}`")
        if status["db_updated_at"]:
            st.sidebar.caption(f"DB updated: {format_sgt_datetime(status['db_updated_at'])}")
    else:
        st.sidebar.warning("SQLite query layer has not been built yet.")

    if status["latest_history_updated_at"]:
        st.sidebar.caption(f"History latest: {format_sgt_datetime(status['latest_history_updated_at'])}")
    if status["latest_current_pipeline_updated_at"]:
        st.sidebar.caption(f"Current pipeline latest: {format_sgt_datetime(status['latest_current_pipeline_updated_at'])}")
    if status["current_market_data_as_of"]:
        st.sidebar.caption(f"Current market data as of: {format_sgt_datetime(status['current_market_data_as_of'])}")

    with st.sidebar.expander("Timestamp guide"):
        st.caption("DB updated: when the local SQLite query layer was last rebuilt.")
        st.caption("History latest: newest archived snapshot file timestamp under `data/history/YYYY-MM-DD/`.")
        st.caption("Current pipeline latest: newest timestamp across the current CSV pipeline files under `data/`.")
        st.caption("Current market data as of: latest CoinGecko token-market snapshot timestamp from `data/processed/token_market_metrics.csv`.")

    if not status["db_exists"] or status["needs_refresh"]:
        st.sidebar.warning("SQLite query layer is missing or behind the current pipeline files.")
        if st.sidebar.button("Refresh Query Layer", width="stretch"):
            with st.spinner("Rebuilding SQLite query layer..."):
                wq.rebuild_query_layer()
            st.success("SQLite query layer refreshed.")
            st.rerun()


def ensure_query_layer_current():
    status = wq.query_layer_status()
    if status["db_exists"] and not status["needs_refresh"]:
        return

    with st.spinner("Syncing the latest daily files into SQLite..."):
        wq.rebuild_query_layer()


def render_overview(snapshot_date: str):
    st.header("Overview")
    route_caption("overview", snapshot_date)

    summary = wq.snapshot_summary(snapshot_date)
    market_data_as_of = wq.snapshot_market_data_as_of(snapshot_date)
    recent_df, recent_count, used_fallback = wq.recent_listings(snapshot_date, limit=12, lookback_hours=LOOKBACK_HOURS)
    hot_new_df = wq.leaderboard("hot_new", snapshot_date, limit=12)
    top_volume_df = wq.leaderboard("top_volume", snapshot_date, limit=12)
    top_movers_df = wq.top_movers(snapshot_date, limit=12)

    metric_cols = st.columns(4)
    metric_cols[0].metric("New Listings 24h", recent_count)
    metric_cols[1].metric("Hot New Tokens", len(hot_new_df))
    metric_cols[2].metric("Tracked Tokens", summary["tracked_tokens"])
    metric_cols[3].metric("Monitored Venues", summary["monitored_venues"])

    if used_fallback:
        st.info("No listings fall inside the last 24 hours for the selected snapshot. Showing the most recent known listings instead.")

    st.subheader("Listing View")
    st.dataframe(prepare_recent_listings_table(recent_df), width="stretch", hide_index=True)

    st.subheader("Token Market View")
    market_note = TOKEN_MARKET_SCOPE_NOTE
    if market_data_as_of:
        market_note += f" Market data as of: {format_sgt_datetime(market_data_as_of)}."
    st.caption(market_note)

    left, right = st.columns(2)
    with left:
        st.markdown("**Hot New Tokens**")
        st.dataframe(prepare_leaderboard_table(hot_new_df), width="stretch", hide_index=True)

    with right:
        st.markdown("**Top Volume 24h**")
        st.dataframe(prepare_leaderboard_table(top_volume_df), width="stretch", hide_index=True)
        st.markdown("**Top Movers 24h**")
        st.dataframe(prepare_leaderboard_table(top_movers_df), width="stretch", hide_index=True)


def render_token_page(snapshot_date: str, selected_token: str):
    st.header("Token Drill-down")
    route_caption("token", snapshot_date, token=selected_token)

    profile_df = wq.token_profile(selected_token, snapshot_date)
    coverage_df = wq.token_venue_coverage(selected_token)
    venue_metrics_df = wq.token_venue_metrics(selected_token, snapshot_date)
    expansion_df = wq.token_expansion_history(selected_token)

    if profile_df.empty:
        st.warning(f"No token profile found for `{selected_token}` in snapshot `{snapshot_date}`.")
        return

    profile = profile_df.iloc[0]
    st.subheader("Token Market View")
    st.caption(TOKEN_MARKET_SCOPE_NOTE)
    metric_cols = st.columns(6)
    metric_cols[0].metric("Venue Coverage", int(profile["venue_count"] or 0))
    metric_cols[1].metric("Earliest Listing", clean_text(profile["earliest_listing_time_sgt"]) or format_sgt_date(profile["earliest_listing_time_utc"]))
    metric_cols[2].metric("Price (USD)", format_number(profile["current_price_usd"], 4))
    metric_cols[3].metric("24h Price Chg", format_pct(profile["price_change_24h_pct"]))
    metric_cols[4].metric("24h Volume", format_compact_usd(profile["volume_24h_usd"]))
    metric_cols[5].metric("Market Cap", format_compact_usd(profile["market_cap_usd"]))

    info_cols = st.columns(3)
    info_cols[0].caption(f"CoinGecko ID: `{clean_text(profile['coingecko_id']) or 'n/a'}`")
    info_cols[1].caption(f"Match Status: `{clean_text(profile['match_status']) or 'n/a'}`")
    info_cols[2].caption(f"Market Data As Of: `{format_sgt_datetime(profile['market_data_as_of']) or 'n/a'}`")

    st.subheader("Listing Coverage")
    st.dataframe(prepare_token_coverage_table(coverage_df), width="stretch", hide_index=True)

    st.subheader("Venue Perp View")
    st.caption(VENUE_PERP_SCOPE_NOTE)
    if venue_metrics_df.empty:
        st.info("No venue-level perp metrics are available for this token in the selected snapshot.")
    else:
        st.dataframe(prepare_token_venue_metrics_table(venue_metrics_df), width="stretch", hide_index=True)

    st.subheader("Venue Expansion Over Time")
    if expansion_df.empty:
        st.info("No historical venue coverage data is available for this token yet.")
    else:
        chart_df = expansion_df.set_index("snapshot_date")[["venue_count"]]
        st.line_chart(chart_df, width="stretch")
        st.dataframe(expansion_df, width="stretch", hide_index=True)


def render_venue_page(snapshot_date: str, selected_venue: str):
    st.header("Venue View")
    route_caption("venue", snapshot_date, venue=selected_venue)

    listings_df = wq.venue_listings(selected_venue, snapshot_date)
    recent_additions_df = wq.venue_recent_additions(selected_venue, snapshot_date, limit=20)
    venue_ticker_df = wq.venue_ticker_metrics(selected_venue, snapshot_date)

    metric_cols = st.columns(4)
    metric_cols[0].metric("Tracked Listings", len(listings_df))
    metric_cols[1].metric("Tracked Tokens", listings_df["token"].nunique() if not listings_df.empty else 0)
    metric_cols[2].metric("Recent Additions", len(recent_additions_df))
    metric_cols[3].metric("Venue Ticker Rows", len(venue_ticker_df))

    st.subheader("Venue Listing View")
    st.markdown("**Recent Additions**")
    st.dataframe(prepare_venue_listings_table(recent_additions_df), width="stretch", hide_index=True)

    st.markdown("**Per-venue Listings**")
    token_filter = st.text_input("Filter tokens on this venue", value="")
    filtered_df = listings_df
    if token_filter:
        token_filter_upper = token_filter.upper()
        filtered_df = filtered_df[
            filtered_df["token"].str.upper().str.contains(token_filter_upper, na=False)
            | filtered_df["symbol_raw"].str.upper().str.contains(token_filter_upper, na=False)
        ]
    st.dataframe(prepare_venue_listings_table(filtered_df), width="stretch", hide_index=True)

    st.subheader("Venue Perp View")
    st.caption(VENUE_PERP_SCOPE_NOTE)
    if venue_ticker_df.empty:
        st.info("No venue ticker metrics are available for this venue in the selected snapshot.")
    else:
        top_turnover_row = top_row_by_numeric(venue_ticker_df, "turnover_24h_usd")
        top_mover_row = top_row_by_numeric(venue_ticker_df, "price_change_24h_pct", absolute=True)
        summary_cols = st.columns(2)
        if top_turnover_row is not None:
            summary_cols[0].metric(
                "Top Venue Perp Volume",
                clean_text(top_turnover_row.get("symbol_raw")) or "n/a",
                format_compact_usd(top_turnover_row.get("turnover_24h_usd")),
            )
        if top_mover_row is not None:
            summary_cols[1].metric(
                "Top Venue Movers",
                clean_text(top_mover_row.get("symbol_raw")) or "n/a",
                format_pct(top_mover_row.get("price_change_24h_pct")),
            )
        st.caption("Sorted by 24h Turnover (USD) descending.")
        st.dataframe(prepare_token_venue_metrics_table(venue_ticker_df), width="stretch", hide_index=True)


def render_history_page(snapshot_date: str, history_token: str):
    st.header("History / Diff")
    route_caption("history", snapshot_date, token=history_token)

    changes_df = wq.daily_change_counts()
    previous_snapshot = wq.previous_snapshot_date(snapshot_date)
    previous_snapshot, added_df, removed_df = wq.snapshot_diff(snapshot_date)
    expansion_summary_df = wq.token_expansion_summary(limit=50)

    st.subheader("Changes by Date")
    if changes_df.empty:
        st.info("No archived history is available yet.")
    else:
        st.bar_chart(changes_df.set_index("snapshot_date")[["new_listing_rows"]], width="stretch")
        st.dataframe(prepare_change_table(changes_df), width="stretch", hide_index=True)

    st.subheader("Snapshot Diff")
    if not previous_snapshot:
        st.info("At least two archived snapshot dates are needed to compute diffs.")
    else:
        diff_cols = st.columns(3)
        diff_cols[0].metric("Current Snapshot", snapshot_date)
        diff_cols[1].metric("Previous Snapshot", previous_snapshot)
        diff_cols[2].metric("Net Listing Change", len(added_df) - len(removed_df))

        left, right = st.columns(2)
        with left:
            st.markdown("**Added Since Previous Snapshot**")
            st.dataframe(prepare_venue_listings_table(added_df), width="stretch", hide_index=True)
        with right:
            st.markdown("**Removed Since Previous Snapshot**")
            st.dataframe(prepare_venue_listings_table(removed_df), width="stretch", hide_index=True)

    st.subheader("Venue Expansion Summary")
    st.dataframe(expansion_summary_df, width="stretch", hide_index=True)

    if history_token:
        st.subheader(f"Venue Expansion Over Time: {history_token}")
        token_history_df = wq.token_expansion_history(history_token)
        if token_history_df.empty:
            st.info("No expansion history is available for the selected token.")
        else:
            st.line_chart(token_history_df.set_index("snapshot_date")[["venue_count"]], width="stretch")
            st.dataframe(token_history_df, width="stretch", hide_index=True)


def render_quality_page(snapshot_date: str):
    st.header("Data Quality")
    route_caption("quality", snapshot_date)

    override_df = pd.DataFrame(override_rows())
    token_audit_df = load_csv_table(TOKEN_AUDIT_FILE)
    listing_audit_df = load_csv_table(LISTING_AUDIT_FILE)
    match_audit_df = load_csv_table(TOKEN_MATCH_AUDIT_FILE)

    ambiguous_df = pd.DataFrame()
    if not match_audit_df.empty and "match_status" in match_audit_df.columns:
        ambiguous_df = match_audit_df[match_audit_df["match_status"] == "unmatched_ambiguous_symbol_candidates"].copy()
        if "candidate_count" in ambiguous_df.columns:
            ambiguous_df["candidate_count"] = pd.to_numeric(ambiguous_df["candidate_count"], errors="coerce")
            ambiguous_df = ambiguous_df.sort_values(["candidate_count", "token"], ascending=[False, True])

    metric_cols = st.columns(3)
    metric_cols[0].metric("Override Decisions", len(override_df))
    metric_cols[1].metric("Sample Audit Tokens", len(token_audit_df))
    metric_cols[2].metric("Ambiguous CoinGecko Matches", len(ambiguous_df))

    st.subheader("Override Decisions")
    st.caption("Curated high-confidence CoinGecko mappings applied before symbol-based matching.")
    st.dataframe(override_df.rename(columns={"token": "Token", "coingecko_id": "CoinGecko ID", "reason": "Reason"}), width="stretch", hide_index=True)

    st.subheader("Latest Audit Sample")
    if token_audit_df.empty:
        st.info("No token market audit CSV is available yet.")
    else:
        st.dataframe(token_audit_df.rename(columns={
            "token": "Token",
            "selected_coingecko_id": "CoinGecko ID",
            "candidate_count": "Candidate Count",
            "match_status": "Match Status",
            "current_price_usd": "Price (USD)",
            "volume_24h_usd": "24h Volume",
            "market_cap_usd": "Market Cap",
            "market_data_as_of": "Market Data As Of",
        }), width="stretch", hide_index=True)

    st.subheader("Listing Coverage Audit")
    if listing_audit_df.empty:
        st.info("No listing coverage audit CSV is available yet.")
    else:
        st.dataframe(listing_audit_df.rename(columns={
            "token": "Token",
            "dropped_stage": "Dropped Stage",
            "audit_note": "Audit Note",
        }), width="stretch", hide_index=True)

    st.subheader("Ambiguous CoinGecko Matches")
    if ambiguous_df.empty:
        st.info("No ambiguous CoinGecko matches are present in the latest token match audit.")
    else:
        st.dataframe(
            ambiguous_df.rename(columns={
                "token": "Token",
                "selected_coingecko_id": "CoinGecko ID",
                "candidate_count": "Candidate Count",
                "match_status": "Match Status",
                "market_data_as_of": "Market Data As Of",
            })[["Token", "Candidate Count", "Match Status", "CoinGecko ID", "market_cap_usd", "volume_24h_usd", "Market Data As Of"]].rename(
                columns={
                    "market_cap_usd": "Market Cap",
                    "volume_24h_usd": "24h Volume",
                }
            ),
            width="stretch",
            hide_index=True,
        )


def main():
    st.title("Perp Listing Watchboard")
    st.caption("SQLite-backed internal dashboard for perp listing monitoring and drill-down.")

    ensure_query_layer_current()
    show_query_layer_status()

    snapshot_dates = wq.snapshot_dates()
    if not snapshot_dates:
        st.error("No SQLite snapshot data is available yet. Run `python src/transform/build_history_store.py` after archiving at least one daily snapshot.")
        st.stop()

    query_params = read_query_params()
    initial_page_key = query_params.get("page", "overview")
    if initial_page_key not in PAGE_LABELS:
        initial_page_key = "overview"

    latest_snapshot = snapshot_dates[0]
    initial_snapshot = query_params.get("snapshot", latest_snapshot)
    if initial_snapshot not in snapshot_dates:
        initial_snapshot = latest_snapshot

    st.sidebar.subheader("Navigation")
    selected_page_label = st.sidebar.radio(
        "Page",
        options=list(PAGE_LABELS.values()),
        index=list(PAGE_LABELS.keys()).index(initial_page_key),
    )
    selected_page_key = LABEL_TO_PAGE[selected_page_label]

    selected_snapshot = st.sidebar.selectbox(
        "Snapshot Date",
        options=snapshot_dates,
        index=snapshot_dates.index(initial_snapshot),
    )

    selected_token = ""
    selected_venue = ""

    if selected_page_key in {"token", "history"}:
        token_list = wq.token_options(selected_snapshot)
        default_token = query_params.get("token", token_list[0] if token_list else "")
        if default_token not in token_list and token_list:
            default_token = token_list[0]
        selected_token = st.sidebar.selectbox(
            "Token",
            options=token_list,
            index=token_list.index(default_token) if default_token in token_list else 0,
        ) if token_list else ""

    if selected_page_key == "venue":
        venue_list = wq.venue_options(selected_snapshot)
        default_venue = query_params.get("venue", venue_list[0] if venue_list else "")
        if default_venue not in venue_list and venue_list:
            default_venue = venue_list[0]
        selected_venue = st.sidebar.selectbox(
            "Venue",
            options=venue_list,
            index=venue_list.index(default_venue) if default_venue in venue_list else 0,
        ) if venue_list else ""

    write_query_params(page=selected_page_key, snapshot=selected_snapshot, token=selected_token, venue=selected_venue)

    if selected_page_key == "overview":
        render_overview(selected_snapshot)
    elif selected_page_key == "token":
        if not selected_token:
            st.warning("No token is available for the selected snapshot.")
        else:
            render_token_page(selected_snapshot, selected_token)
    elif selected_page_key == "venue":
        if not selected_venue:
            st.warning("No venue is available for the selected snapshot.")
        else:
            render_venue_page(selected_snapshot, selected_venue)
    elif selected_page_key == "history":
        render_history_page(selected_snapshot, selected_token)
    elif selected_page_key == "quality":
        render_quality_page(selected_snapshot)


if __name__ == "__main__":
    main()
