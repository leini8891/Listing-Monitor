from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicCategory:
    key: str
    public_category_name: str
    short_definition: str
    display_priority: int = 100
    family_name: str = ""


PUBLIC_CATEGORY_DICTIONARY: dict[str, PublicCategory] = {
    "rwa": PublicCategory(
        key="rwa",
        public_category_name="RWA",
        short_definition="A broad category for real-world assets brought on-chain, including both tokenized assets and the protocols that support them.",
        display_priority=10,
    ),
    "rwa-protocol": PublicCategory(
        key="rwa-protocol",
        public_category_name="RWA Protocol",
        short_definition="Protocols and infrastructure that help real-world assets get issued, managed, or traded on-chain.",
        display_priority=20,
        family_name="RWA",
    ),
    "tokenized-assets": PublicCategory(
        key="tokenized-assets",
        public_category_name="Tokenized Assets",
        short_definition="Blockchain-based representations of real-world or traditional financial assets, such as treasuries, gold, stocks, funds, or real estate.",
        display_priority=30,
        family_name="RWA",
    ),
    "tokenized-treasury-bills": PublicCategory(
        key="tokenized-treasury-bills",
        public_category_name="Tokenized Treasury Bills (T-Bills)",
        short_definition="On-chain products backed by short-term government debt or treasury exposure.",
        display_priority=31,
        family_name="Tokenized Assets",
    ),
    "tokenized-commodities": PublicCategory(
        key="tokenized-commodities",
        public_category_name="Tokenized Commodities",
        short_definition="On-chain representations of commodity exposure, such as gold or other physical assets.",
        display_priority=32,
        family_name="Tokenized Assets",
    ),
    "tokenized-stock": PublicCategory(
        key="tokenized-stock",
        public_category_name="Tokenized Stock",
        short_definition="On-chain representations of publicly traded equity exposure.",
        display_priority=33,
        family_name="Tokenized Assets",
    ),
    "tokenized-etfs": PublicCategory(
        key="tokenized-etfs",
        public_category_name="Tokenized Exchange-Traded Funds (ETFs)",
        short_definition="On-chain representations of exchange-traded fund exposure.",
        display_priority=34,
        family_name="Tokenized Assets",
    ),
    "tokenized-real-estate": PublicCategory(
        key="tokenized-real-estate",
        public_category_name="Tokenized Real Estate",
        short_definition="On-chain representations of real estate ownership or real estate-linked exposure.",
        display_priority=35,
        family_name="Tokenized Assets",
    ),
    "yield-bearing-stablecoins": PublicCategory(
        key="yield-bearing-stablecoins",
        public_category_name="Yield-Bearing Stablecoins",
        short_definition="Stable-value tokens that pass through yield from reserve assets, often linked to treasury-style exposure.",
        display_priority=36,
        family_name="Tokenized Assets",
    ),
    "asset-backed-stablecoins": PublicCategory(
        key="asset-backed-stablecoins",
        public_category_name="Asset-Backed Stablecoins",
        short_definition="Stable-value tokens linked to off-chain reserve assets such as commodities or other financial instruments.",
        display_priority=37,
        family_name="Tokenized Assets",
    ),
    "institutional-credit": PublicCategory(
        key="institutional-credit",
        public_category_name="Institutional Credit & Financing",
        short_definition="Protocols or products connected to private credit, lending, or financing tied to real-world assets.",
        display_priority=38,
        family_name="RWA Protocol",
    ),
    "outside-rwa-reference-categories": PublicCategory(
        key="outside-rwa-reference-categories",
        public_category_name="Outside RWA Reference Categories",
        short_definition="This token does not currently match the RWA, RWA Protocol, or Tokenized Assets reference categories used by this product.",
        display_priority=90,
    ),
    "under-review-rwa-candidate": PublicCategory(
        key="under-review-rwa-candidate",
        public_category_name="RWA Candidate (Under Review)",
        short_definition="This token sits close enough to the RWA theme to review, but the available evidence is not yet strong enough for a final public category.",
        display_priority=95,
        family_name="RWA",
    ),
    "unresolved-identity": PublicCategory(
        key="unresolved-identity",
        public_category_name="Unresolved Identity",
        short_definition="The token's market identity is not confirmed yet, so it cannot be mapped safely to a public RWA reference category.",
        display_priority=99,
    ),
}


_CATEGORY_ALIASES = {
    "rwa-protocol": "rwa-protocol",
    "rwa-financing-protocol": "rwa-protocol",
    "rwa-chain-infrastructure": "rwa-protocol",
    "regulated-tokenization-infrastructure": "rwa-protocol",
    "institutional-credit": "institutional-credit",
    "tokenized-gold": "tokenized-commodities",
    "tokenized-commodity": "tokenized-commodities",
    "tokenized-treasury": "tokenized-treasury-bills",
    "tokenized-stock": "tokenized-stock",
    "tokenized-etf": "tokenized-etfs",
    "tokenized-etfs": "tokenized-etfs",
    "tokenized-real-estate": "tokenized-real-estate",
    "yield-bearing-stablecoin": "yield-bearing-stablecoins",
    "asset-backed-stablecoin": "asset-backed-stablecoins",
    "excluded-mainstream-stablecoin": "outside-rwa-reference-categories",
    "broad-rwa-candidate-unresolved": "under-review-rwa-candidate",
    "mixed-rwa-signals": "under-review-rwa-candidate",
    "mixed-keyword-signals": "under-review-rwa-candidate",
    "ambiguous-rwa-language": "under-review-rwa-candidate",
    "unresolved-identity": "unresolved-identity",
    "missing-detail-cache": "under-review-rwa-candidate",
}


def resolve_public_category_key(rwa_label: str = "", rwa_category: str = "", evidence_type: str = "") -> str:
    normalized_category = (rwa_category or "").strip()
    normalized_label = (rwa_label or "").strip()
    normalized_evidence = (evidence_type or "").strip()

    if normalized_category:
        mapped = _CATEGORY_ALIASES.get(normalized_category)
        if mapped:
            return mapped

    if normalized_label == "core":
        return "tokenized-assets"
    if normalized_label == "related":
        return "rwa-protocol"
    if normalized_label == "review_pending":
        if normalized_evidence == "missing_coingecko_id":
            return "unresolved-identity"
        return "under-review-rwa-candidate"
    if normalized_label == "non_rwa":
        return "outside-rwa-reference-categories"
    return ""


def resolve_public_category(rwa_label: str = "", rwa_category: str = "", evidence_type: str = "") -> PublicCategory | None:
    key = resolve_public_category_key(rwa_label=rwa_label, rwa_category=rwa_category, evidence_type=evidence_type)
    if not key:
        return None
    return PUBLIC_CATEGORY_DICTIONARY.get(key)


def top_level_reference_definitions() -> list[PublicCategory]:
    ordered_keys = ["rwa", "rwa-protocol", "tokenized-assets"]
    return [PUBLIC_CATEGORY_DICTIONARY[key] for key in ordered_keys]


def supported_public_categories() -> list[PublicCategory]:
    excluded = {"outside-rwa-reference-categories", "under-review-rwa-candidate", "unresolved-identity"}
    return sorted(
        [category for key, category in PUBLIC_CATEGORY_DICTIONARY.items() if key not in excluded],
        key=lambda category: (category.display_priority, category.public_category_name),
    )
