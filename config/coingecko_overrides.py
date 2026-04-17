from __future__ import annotations

"""
Curated CoinGecko override map for high-confidence ambiguous tokens.

Purpose:
  - Keep manual token -> CoinGecko ID decisions explicit.
  - Make the override layer easy to extend without touching the matching core.
  - Reserve this for high-confidence cases where symbol-only matching is ambiguous.
"""

COINGECKO_OVERRIDE_MAP = {
    "ADA": {
        "coingecko_id": "cardano",
        "reason": "Canonical ADA asset across monitored perp venues; dominant exact-symbol candidate with price-compatible major-market mapping.",
    },
    "ARB": {
        "coingecko_id": "arbitrum",
        "reason": "Canonical ARB asset across monitored perp venues; exact-symbol candidate is dominant and price-compatible.",
    },
    "AVAX": {
        "coingecko_id": "avalanche-2",
        "reason": "Canonical AVAX asset across monitored perp venues; exact-symbol candidate Avalanche clearly dominates wrapper variants.",
    },
    "BTC": {
        "coingecko_id": "bitcoin",
        "reason": "Canonical BTC asset across all monitored perp venues.",
    },
    "BCH": {
        "coingecko_id": "bitcoin-cash",
        "reason": "Canonical BCH asset across monitored perp venues; exact-symbol candidate Bitcoin Cash clearly dominates wrapper variants.",
    },
    "BNB": {
        "coingecko_id": "binancecoin",
        "reason": "Canonical BNB asset across monitored perp venues; exact-symbol candidate is the dominant non-wrapper mapping.",
    },
    "CFG": {
        "coingecko_id": "centrifuge",
        "reason": "CFG is the canonical Centrifuge token; this removes an avoidable ambiguity for core RWA labeling.",
    },
    "CRV": {
        "coingecko_id": "curve-dao-token",
        "reason": "Canonical CRV asset across monitored perp venues; exact-symbol candidate Curve DAO matches venue pricing and only meaningful same-symbol alternatives are wrappers.",
    },
    "BASED": {
        "coingecko_id": "based-one",
        "reason": "Venue perp prices align closely with CoinGecko based-one; other BASED candidates are orders of magnitude away.",
    },
    "BSB": {
        "coingecko_id": "block-street",
        "reason": "Venue perp prices align with CoinGecko block-street; the other BSB candidate is micro-priced and not compatible.",
    },
    "EDGE": {
        "coingecko_id": "edgex",
        "reason": "Venue perp prices align closely with CoinGecko edgeX; other EDGE candidates price far away.",
    },
    "ETH": {
        "coingecko_id": "ethereum",
        "reason": "Canonical ETH asset across monitored perp venues; exact-symbol candidate Ethereum has overwhelming market-cap dominance.",
    },
    "FIL": {
        "coingecko_id": "filecoin",
        "reason": "Canonical FIL asset across monitored perp venues; exact-symbol candidate Filecoin clearly dominates wrapper variants.",
    },
    "HYPE": {
        "coingecko_id": "hyperliquid",
        "reason": "Canonical HYPE asset across monitored perp venues; exact-symbol candidate Hyperliquid matches the multi-venue perp footprint and dominates wrapper-like or minor alternatives.",
    },
    "USDC": {
        "coingecko_id": "usd-coin",
        "reason": "Canonical Circle USD Coin mapping for mainstream stablecoin exclusion handling.",
    },
    "USDT": {
        "coingecko_id": "tether",
        "reason": "Canonical Tether mapping for mainstream stablecoin exclusion handling.",
    },
    "DAI": {
        "coingecko_id": "dai",
        "reason": "Canonical DAI mapping for mainstream stablecoin exclusion handling.",
    },
    "FDUSD": {
        "coingecko_id": "first-digital-usd",
        "reason": "Canonical First Digital USD mapping for mainstream stablecoin exclusion handling.",
    },
    "PAXG": {
        "coingecko_id": "pax-gold",
        "reason": "Canonical PAX Gold mapping for tokenized gold classification and audit coverage.",
    },
    "DOT": {
        "coingecko_id": "polkadot",
        "reason": "Canonical DOT asset across monitored perp venues; exact-symbol candidate Polkadot is the dominant non-wrapper mapping.",
    },
    "LTC": {
        "coingecko_id": "litecoin",
        "reason": "Canonical LTC asset across monitored perp venues; exact-symbol candidate Litecoin is the obvious non-wrapper mapping.",
    },
    "NEAR": {
        "coingecko_id": "near",
        "reason": "Canonical NEAR asset across monitored perp venues; exact-symbol candidate NEAR Protocol clearly dominates wrapper variants.",
    },
    "PRL": {
        "coingecko_id": "perle",
        "reason": "Venue perp prices align with CoinGecko Perle; the other PRL candidate is not price-compatible.",
    },
    "SOL": {
        "coingecko_id": "solana",
        "reason": "Canonical SOL asset across monitored perp venues; exact-symbol candidate Solana has overwhelming market-cap dominance.",
    },
    "TAO": {
        "coingecko_id": "bittensor",
        "reason": "Canonical TAO asset across monitored perp venues; exact-symbol candidate Bittensor is dominant and price-compatible.",
    },
    "TIA": {
        "coingecko_id": "celestia",
        "reason": "Canonical TIA asset across monitored perp venues; exact-symbol candidate Celestia is uniquely price-compatible at scale.",
    },
    "TRU": {
        "coingecko_id": "truefi",
        "reason": "TRU is the canonical TrueFi token; this removes ambiguity for related RWA credit protocol labeling.",
    },
    "UNI": {
        "coingecko_id": "uniswap",
        "reason": "Canonical UNI asset across monitored perp venues; exact-symbol candidate Uniswap is strongly favored by search rank, market-cap dominance, and multi-venue price alignment.",
    },
    "XRP": {
        "coingecko_id": "ripple",
        "reason": "Canonical XRP asset across monitored perp venues; exact-symbol candidate Ripple/XRP has dominant market-cap support.",
    },
    "ZEC": {
        "coingecko_id": "zcash",
        "reason": "Canonical ZEC asset across monitored perp venues; exact-symbol candidate Zcash clearly dominates wrapper alternatives.",
    },
}


def override_rows() -> list[dict]:
    rows = []
    for token in sorted(COINGECKO_OVERRIDE_MAP):
        entry = COINGECKO_OVERRIDE_MAP[token]
        rows.append(
            {
                "token": token,
                "coingecko_id": entry.get("coingecko_id", ""),
                "reason": entry.get("reason", ""),
            }
        )
    return rows
