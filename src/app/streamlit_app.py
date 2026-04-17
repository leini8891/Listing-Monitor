from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

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
from src.common.rwa_public_categories import (
    resolve_public_category,
    supported_public_categories,
    top_level_reference_definitions,
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
RWA_FILTER_OPTIONS = ["All", "RWA", "Non-RWA", "Review Pending"]
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


def route_caption(page: str, snapshot_date: str, token: str = "", venue: str = "", rwa: str = ""):
    parts = [f"page={page}", f"snapshot={snapshot_date}"]
    if token:
        parts.append(f"token={token}")
    if venue:
        parts.append(f"venue={venue}")
    if rwa and rwa != "All":
        parts.append(f"rwa={rwa}")
    st.caption(f"Stable deep-link params for Lark links: `?{'&'.join(parts)}`")


def rwa_badge(label: str) -> str:
    normalized = clean_text(label)
    return {
        "core": "RWA Core",
        "related": "RWA Related",
        "review_pending": "Review Pending",
        "non_rwa": "Non-RWA",
    }.get(normalized, "")


def rwa_badge_or_default(label: str, historical_unavailable: bool = False) -> str:
    badge = rwa_badge(label)
    if badge:
        return badge
    if historical_unavailable:
        return "Not labeled"
    return "Not labeled"


def safe_series(df: pd.DataFrame, column: str, default="") -> pd.Series:
    if column in df.columns:
        return df[column].fillna(default)
    return pd.Series([default] * len(df), index=df.index, dtype="object")


def parse_json_dict(value) -> dict:
    text = clean_text(value)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def humanize_slug(value: str) -> str:
    text = clean_text(value).replace("-", " ").replace("_", " ")
    if not text:
        return ""
    parts = [part for part in text.split() if part]
    return " ".join(part.upper() if part.isupper() else part.capitalize() for part in parts)


def public_entity_name(row) -> str:
    for candidate in [row.get("protocol"), row.get("name"), row.get("display_name")]:
        text = clean_text(candidate)
        if text:
            return text
    coin_id = clean_text(row.get("coingecko_id"))
    if coin_id:
        return humanize_slug(coin_id)
    token = clean_text(row.get("token"))
    if token:
        return token
    return "This token"


def rwa_public_definitions() -> list[str]:
    return [f"{item.public_category_name}: {item.short_definition}" for item in top_level_reference_definitions()]


def public_reference_category(row) -> str:
    category = resolve_public_category(
        rwa_label=clean_text(row.get("rwa_label")),
        rwa_category=clean_text(row.get("rwa_category")),
        evidence_type=clean_text(row.get("evidence_type")),
    )
    if not category:
        return ""
    return category.public_category_name


def public_reference_definition(row) -> str:
    category = resolve_public_category(
        rwa_label=clean_text(row.get("rwa_label")),
        rwa_category=clean_text(row.get("rwa_category")),
        evidence_type=clean_text(row.get("evidence_type")),
    )
    if not category:
        return ""
    if category.family_name:
        return f"{category.short_definition} Part of: {category.family_name}."
    return category.short_definition


def rwa_source_label(source: str) -> str:
    normalized = clean_text(source)
    return {
        "manual_override": "Manual override",
        "seed_allowlist": "Seed allowlist",
        "coingecko_rwa_universe_gate": "CoinGecko broad RWA universe gate",
        "cached_coingecko_categories": "CoinGecko categories",
        "conservative_keyword_fallback": "Conservative keyword fallback",
    }.get(normalized, normalized or "n/a")


def rwa_reason_text(row) -> str:
    source = clean_text(row.get("label_source"))
    evidence_type = clean_text(row.get("evidence_type"))
    category = clean_text(row.get("rwa_category"))
    detail = parse_json_dict(row.get("evidence_detail_json"))
    notes = clean_text(detail.get("notes"))
    name = public_entity_name(row)
    label = clean_text(row.get("rwa_label"))
    reference_category = public_reference_category(row)

    if source == "manual_override":
        if label == "non_rwa":
            return f"{name} is treated as non-RWA under a curated policy decision rather than automatic evidence matching."
        if label == "core":
            return f"{name} is treated as a core RWA asset under a curated policy decision."
        if label == "related":
            return f"{name} is treated as RWA-related under a curated policy decision."
        return f"{name} is handled through a curated policy decision rather than automatic evidence matching."
    if source == "seed_allowlist":
        if label == "core":
            if reference_category:
                return f"{name} is treated as {reference_category} exposure based on the project's curated coverage of known on-chain asset exposures."
            return f"{name} is treated as a tokenized real-world asset based on the project's curated coverage of known on-chain asset exposures."
        if label == "related":
            if reference_category:
                return f"{name} is treated as RWA-related because it sits in the {reference_category} layer that supports real-world assets on-chain."
            return f"{name} is treated as RWA-related because it is part of the infrastructure, protocol, or credit layer that supports real-world assets on-chain."
        if label == "non_rwa":
            return f"{name} is treated as non-RWA under the project's curated coverage policy."
        return f"{name} is classified through the project's curated coverage baseline."
    if evidence_type == "not_in_broad_rwa_universe":
        return (
            f"{name} is treated as a crypto-native token or general blockchain asset rather than a tokenized real-world asset. "
            "It also does not appear in CoinGecko's broad RWA categories."
        )

    if evidence_type == "missing_coingecko_id":
        return f"{name} is still under review because its market identity has not been resolved with enough confidence yet."
    if evidence_type == "missing_coingecko_detail":
        upstream_reason = clean_text(detail.get("reason"))
        if "429" in upstream_reason:
            return f"{name} is still under review because the reference data needed for a safer label was temporarily unavailable."
        return f"{name} is still under review because the reference data needed for a safer label is incomplete or unavailable."
    if evidence_type == "broad_rwa_candidate_unresolved":
        return (
            f"{name} appears close enough to the RWA theme to deserve review, but the available evidence is not strong enough yet "
            "to place it confidently in Tokenized Assets or RWA Protocol."
        )
    if evidence_type == "keyword_ambiguous":
        return f"{name} is still under review because the public wording around the project hints at RWA exposure, but not clearly enough for a confident label."
    if evidence_type == "keyword_conflict":
        return f"{name} is still under review because the available descriptions point to more than one plausible RWA interpretation."
    if evidence_type == "coingecko_categories_conflict":
        return f"{name} is still under review because category signals point to mixed RWA interpretations."
    if evidence_type == "coingecko_categories_core":
        matched_category = clean_text(detail.get("matched_category"))
        ref = reference_category or "Tokenized Assets"
        if matched_category:
            return f"{name} is treated as a core RWA asset because its category profile points to direct {ref} exposure ({matched_category})."
        return f"{name} is treated as a core RWA asset because its category profile points to direct {ref} exposure."
    if evidence_type == "coingecko_categories_related":
        matched_category = clean_text(detail.get("matched_category"))
        ref = reference_category or "RWA Protocol"
        if matched_category:
            return f"{name} is treated as RWA-related because its category profile points to {ref} exposure connected to tokenized assets ({matched_category})."
        return f"{name} is treated as RWA-related because its category profile points to {ref} exposure connected to tokenized assets."
    if evidence_type == "keyword_core":
        matched_text = clean_text(detail.get("matched_text"))
        ref = reference_category or "Tokenized Assets"
        if matched_text:
            return f"{name} is treated as a core RWA asset because the project description points to direct {ref} exposure ({matched_text})."
        return f"{name} is treated as a core RWA asset because the project description points to direct {ref} exposure."
    if evidence_type == "keyword_related":
        matched_text = clean_text(detail.get("matched_text"))
        ref = reference_category or "RWA Protocol"
        if matched_text:
            return f"{name} is treated as RWA-related because the project description points to {ref} exposure ({matched_text})."
        return f"{name} is treated as RWA-related because the project description points to {ref} exposure."
    if evidence_type == "no_rwa_signals_found":
        return f"{name} is treated as non-RWA because the available reference data does not show tokenized-asset exposure or RWA protocol exposure."

    if category:
        return f"{name} is currently grouped under {category}."
    return f"{name} is labeled using the current RWA evidence model."


def rwa_reason_display(row, historical_unavailable: bool = False) -> str:
    label = clean_text(row.get("rwa_label"))
    source = clean_text(row.get("label_source"))
    evidence_type = clean_text(row.get("evidence_type"))
    category = clean_text(row.get("rwa_category"))
    detail = clean_text(row.get("evidence_detail_json"))
    if any([label, source, evidence_type, category, detail]):
        return rwa_reason_text(row)
    if historical_unavailable:
        return "Historical snapshot has no RWA labels yet."
    return "No RWA label is available for this token in the selected snapshot."


def broad_universe_category_names(detail: dict) -> list[str]:
    categories_checked = detail.get("categories_checked") or []
    names = []
    for item in categories_checked:
        name = clean_text(item.get("requested_name")) or clean_text(item.get("matched_name"))
        if name and name not in names:
            names.append(name)
    return names


def rwa_evidence_summary_lines(row, historical_unavailable: bool = False) -> list[str]:
    label = clean_text(row.get("rwa_label"))
    source = clean_text(row.get("label_source"))
    evidence_type = clean_text(row.get("evidence_type"))
    category = clean_text(row.get("rwa_category"))
    detail = parse_json_dict(row.get("evidence_detail_json"))
    notes = clean_text(detail.get("notes"))
    name = public_entity_name(row)

    if not any([label, source, evidence_type, category, detail]):
        if historical_unavailable:
            return [
                "This historical snapshot does not have persisted RWA labels.",
                "The dashboard shows a display fallback instead of a historical label.",
            ]
        return ["No RWA label is available for this token in the selected snapshot."]

    if source == "manual_override":
        lines = [
            "Reviewed through a curated policy decision.",
            "This label comes from maintained coverage rules rather than automatic matching.",
        ]
        if notes:
            lines.append(notes)
        return lines[:4]

    if source == "seed_allowlist":
        lines = [
            "Covered in the project's curated RWA baseline.",
            "Used for well-known assets or protocols where the business classification is already clear.",
        ]
        if category:
            lines.append(f"Assigned category: {category}.")
        if notes:
            lines.append(notes)
        return lines[:4]

    if evidence_type == "not_in_broad_rwa_universe":
        names = broad_universe_category_names(detail) or ["Real World Assets (RWA)", "RWA Protocol", "Tokenized Assets"]
        lines = ["Checked against CoinGecko's broad RWA categories."]
        lines.extend([f"No match found in {name}." for name in names[:3]])
        return lines[:4]

    if evidence_type == "missing_coingecko_id":
        return [
            "No stable market identity has been confirmed for this token yet.",
            "The product avoids symbol-only labeling when identity is uncertain.",
            "It stays in review until the mapping is confirmed.",
        ]

    if evidence_type == "missing_coingecko_detail":
        reason = clean_text(detail.get("reason"))
        lines = []
        if clean_text(row.get("coingecko_id")):
            lines.append("A stable market identity has been found for this token.")
        if "429" in reason:
            lines.append("Reference data was temporarily rate-limited during this run.")
        else:
            lines.append("Reference data was unavailable for this token in this run.")
        lines.append("It stays in review until richer reference data is available.")
        return lines[:4]

    if evidence_type == "broad_rwa_candidate_unresolved":
        lines = [
            "It appears close enough to the RWA theme to deserve a closer look.",
            "The available evidence is not strong enough yet for a confident tokenized-asset or RWA-protocol label.",
            "It stays in review until stronger evidence is available.",
        ]
        if category:
            lines.append(f"Current review category: {category}.")
        return lines[:4]

    if evidence_type == "keyword_ambiguous":
        matched_terms = detail.get("matched_terms") or []
        lines = [
            "Public descriptions contain some RWA-like wording.",
            "The wording is too broad or weak for a firm public label.",
            "It stays in review instead of being force-classified.",
        ]
        if matched_terms:
            lines.append(f"Examples of broad wording: {', '.join(map(clean_text, matched_terms[:3]))}.")
        return lines[:4]

    if evidence_type == "keyword_conflict":
        return [
            "Public descriptions point to mixed RWA interpretations.",
            "The available signals conflict with each other.",
            "It stays in review instead of forcing a label.",
        ]

    if evidence_type == "coingecko_categories_conflict":
        return [
            "Category signals point in mixed directions.",
            "The available category evidence is not consistent enough for a final label.",
            "It stays in review instead of forcing a label.",
        ]

    if evidence_type == "coingecko_categories_core":
        matched_category = clean_text(detail.get("matched_category"))
        lines = [
            "Category evidence supports direct real-world asset exposure.",
            "That is strong enough for a core RWA label.",
        ]
        if matched_category:
            lines.append(f"Matched category: {matched_category}.")
        return lines[:4]

    if evidence_type == "coingecko_categories_related":
        matched_category = clean_text(detail.get("matched_category"))
        lines = [
            "Category evidence supports RWA protocol or tokenization-infrastructure exposure.",
            "That is strong enough for a related RWA label.",
        ]
        if matched_category:
            lines.append(f"Matched category: {matched_category}.")
        return lines[:4]

    if evidence_type == "keyword_core":
        matched_text = clean_text(detail.get("matched_text"))
        lines = [
            "Project description language supports direct real-world asset exposure.",
            "That wording is strong enough for a core RWA label.",
        ]
        if matched_text:
            lines.append(f"Matched wording: {matched_text}.")
        return lines[:4]

    if evidence_type == "keyword_related":
        matched_text = clean_text(detail.get("matched_text"))
        lines = [
            "Project description language supports RWA protocol or tokenization infrastructure exposure.",
            "That wording is strong enough for a related RWA label.",
        ]
        if matched_text:
            lines.append(f"Matched wording: {matched_text}.")
        return lines[:4]

    if evidence_type == "no_rwa_signals_found":
        return [
            "No coverage, category, or description signal suggests RWA exposure.",
            "It remains classified as non-RWA under the current policy.",
        ]

    lines = []
    if category:
        lines.append(f"Assigned category: {category}.")
    if label:
        lines.append(f"Current label: {rwa_badge(label) or label}.")
    lines.append("No richer public evidence summary is available for this label yet.")
    return lines[:4]


def rwa_evidence_summary_display(row, historical_unavailable: bool = False) -> str:
    return "\n".join(f"- {line}" for line in rwa_evidence_summary_lines(row, historical_unavailable))


def market_metric_missing_reason(row, field: str) -> str:
    match_status = clean_text(row.get("match_status"))
    coingecko_id = clean_text(row.get("coingecko_id"))
    if "ambiguous" in match_status:
        return "No market match (ambiguous)"
    if match_status.startswith("unmatched_"):
        return "No market match"
    if coingecko_id:
        return {
            "volume_24h_usd": "Upstream 24h volume unavailable",
            "price_change_24h_pct": "Upstream 24h price change unavailable",
            "market_cap_usd": "Upstream market cap unavailable",
        }.get(field, "Upstream market data unavailable")
    return "No token-level market metrics matched"


def format_market_metric(row, field: str, formatter) -> str:
    value = row.get(field)
    formatted = formatter(value)
    if formatted:
        return formatted
    return market_metric_missing_reason(row, field)


def open_interest_display(value) -> str:
    formatted = format_number(value)
    return formatted or "Venue snapshot did not capture open interest"


def fetch_status_display(value) -> str:
    text = clean_text(value)
    return text or "Historical snapshot predates fetch-status tracking"


def freshness_display(value) -> str:
    text = clean_text(value)
    return text or "Historical snapshot predates freshness tracking"


def merge_rwa_labels(df: pd.DataFrame, rwa_df: pd.DataFrame, token_col: str = "token") -> pd.DataFrame:
    if df.empty or rwa_df.empty or token_col not in df.columns or "token" not in rwa_df.columns:
        return df
    label_cols = [
        "token",
        "rwa_label",
        "rwa_category",
        "protocol",
        "label_source",
        "evidence_type",
        "evidence_detail_json",
        "confidence",
        "labeled_at",
    ]
    available = [col for col in label_cols if col in rwa_df.columns]
    labels = rwa_df[available].drop_duplicates(subset=["token"])
    merged = df.merge(labels, left_on=token_col, right_on="token", how="left", suffixes=("", "_rwa"))
    if token_col != "token" and "token_rwa" in merged.columns:
        merged = merged.drop(columns=["token_rwa"])
    return merged


def apply_rwa_filter(df: pd.DataFrame, rwa_filter: str, label_col: str = "rwa_label") -> pd.DataFrame:
    if df.empty or rwa_filter == "All":
        return df
    if label_col not in df.columns:
        return df.iloc[0:0].copy()
    if rwa_filter == "RWA":
        return df[df[label_col].isin(["core", "related"])].copy()
    if rwa_filter == "Non-RWA":
        return df[df[label_col] == "non_rwa"].copy()
    if rwa_filter == "Review Pending":
        return df[df[label_col] == "review_pending"].copy()
    return df


def prepare_recent_listings_table(df: pd.DataFrame, historical_rwa_unavailable: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["Listed (SGT)"] = table["known_listing_time_utc"].apply(format_sgt_datetime)
    table["RWA"] = safe_series(table, "rwa_label").apply(lambda value: rwa_badge_or_default(value, historical_rwa_unavailable))
    table["Why this label"] = table.apply(lambda row: rwa_reason_display(row, historical_rwa_unavailable), axis=1)
    return table.rename(
        columns={
            "token": "Token",
            "venue": "Venue",
            "symbol_display": "Symbol",
            "quote_asset": "Quote",
            "settle_ccy": "Settle",
            "contract_type": "Contract",
        }
    )[["Token", "RWA", "Why this label", "Venue", "Symbol", "Listed (SGT)", "Quote", "Settle", "Contract"]]


def prepare_leaderboard_table(df: pd.DataFrame, historical_rwa_unavailable: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["RWA"] = safe_series(table, "rwa_label").apply(lambda value: rwa_badge_or_default(value, historical_rwa_unavailable))
    table["Why this label"] = table.apply(lambda row: rwa_reason_display(row, historical_rwa_unavailable), axis=1)
    table["24h Price Chg"] = table.apply(lambda row: format_market_metric(row, "price_change_24h_pct", format_pct), axis=1)
    table["24h Volume"] = table.apply(lambda row: format_market_metric(row, "volume_24h_usd", format_compact_usd), axis=1)
    table["Market Cap"] = table.apply(lambda row: format_market_metric(row, "market_cap_usd", format_compact_usd), axis=1)
    table["Earliest Listing (SGT)"] = table["earliest_listing_time_utc"].apply(format_sgt_date)
    table["Venue Count"] = table["venue_count"].fillna(0).astype(int)
    return table.rename(columns={"token": "Token"})[
        ["Token", "RWA", "Why this label", "Venue Count", "24h Volume", "24h Price Chg", "Market Cap", "Earliest Listing (SGT)"]
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
    table["RWA"] = safe_series(table, "rwa_label").apply(rwa_badge_or_default)
    table["Last Price"] = table["last_price"].apply(format_number)
    table["24h Price Chg"] = table["price_change_24h_pct"].apply(format_pct)
    table["24h Turnover (USD)"] = table["turnover_24h_usd"].apply(format_compact_usd)
    table["Open Interest"] = table["open_interest"].apply(open_interest_display)
    table["Snapshot (SGT)"] = table["snapshot_time"].apply(format_sgt_datetime)
    table["Fetch Status"] = safe_series(table, "fetch_status").apply(fetch_status_display)
    table["Freshness"] = safe_series(table, "data_freshness").apply(freshness_display)
    return table.rename(
        columns={
            "venue": "Venue",
            "symbol_raw": "Symbol",
            "quote_asset": "Quote",
            "volume_24h_base": "24h Volume Base",
            "volume_24h_quote": "24h Volume Quote",
        }
    )[
        [
            "RWA",
            "Venue",
            "Symbol",
            "Quote",
            "Last Price",
            "24h Price Chg",
            "24h Volume Base",
            "24h Volume Quote",
            "24h Turnover (USD)",
            "Open Interest",
            "Fetch Status",
            "Freshness",
            "Snapshot (SGT)",
        ]
    ]


def prepare_venue_listings_table(df: pd.DataFrame, historical_rwa_unavailable: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["Listed (SGT)"] = table["known_listing_time_utc"].apply(format_sgt_datetime)
    table["RWA"] = safe_series(table, "rwa_label").apply(lambda value: rwa_badge_or_default(value, historical_rwa_unavailable))
    table["Why this label"] = table.apply(lambda row: rwa_reason_display(row, historical_rwa_unavailable), axis=1)
    return table.rename(
        columns={
            "token": "Token",
            "symbol_display": "Symbol",
            "quote_asset": "Quote",
            "settle_ccy": "Settle",
            "contract_type": "Contract",
        }
    )[["Token", "RWA", "Why this label", "Symbol", "Quote", "Settle", "Contract", "Listed (SGT)"]]


def prepare_rwa_review_queue_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["RWA"] = safe_series(table, "rwa_label").apply(rwa_badge_or_default)
    table["Reference category"] = table.apply(public_reference_category, axis=1)
    table["Why this label"] = table.apply(rwa_reason_display, axis=1)
    table["Evidence summary"] = table.apply(rwa_evidence_summary_display, axis=1)
    table["Price (USD)"] = table["current_price_usd"].apply(lambda value: format_number(value, 4))
    table["24h Volume"] = table["volume_24h_usd"].apply(format_compact_usd)
    table["Market Cap"] = table["market_cap_usd"].apply(format_compact_usd)
    return table.rename(
        columns={
            "token": "Token",
            "coingecko_id": "CoinGecko ID",
        }
    )[
        ["Token", "RWA", "Reference category", "Why this label", "Evidence summary", "CoinGecko ID", "Price (USD)", "24h Volume", "Market Cap"]
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


def prepare_expansion_summary_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    table = df.copy()
    table["RWA"] = safe_series(table, "rwa_label").apply(rwa_badge_or_default)
    table["Why this label"] = table.apply(rwa_reason_display, axis=1)
    columns = []
    if "token" in table.columns:
        columns.append("token")
    columns.extend(["RWA", "Why this label"])
    columns.extend(
        [
            col
            for col in ["first_snapshot_date", "latest_snapshot_date", "first_venue_count", "latest_venue_count", "venue_expansion"]
            if col in table.columns
        ]
    )
    renamed = table.rename(
        columns={
            "token": "Token",
            "first_snapshot_date": "First Snapshot",
            "latest_snapshot_date": "Latest Snapshot",
            "first_venue_count": "First Venue Count",
            "latest_venue_count": "Latest Venue Count",
            "venue_expansion": "Venue Expansion",
        }
    )
    selected_columns = []
    column_name_map = {
        "token": "Token",
        "first_snapshot_date": "First Snapshot",
        "latest_snapshot_date": "Latest Snapshot",
        "first_venue_count": "First Venue Count",
        "latest_venue_count": "Latest Venue Count",
        "venue_expansion": "Venue Expansion",
    }
    for col in columns:
        selected_columns.append(column_name_map.get(col, col))
    return renamed[selected_columns]


def load_csv_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def render_token_market_missing_note(rwa_history_missing: bool = False):
    if rwa_history_missing:
        st.info("This historical snapshot does not have persisted RWA labels yet. RWA columns below show display fallbacks such as `Not labeled` and a historical-label note.")
    st.caption(
        "When `24h Volume`, `24h Price Chg`, or `Market Cap` show a reason instead of a number, the token either did not match a token-level CoinGecko market snapshot for that date or CoinGecko did not return that field."
    )


def render_venue_perp_column_guide():
    st.caption("Open Interest: venue-level open interest when the snapshot captured it; otherwise the venue did not expose or we did not archive it.")
    st.caption("Fetch Status: fetch result for that venue snapshot. Older archived snapshots may predate this status field and will say so explicitly.")
    st.caption("Freshness: whether the row is fresh or a stale fallback reused from the previous successful ticker snapshot. Older snapshots may predate this field.")


def render_quality_column_glossary():
    with st.expander("Column glossary", expanded=False):
        st.markdown("**How to read RWA labels**")
        for line in rwa_public_definitions():
            label, definition = line.split(": ", 1)
            st.markdown(f"- `{label}`: {definition}")
        st.markdown("")
        st.markdown("**Supported public reference categories**")
        for category in supported_public_categories():
            family_text = f" Part of: {category.family_name}." if category.family_name else ""
            st.markdown(f"- `{category.public_category_name}`: {category.short_definition}{family_text}")
        st.markdown("")
        st.markdown("**Override Decisions**")
        st.markdown("- `Token`: local watchboard token symbol.")
        st.markdown("- `CoinGecko ID`: chosen CoinGecko asset identifier used for token-level market data.")
        st.markdown("- `Reason`: why this manual mapping override exists.")
        st.markdown("")
        st.markdown("**Latest Audit Sample / Ambiguous CoinGecko Matches**")
        st.markdown("- `Candidate Count`: how many CoinGecko candidates matched this symbol before disambiguation.")
        st.markdown("- `Match Status`: whether the token matched cleanly, remained ambiguous, or had no symbol candidate.")
        st.markdown("- `Price (USD)`, `24h Volume`, `Market Cap`: token-level CoinGecko market fields used to understand business priority, not venue-specific perp metrics.")
        st.markdown("- `Market Data As Of`: timestamp of the token-level market snapshot returned by CoinGecko.")
        st.markdown("")
        st.markdown("**Listing Coverage Audit**")
        st.markdown("- `Dropped Stage`: pipeline stage where the token disappeared or was excluded.")
        st.markdown("- `Audit Note`: short human explanation of the drop reason.")
        st.markdown("")
        st.markdown("**RWA Review Queue**")
        st.markdown("- `RWA`: current public-facing label state, such as `RWA Core`, `RWA Related`, `Non-RWA`, or `Review Pending`.")
        st.markdown("- `Reference category`: the public-facing business category, such as `Tokenized Commodities`, `Tokenized Treasury Bills (T-Bills)`, or `RWA Protocol`.")
        st.markdown("- `Why this label`: one plain-language sentence that explains the current label or why the token remains in review.")
        st.markdown("- `Evidence summary`: short business-friendly bullets that summarize the evidence without exposing raw internal JSON.")
        st.markdown("- `Price (USD)`, `24h Volume`, `Market Cap`: token-level market fields used only to prioritize review order and business impact.")


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


def render_overview(snapshot_date: str, rwa_filter: str):
    st.header("Overview")
    route_caption("overview", snapshot_date, rwa=rwa_filter)

    summary = wq.snapshot_summary(snapshot_date)
    market_data_as_of = wq.snapshot_market_data_as_of(snapshot_date)
    recent_df, recent_count, used_fallback = wq.recent_listings(snapshot_date, limit=12, lookback_hours=LOOKBACK_HOURS)
    hot_new_df = wq.leaderboard("hot_new", snapshot_date, limit=12)
    top_volume_df = wq.leaderboard("top_volume", snapshot_date, limit=12)
    top_movers_df = wq.top_movers(snapshot_date, limit=12)
    rwa_df = wq.token_rwa_labels(snapshot_date)
    rwa_history_missing = rwa_df.empty

    recent_df = apply_rwa_filter(merge_rwa_labels(recent_df, rwa_df), rwa_filter)
    hot_new_df = apply_rwa_filter(merge_rwa_labels(hot_new_df, rwa_df), rwa_filter)
    top_volume_df = apply_rwa_filter(merge_rwa_labels(top_volume_df, rwa_df), rwa_filter)
    top_movers_df = apply_rwa_filter(merge_rwa_labels(top_movers_df, rwa_df), rwa_filter)

    metric_cols = st.columns(4)
    metric_cols[0].metric("New Listings 24h", len(recent_df))
    metric_cols[1].metric("Hot New Tokens", len(hot_new_df))
    metric_cols[2].metric("Tracked Tokens", summary["tracked_tokens"])
    metric_cols[3].metric("Monitored Venues", summary["monitored_venues"])
    st.caption(f"RWA Filter: `{rwa_filter}`")
    st.caption("RWA here includes both tokenized assets and the protocols that support them.")
    st.caption("Where relevant, the product uses public reference categories such as `Tokenized Assets` and `RWA Protocol` in its explanations.")

    if used_fallback:
        st.info("No listings fall inside the last 24 hours for the selected snapshot. Showing the most recent known listings instead.")

    st.subheader("Listing View")
    st.dataframe(prepare_recent_listings_table(recent_df, historical_rwa_unavailable=rwa_history_missing), width="stretch", hide_index=True)

    st.subheader("Token Market View")
    market_note = TOKEN_MARKET_SCOPE_NOTE
    if market_data_as_of:
        market_note += f" Market data as of: {format_sgt_datetime(market_data_as_of)}."
    st.caption(market_note)
    render_token_market_missing_note(rwa_history_missing=rwa_history_missing)

    left, right = st.columns(2)
    with left:
        st.markdown("**Hot New Tokens**")
        st.dataframe(prepare_leaderboard_table(hot_new_df, historical_rwa_unavailable=rwa_history_missing), width="stretch", hide_index=True)

    with right:
        st.markdown("**Top Volume 24h**")
        st.dataframe(prepare_leaderboard_table(top_volume_df, historical_rwa_unavailable=rwa_history_missing), width="stretch", hide_index=True)
        st.markdown("**Top Movers 24h**")
        st.dataframe(prepare_leaderboard_table(top_movers_df, historical_rwa_unavailable=rwa_history_missing), width="stretch", hide_index=True)


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
    rwa_badge_text = rwa_badge(profile.get("rwa_label"))
    if rwa_badge_text:
        st.subheader("RWA View")
        st.caption("RWA here includes both tokenized assets and the protocols that support them.")
        reference_category = public_reference_category(profile) or "n/a"
        reference_definition = public_reference_definition(profile)
        evidence_cols = st.columns(2)
        evidence_cols[0].metric("RWA Status", rwa_badge_text)
        evidence_cols[1].metric("Reference category", reference_category)
        if reference_definition:
            st.caption(reference_definition)
        with st.expander("How to read these RWA labels", expanded=False):
            for line in rwa_public_definitions():
                label, definition = line.split(": ", 1)
                st.markdown(f"- `{label}`: {definition}")
            st.markdown("")
            st.markdown("**Public reference categories used in this product**")
            for category in supported_public_categories():
                family_text = f" Part of: {category.family_name}." if category.family_name else ""
                st.markdown(f"- `{category.public_category_name}`: {category.short_definition}{family_text}")
        st.markdown("**Why this label**")
        st.info(rwa_reason_text(profile))
        st.markdown("**Evidence summary**")
        for line in rwa_evidence_summary_lines(profile):
            st.markdown(f"- {line}")

    st.subheader("Listing Coverage")
    st.dataframe(prepare_token_coverage_table(coverage_df), width="stretch", hide_index=True)

    st.subheader("Venue Perp View")
    st.caption(VENUE_PERP_SCOPE_NOTE)
    render_venue_perp_column_guide()
    if venue_metrics_df.empty:
        st.info("No venue-level perp metrics are available for this token in the selected snapshot.")
    else:
        if not safe_series(venue_metrics_df, "fetch_status").replace("", pd.NA).notna().any():
            st.info("This snapshot predates fetch-status and freshness tracking for venue ticker ingestion. Those columns below are display fallbacks.")
        stale_rows = int((venue_metrics_df["data_freshness"] == "stale_fallback").sum()) if "data_freshness" in venue_metrics_df.columns else 0
        if stale_rows:
            st.warning(f"{stale_rows} venue metric row(s) are using stale fallback data from the previous successful ticker snapshot.")
        st.dataframe(prepare_token_venue_metrics_table(venue_metrics_df), width="stretch", hide_index=True)

    st.subheader("Venue Expansion Over Time")
    if expansion_df.empty:
        st.info("No historical venue coverage data is available for this token yet.")
    else:
        chart_df = expansion_df.set_index("snapshot_date")[["venue_count"]]
        st.line_chart(chart_df, width="stretch")
        st.dataframe(expansion_df, width="stretch", hide_index=True)


def render_venue_page(snapshot_date: str, selected_venue: str, rwa_filter: str):
    st.header("Venue View")
    route_caption("venue", snapshot_date, venue=selected_venue, rwa=rwa_filter)

    listings_df = wq.venue_listings(selected_venue, snapshot_date)
    recent_additions_df = wq.venue_recent_additions(selected_venue, snapshot_date, limit=20)
    venue_ticker_df = wq.venue_ticker_metrics(selected_venue, snapshot_date)
    rwa_df = wq.token_rwa_labels(snapshot_date)
    rwa_history_missing = rwa_df.empty

    listings_df = apply_rwa_filter(merge_rwa_labels(listings_df, rwa_df), rwa_filter)
    recent_additions_df = apply_rwa_filter(merge_rwa_labels(recent_additions_df, rwa_df), rwa_filter)
    venue_ticker_df = apply_rwa_filter(merge_rwa_labels(venue_ticker_df, rwa_df, token_col="base_token"), rwa_filter)

    metric_cols = st.columns(4)
    metric_cols[0].metric("Tracked Listings", len(listings_df))
    metric_cols[1].metric("Tracked Tokens", listings_df["token"].nunique() if not listings_df.empty else 0)
    metric_cols[2].metric("Recent Additions", len(recent_additions_df))
    metric_cols[3].metric("Venue Ticker Rows", len(venue_ticker_df))
    st.caption(f"RWA Filter: `{rwa_filter}`")
    st.caption("RWA here includes both tokenized assets and the protocols that support them.")
    st.caption("Where relevant, the product uses public reference categories such as `Tokenized Assets` and `RWA Protocol` in its explanations.")

    st.subheader("Venue Listing View")
    st.markdown("**Recent Additions**")
    if rwa_history_missing:
        st.info("This historical snapshot does not have persisted RWA labels yet. Venue listing tables below show `Not labeled` display fallbacks.")
    st.dataframe(prepare_venue_listings_table(recent_additions_df, historical_rwa_unavailable=rwa_history_missing), width="stretch", hide_index=True)

    st.markdown("**Per-venue Listings**")
    token_filter = st.text_input("Filter tokens on this venue", value="")
    filtered_df = listings_df
    if token_filter:
        token_filter_upper = token_filter.upper()
        filtered_df = filtered_df[
            filtered_df["token"].str.upper().str.contains(token_filter_upper, na=False)
            | filtered_df["symbol_raw"].str.upper().str.contains(token_filter_upper, na=False)
        ]
    st.dataframe(prepare_venue_listings_table(filtered_df, historical_rwa_unavailable=rwa_history_missing), width="stretch", hide_index=True)

    st.subheader("Venue Perp View")
    st.caption(VENUE_PERP_SCOPE_NOTE)
    render_venue_perp_column_guide()
    if venue_ticker_df.empty:
        st.info("No venue ticker metrics are available for this venue in the selected snapshot.")
    else:
        if not safe_series(venue_ticker_df, "fetch_status").replace("", pd.NA).notna().any():
            st.info("This snapshot predates fetch-status and freshness tracking for venue ticker ingestion. Those columns below are display fallbacks.")
        stale_rows = int((venue_ticker_df["data_freshness"] == "stale_fallback").sum()) if "data_freshness" in venue_ticker_df.columns else 0
        if stale_rows:
            st.warning(f"{stale_rows} ticker row(s) are using stale fallback data because the latest venue fetch did not fully succeed.")
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


def render_history_page(snapshot_date: str, history_token: str, rwa_filter: str):
    st.header("History / Diff")
    route_caption("history", snapshot_date, token=history_token, rwa=rwa_filter)

    changes_df = wq.daily_change_counts()
    previous_snapshot = wq.previous_snapshot_date(snapshot_date)
    previous_snapshot, added_df, removed_df = wq.snapshot_diff(snapshot_date)
    expansion_summary_df = wq.token_expansion_summary(limit=50)
    rwa_df = wq.token_rwa_labels(snapshot_date)
    rwa_history_missing = rwa_df.empty

    added_df = apply_rwa_filter(merge_rwa_labels(added_df, rwa_df), rwa_filter)
    removed_df = apply_rwa_filter(merge_rwa_labels(removed_df, rwa_df), rwa_filter)
    expansion_summary_df = apply_rwa_filter(merge_rwa_labels(expansion_summary_df, rwa_df), rwa_filter)
    st.caption(f"RWA Filter: `{rwa_filter}`")
    st.caption("RWA here includes both tokenized assets and the protocols that support them.")
    st.caption("Where relevant, the product uses public reference categories such as `Tokenized Assets` and `RWA Protocol` in its explanations.")
    if rwa_history_missing:
        st.info("This historical snapshot does not have persisted RWA labels yet. History tables below show `Not labeled` display fallbacks for RWA columns.")

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
            st.dataframe(prepare_venue_listings_table(added_df, historical_rwa_unavailable=rwa_history_missing), width="stretch", hide_index=True)
        with right:
            st.markdown("**Removed Since Previous Snapshot**")
            st.dataframe(prepare_venue_listings_table(removed_df, historical_rwa_unavailable=rwa_history_missing), width="stretch", hide_index=True)

    st.subheader("Venue Expansion Summary")
    st.dataframe(prepare_expansion_summary_table(expansion_summary_df), width="stretch", hide_index=True)

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
    render_quality_column_glossary()

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

    st.subheader("RWA Review Queue")
    review_queue_df = wq.rwa_review_queue(snapshot_date, limit=200)
    if review_queue_df.empty:
        if snapshot_date < "2026-04-15":
            st.info("This historical snapshot predates persisted RWA labels, so no RWA review queue exists for that date.")
        else:
            st.info("No review-pending RWA rows are available for the selected snapshot.")
    else:
        st.caption("Highest-priority `review_pending` tokens, ranked by 24h volume, market cap, then signal evidence.")
        public_review_queue_df = prepare_rwa_review_queue_table(review_queue_df)
        csv_payload = public_review_queue_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download RWA Review Queue CSV",
            data=csv_payload,
            file_name=f"token_rwa_review_queue_{snapshot_date}.csv",
            mime="text/csv",
            width="content",
        )
        st.dataframe(public_review_queue_df, width="stretch", hide_index=True)


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

    selected_rwa_filter = "All"
    if selected_page_key in {"overview", "venue", "history"}:
        initial_rwa_filter = query_params.get("rwa", "All")
        if initial_rwa_filter not in RWA_FILTER_OPTIONS:
            initial_rwa_filter = "All"
        selected_rwa_filter = st.sidebar.radio(
            "RWA Filter",
            options=RWA_FILTER_OPTIONS,
            index=RWA_FILTER_OPTIONS.index(initial_rwa_filter),
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

    write_query_params(
        page=selected_page_key,
        snapshot=selected_snapshot,
        token=selected_token,
        venue=selected_venue,
        rwa=selected_rwa_filter if selected_page_key in {"overview", "venue", "history"} else "",
    )

    if selected_page_key == "overview":
        render_overview(selected_snapshot, selected_rwa_filter)
    elif selected_page_key == "token":
        if not selected_token:
            st.warning("No token is available for the selected snapshot.")
        else:
            render_token_page(selected_snapshot, selected_token)
    elif selected_page_key == "venue":
        if not selected_venue:
            st.warning("No venue is available for the selected snapshot.")
        else:
            render_venue_page(selected_snapshot, selected_venue, selected_rwa_filter)
    elif selected_page_key == "history":
        render_history_page(selected_snapshot, selected_token, selected_rwa_filter)
    elif selected_page_key == "quality":
        render_quality_page(selected_snapshot)


if __name__ == "__main__":
    main()
