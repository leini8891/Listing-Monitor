from __future__ import annotations

"""
Curated CoinGecko override map for high-confidence ambiguous tokens.

Purpose:
  - Keep manual token -> CoinGecko ID decisions explicit.
  - Make the override layer easy to extend without touching the matching core.
  - Reserve this for high-confidence cases where symbol-only matching is ambiguous.
"""

COINGECKO_OVERRIDE_MAP = {
    "BTC": {
        "coingecko_id": "bitcoin",
        "reason": "Canonical BTC asset across all monitored perp venues.",
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
    "PRL": {
        "coingecko_id": "perle",
        "reason": "Venue perp prices align with CoinGecko Perle; the other PRL candidate is not price-compatible.",
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
