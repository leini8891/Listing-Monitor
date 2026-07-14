from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.transform.label_rwa_tokens import (  # noqa: E402
    AllowlistEntry,
    build_review_queue,
    classify_token_row,
    validate_output_rows,
)


class LabelRwaTokensTest(unittest.TestCase):
    def setUp(self):
        self.labeled_at = "2026-04-15T00:00:00+00:00"
        self.empty_broad_universe = {"coin_ids": []}

    def test_manual_override_priority_wins_over_cache_signals(self):
        row = {"token": "USDT", "coingecko_id": "tether"}
        manual_overrides = {
            "tether": AllowlistEntry(
                coingecko_id="tether",
                rwa_label="non_rwa",
                rwa_category="excluded-mainstream-stablecoin",
                protocol="Tether",
                force_override=True,
                notes="Explicit exclusion for mainstream stablecoin.",
            )
        }
        cache = {
            "coins": {
                "tether": {
                    "name": "Tether",
                    "description": "A token focused on real-world assets and tokenized treasuries.",
                    "categories": ["Real World Assets (RWA)"],
                    "fetched_at": self.labeled_at,
                }
            }
        }

        result = classify_token_row(
            row=row,
            manual_overrides=manual_overrides,
            seed_allowlist={},
            cache=cache,
            broad_universe_cache=self.empty_broad_universe,
            labeled_at=self.labeled_at,
        )

        self.assertEqual(result["rwa_label"], "non_rwa")
        self.assertEqual(result["label_source"], "manual_override")

    def test_seed_allowlist_known_core_token(self):
        row = {"token": "ONDO", "coingecko_id": "ondo-finance"}
        seed_allowlist = {
            "ondo-finance": AllowlistEntry(
                coingecko_id="ondo-finance",
                rwa_label="core",
                rwa_category="rwa-protocol",
                protocol="Ondo",
                force_override=False,
                notes="Seed allowlist core token.",
            )
        }

        result = classify_token_row(
            row=row,
            manual_overrides={},
            seed_allowlist=seed_allowlist,
            cache={"coins": {}},
            broad_universe_cache=self.empty_broad_universe,
            labeled_at=self.labeled_at,
        )

        self.assertEqual(result["rwa_label"], "core")
        self.assertEqual(result["label_source"], "seed_allowlist")

    def test_seed_allowlist_known_related_token(self):
        row = {"token": "USDM", "coingecko_id": "mountain-protocol-usdm"}
        seed_allowlist = {
            "mountain-protocol-usdm": AllowlistEntry(
                coingecko_id="mountain-protocol-usdm",
                rwa_label="related",
                rwa_category="yield-bearing-stablecoin",
                protocol="Mountain Protocol",
                force_override=False,
                notes="Yield-bearing stablecoin is related, not core.",
            )
        }

        result = classify_token_row(
            row=row,
            manual_overrides={},
            seed_allowlist=seed_allowlist,
            cache={"coins": {}},
            broad_universe_cache=self.empty_broad_universe,
            labeled_at=self.labeled_at,
        )

        self.assertEqual(result["rwa_label"], "related")
        self.assertEqual(result["rwa_category"], "yield-bearing-stablecoin")

    def test_unknown_token_fallback_non_rwa(self):
        row = {"token": "PEPE", "coingecko_id": "pepe"}
        cache = {
            "coins": {
                "pepe": {
                    "name": "Pepe",
                    "description": "A meme token with community-driven culture.",
                    "categories": ["Meme"],
                    "fetched_at": self.labeled_at,
                }
            }
        }

        result = classify_token_row(
            row=row,
            manual_overrides={},
            seed_allowlist={},
            cache=cache,
            broad_universe_cache={"coin_ids": []},
            labeled_at=self.labeled_at,
        )

        self.assertEqual(result["rwa_label"], "non_rwa")
        self.assertEqual(result["label_source"], "conservative_keyword_fallback")

    def test_missing_coingecko_id_stays_review_pending(self):
        row = {"token": "QQQ", "coingecko_id": ""}
        result = classify_token_row(
            row=row,
            manual_overrides={},
            seed_allowlist={},
            cache={"coins": {}},
            broad_universe_cache=self.empty_broad_universe,
            labeled_at=self.labeled_at,
        )

        self.assertEqual(result["rwa_label"], "review_pending")
        self.assertEqual(result["evidence_type"], "missing_coingecko_id")

    def test_stable_id_outside_broad_rwa_universe_becomes_non_rwa(self):
        row = {"token": "BTC", "coingecko_id": "bitcoin"}

        result = classify_token_row(
            row=row,
            manual_overrides={},
            seed_allowlist={},
            cache={"coins": {}},
            broad_universe_cache={
                "coin_ids": ["ondo-finance", "pax-gold"],
                "target_categories": [
                    {"requested_name": "Real World Assets (RWA)", "matched_name": "Real World Assets (RWA)", "category_id": "real-world-assets-rwa"}
                ],
                "fetched_at": self.labeled_at,
            },
            labeled_at=self.labeled_at,
        )

        self.assertEqual(result["rwa_label"], "non_rwa")
        self.assertEqual(result["label_source"], "coingecko_rwa_universe_gate")
        self.assertEqual(result["evidence_type"], "not_in_broad_rwa_universe")

    def test_broad_rwa_candidate_with_missing_detail_stays_review_pending(self):
        row = {"token": "BARD", "coingecko_id": "lombard-protocol"}

        result = classify_token_row(
            row=row,
            manual_overrides={},
            seed_allowlist={},
            cache={"coins": {}},
            broad_universe_cache={
                "coin_ids": ["lombard-protocol"],
                "target_categories": [
                    {"requested_name": "RWA Protocol", "matched_name": "RWA Protocol", "category_id": "rwa-protocol"}
                ],
                "fetched_at": self.labeled_at,
            },
            labeled_at=self.labeled_at,
        )

        self.assertEqual(result["rwa_label"], "review_pending")
        self.assertEqual(result["evidence_type"], "missing_coingecko_detail")

    def test_broad_rwa_candidate_without_refinement_stays_review_pending(self):
        row = {"token": "MYST", "coingecko_id": "myst-token"}

        result = classify_token_row(
            row=row,
            manual_overrides={},
            seed_allowlist={},
            cache={
                "coins": {
                    "myst-token": {
                        "name": "Myst Token",
                        "description": "Infrastructure token.",
                        "categories": ["Layer 1"],
                        "fetched_at": self.labeled_at,
                    }
                }
            },
            broad_universe_cache={
                "coin_ids": ["myst-token"],
                "target_categories": [
                    {"requested_name": "RWA Protocol", "matched_name": "RWA Protocol", "category_id": "rwa-protocol"}
                ],
                "fetched_at": self.labeled_at,
            },
            labeled_at=self.labeled_at,
        )

        self.assertEqual(result["rwa_label"], "review_pending")
        self.assertEqual(result["evidence_type"], "broad_rwa_candidate_unresolved")

    def test_output_data_quality_constraints(self):
        rows = [
            {
                "token": "ONDO",
                "coingecko_id": "ondo-finance",
                "rwa_label": "core",
                "rwa_category": "rwa-protocol",
                "protocol": "Ondo",
                "confidence": "0.95",
                "evidence_type": "seed_allowlist",
                "evidence_detail_json": json.dumps({"notes": "seed"}),
                "label_source": "seed_allowlist",
                "labeled_at": self.labeled_at,
            },
            {
                "token": "QQQ",
                "coingecko_id": "",
                "rwa_label": "review_pending",
                "rwa_category": "unresolved-identity",
                "protocol": "",
                "confidence": "0.15",
                "evidence_type": "missing_coingecko_id",
                "evidence_detail_json": json.dumps({"reason": "missing id"}),
                "label_source": "conservative_keyword_fallback",
                "labeled_at": self.labeled_at,
            },
        ]

        validate_output_rows(rows)

    def test_review_queue_prioritizes_volume_then_market_cap_then_signal(self):
        output_rows = [
            {
                "token": "AAA",
                "coingecko_id": "aaa",
                "rwa_label": "review_pending",
                "rwa_category": "missing-detail-cache",
                "protocol": "",
                "confidence": "0.2",
                "evidence_type": "missing_coingecko_detail",
                "evidence_detail_json": json.dumps({"reason": "missing"}),
                "label_source": "conservative_keyword_fallback",
                "labeled_at": self.labeled_at,
            },
            {
                "token": "BBB",
                "coingecko_id": "bbb",
                "rwa_label": "review_pending",
                "rwa_category": "ambiguous-rwa-language",
                "protocol": "",
                "confidence": "0.35",
                "evidence_type": "keyword_ambiguous",
                "evidence_detail_json": json.dumps({"matched_terms": ["rwa"]}),
                "label_source": "conservative_keyword_fallback",
                "labeled_at": self.labeled_at,
            },
            {
                "token": "CCC",
                "coingecko_id": "ccc",
                "rwa_label": "review_pending",
                "rwa_category": "missing-detail-cache",
                "protocol": "",
                "confidence": "0.2",
                "evidence_type": "missing_coingecko_detail",
                "evidence_detail_json": json.dumps({"reason": "missing"}),
                "label_source": "conservative_keyword_fallback",
                "labeled_at": self.labeled_at,
            },
        ]
        token_market_rows = [
            {"token": "AAA", "current_price_usd": "1", "volume_24h_usd": "100", "market_cap_usd": "1000"},
            {"token": "BBB", "current_price_usd": "1", "volume_24h_usd": "100", "market_cap_usd": "1000"},
            {"token": "CCC", "current_price_usd": "1", "volume_24h_usd": "200", "market_cap_usd": "100"},
        ]

        queue = build_review_queue(output_rows, token_market_rows)

        self.assertEqual([row["token"] for row in queue], ["CCC", "BBB", "AAA"])
        self.assertEqual(queue[1]["has_signal_evidence"], "true")


if __name__ == "__main__":
    unittest.main()
