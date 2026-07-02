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

"""Tests for the models module."""

# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from unittest.mock import MagicMock

import pytest

from packages.valory.skills.market_resolution_manager_abci.models import (
    MarketResolutionManagerParams,
    SharedState,
)


def _make_base_kwargs() -> dict:
    """Return the minimal set of kwargs required by BaseParams."""
    return {
        "service_endpoint_base": "https://example.com/",
        "skill_context": MagicMock(),
        "name": "params",
        "setup": {
            "all_participants": ["0xAgent"],
            "safe_contract_address": "0xSafe",
            "consensus_threshold": None,
        },
        "tendermint_url": "http://localhost:26657",
        "max_healthcheck": 120,
        "round_timeout_seconds": 60.0,
        "sleep_time": 1,
        "retry_timeout": 3,
        "retry_attempts": 10,
        "reset_pause_duration": 30,
        "drand_public_key": "868f005eb8e6e4ca0a47c8a77ceaa5309a47978a7c71bc5cce96366b5d7a569937c529eeda66c7293784a9402801af31",
        "tendermint_check_sleep_delay": 3,
        "tendermint_com_url": "http://localhost:8080",
        "tendermint_max_retries": 5,
        "tendermint_recovery_params": {},
        "cleanup_history_depth": 0,
        "genesis_config": {
            "genesis_time": "2022-05-20T16:00:21.735122717Z",
            "chain_id": "chain-c4daS1",
            "consensus_params": {
                "block": {
                    "max_bytes": "22020096",
                    "max_gas": "-1",
                    "time_iota_ms": "1000",
                },
                "evidence": {
                    "max_age_num_blocks": "100000",
                    "max_age_duration": "172800000000000",
                    "max_bytes": "1048576",
                },
                "validator": {"pub_key_types": ["ed25519"]},
                "version": {},
            },
            "voting_power": "10",
        },
        "default_chain_id": "gnosis",
        "on_chain_service_id": None,
        "share_tm_config_on_startup": False,
        "request_timeout": 10.0,
        "request_retry_delay": 1.0,
        "tx_timeout": 30.0,
        "max_attempts": 10,
        "reset_tendermint_after": 2,
        "service_registry_address": "0xReg",
        "keeper_timeout": 30.0,
        "mech_interact_round_timeout_seconds": 1200,
        "cleanup_history_depth_current": None,
        "is_external": False,
        "use_termination": False,
        "use_slashing": False,
        "slash_cooldown_hours": 3,
        "slash_threshold_amount": 0,
        "light_client_avoidance": False,
        "realitio_contract": "0xRealitio",
        "multisend_address": "0xMultisend",
        "service_id": "market_resolution_manager",
        "tendermint_p2p_url": "localhost:26656",
        "light_slash_unit_amount": 5000000000000000,
        "serious_slash_unit_amount": 8000000000000000,
        "termination_sleep": 900,
        "termination_from_block": 0,
        "validate_timeout": 1205,
        "history_check_timeout": 1205,
        "init_fallback_gas": 0,
        "keeper_allowed_retries": 3,
    }


class TestMarketResolutionManagerParams:
    """Tests for MarketResolutionManagerParams."""

    def test_defaults(self) -> None:
        """Test that params use correct default values."""
        kwargs = _make_base_kwargs()
        params = MarketResolutionManagerParams(**kwargs)
        assert params.service_endpoint_base == "https://example.com/"
        assert params.watched_creator_addresses == []
        assert params.trusted_addresses == []
        assert params.mech_tool_resolve_market == "resolve-market-jury-v1"
        assert params.initial_answer_bond == 10**18
        assert params.challenge_cooldown_fraction == 0.25
        assert params.challenge_urgency_buffer == 3600
        assert params.max_challenge_bond == 16 * 10**18
        assert params.max_mech_retries == 10
        assert params.mech_retry_cooldown == 14400
        assert params.omen_subgraph_max_market_age_seconds == 365 * 86400
        assert params.mech_interact_round_timeout_seconds == 1200

    def test_custom_values(self) -> None:
        """Test that params accept custom values."""
        kwargs = _make_base_kwargs()
        kwargs.update(
            {
                "watched_creator_addresses": ["0xCreator"],
                "trusted_addresses": ["0xTrusted"],
                "mech_tool_resolve_market": "my-tool",
                "initial_answer_bond": 5 * 10**17,
                "challenge_cooldown_fraction": 0.5,
                "challenge_urgency_buffer": 7200,
                "max_challenge_bond": 8 * 10**18,
                "max_mech_retries": 5,
                "mech_retry_cooldown": 7200,
                "omen_subgraph_max_market_age_seconds": 180 * 86400,
            }
        )
        params = MarketResolutionManagerParams(**kwargs)
        assert params.watched_creator_addresses == ["0xCreator"]
        assert params.trusted_addresses == ["0xTrusted"]
        assert params.mech_tool_resolve_market == "my-tool"
        assert params.initial_answer_bond == 5 * 10**17
        assert params.challenge_cooldown_fraction == 0.5
        assert params.challenge_urgency_buffer == 7200
        assert params.max_challenge_bond == 8 * 10**18
        assert params.max_mech_retries == 5
        assert params.mech_retry_cooldown == 7200
        assert params.omen_subgraph_max_market_age_seconds == 180 * 86400

    def test_service_endpoint_base_required(self) -> None:
        """Test that missing service_endpoint_base raises an error."""
        kwargs = _make_base_kwargs()
        del kwargs["service_endpoint_base"]
        with pytest.raises(Exception, match="service_endpoint_base"):
            MarketResolutionManagerParams(**kwargs)


class TestSharedState:
    """Tests for SharedState."""

    def test_questions_db_initialized_empty(self) -> None:
        """Test that questions_db is initialized as an empty dict."""
        context = MagicMock()
        context.params = MagicMock()
        state = SharedState(name="state", skill_context=context)
        assert state.questions_db == {}

    def test_questions_db_settable(self) -> None:
        """Test that questions_db can be set."""
        context = MagicMock()
        context.params = MagicMock()
        state = SharedState(name="state", skill_context=context)
        state.questions_db = {"0xMarket": {"status": "NEEDS_ANSWER"}}
        assert "0xMarket" in state.questions_db

    def test_realitio_claim_build_cache_initialized_empty(self) -> None:
        """The withdraw-bonds claim cache must be initialized on the shared state.

        Regression for the production crash-loop: ``self.context.state``
        resolves to this composed ``SharedState``, and
        ``RealitioWithdrawBondsBehaviour._build_claim_txs`` reads
        ``state.realitio_claim_build_cache`` directly. The cache only
        exists if ``RealitioWithdrawBondsSharedState`` is in this class's
        MRO so its ``__init__`` runs via the cooperative ``super()`` chain
        (the same mechanism that provides ``ignored_ct_positions`` from
        the CT-redeem sub-skill). When it was missing from the MRO the
        attribute was absent and the round raised ``AttributeError`` ->
        ``stop_and_exit`` -> Propel restart loop.
        """
        context = MagicMock()
        context.params = MagicMock()
        state = SharedState(name="state", skill_context=context)
        assert state.realitio_claim_build_cache == {}

    def test_withdraw_bonds_sharedstate_in_mro(self) -> None:
        """The withdraw-bonds sub-skill SharedState must be in the MRO.

        Direct guard on the inheritance that makes the cache attribute
        exist on the composed runtime state. Mirrors how the CT-redeem
        and mech-interact sub-skill SharedStates are wired in.
        """
        from packages.valory.skills.omen_realitio_withdraw_bonds_abci.models import (
            SharedState as RealitioWithdrawBondsSharedState,
        )

        assert RealitioWithdrawBondsSharedState in SharedState.__mro__
