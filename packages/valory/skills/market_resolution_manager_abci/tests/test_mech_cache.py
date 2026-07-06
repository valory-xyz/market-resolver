# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2026 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""Pure-function tests for the mech_cache helpers."""

import json

from packages.valory.skills.market_resolution_manager_abci import mech_cache

PREFIX = "market_resolver/"


class TestCacheKey:
    """Tests for ``cache_key`` and ``list_prefix``."""

    def test_key_layout(self) -> None:
        """Key is <prefix><lower(safe)>/<market_id>/<nonce>."""
        key = mech_cache.cache_key(
            prefix=PREFIX,
            safe_address="0xABCDef",
            market_id="0xmarket",
            nonce="nonce-1",
        )
        assert key == "market_resolver/0xabcdef/0xmarket/nonce-1"

    def test_safe_address_lowercased(self) -> None:
        """Checksummed and lowercase Safe addresses hash to the same key."""
        # Regression pin: an entry stored from a checksummed address must
        # be findable by a LIST at the lowercase-prefixed path (and vice
        # versa). Without normalisation an EIP-55 vs raw-hex mismatch
        # would silently split the cache namespace.
        upper = mech_cache.cache_key(PREFIX, "0xABCDEF", "0xm", "n1")
        lower = mech_cache.cache_key(PREFIX, "0xabcdef", "0xm", "n1")
        assert upper == lower

    def test_list_prefix_ends_with_slash(self) -> None:
        """LIST prefix ends with a trailing slash so it's a clean namespace."""
        # kv_store LIST does prefix filtering; a missing trailing slash
        # would match ``0xmarket2`` when asking about ``0xmarket``.
        prefix = mech_cache.list_prefix(PREFIX, "0xabc", "0xmarket")
        assert prefix == "market_resolver/0xabc/0xmarket/"


class TestSerializeAndParse:
    """Tests for ``serialize_row`` / ``parse_row`` round-trip."""

    def test_serialize_includes_all_fields(self) -> None:
        """Serialize includes every field with correct defaults."""
        value = mech_cache.serialize_row(
            safe_address="0xabc",
            market_id="0xmarket",
            nonce="n1",
            tool="resolve-market-jury-v1",
            prompt="Will X?",
            fired_at=1_700_000_000,
        )
        parsed = json.loads(value)
        assert parsed["safe"] == "0xabc"
        assert parsed["market_id"] == "0xmarket"
        assert parsed["nonce"] == "n1"
        assert parsed["tool"] == "resolve-market-jury-v1"
        assert parsed["prompt"] == "Will X?"
        assert parsed["fired_at"] == 1_700_000_000
        assert parsed["result"] is None
        assert parsed["error"] is None
        assert parsed["delivered_at"] is None

    def test_parse_row_none_on_garbage(self) -> None:
        """Non-JSON string parses to None, never raises."""
        # Poison-pill defense: a malformed row in kv_store must not
        # crash the sweep loop.
        assert mech_cache.parse_row("not-json") is None
        assert mech_cache.parse_row("") is None

    def test_parse_row_none_on_non_dict_json(self) -> None:
        """A JSON list at the top level parses to None."""
        assert mech_cache.parse_row("[1, 2, 3]") is None


class TestRehydrateToSubgraphShape:
    """Tests for ``rehydrate_to_subgraph_shape``."""

    def _row(self, **overrides: object) -> str:
        base = dict(
            safe_address="0xabc",
            market_id="0xm",
            nonce="n1",
            tool="resolve-market-jury-v1",
            prompt="Will X?",
            fired_at=1_700_000_000,
        )
        base.update(overrides)  # type: ignore[arg-type]
        return mech_cache.serialize_row(**base)  # type: ignore[arg-type]

    def test_undelivered_row_has_empty_deliveries(self) -> None:
        """A row with delivered_at=None rehydrates with an empty deliveries list."""
        rows = {"market_resolver/0xabc/0xm/n1": self._row()}
        out = mech_cache.rehydrate_to_subgraph_shape(rows)
        assert len(out) == 1
        assert out[0]["deliveries"] == []
        assert out[0]["parsedRequest"]["tool"] == "resolve-market-jury-v1"

    def test_delivered_row_has_delivery_entry(self) -> None:
        """delivered_at set -> deliveries[0].toolResponse = row.result."""
        rows = {
            "market_resolver/0xabc/0xm/n1": self._row(
                result='{"is_valid": true, "is_determinable": true, "has_occurred": true}',
                delivered_at=1_700_000_100,
            )
        }
        out = mech_cache.rehydrate_to_subgraph_shape(rows)
        assert len(out[0]["deliveries"]) == 1
        assert (
            out[0]["deliveries"][0]["toolResponse"]
            == '{"is_valid": true, "is_determinable": true, "has_occurred": true}'
        )

    def test_malformed_row_silently_skipped(self) -> None:
        """A poison-pill row is skipped, the good row still comes through."""
        # Pinned because the alternative -- raising -- would crash the
        # sweep loop the moment any drift lands in kv_store, and take
        # market-resolver out of service for every market simultaneously.
        rows = {
            "market_resolver/0xabc/0xm/bad": "not-json",
            "market_resolver/0xabc/0xm/good": self._row(),
        }
        out = mech_cache.rehydrate_to_subgraph_shape(rows)
        assert len(out) == 1
        assert out[0]["id"] == "n1"

    def test_empty_input_returns_empty_list(self) -> None:
        """An empty LIST reply rehydrates to an empty list."""
        assert mech_cache.rehydrate_to_subgraph_shape({}) == []
