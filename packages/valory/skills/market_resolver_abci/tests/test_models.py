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

"""Tests for the models module of the composed skill."""

from unittest.mock import MagicMock, patch

from packages.valory.skills.funds_forwarder_abci.rounds import (
    Event as FundsForwarderEvent,
)
from packages.valory.skills.identify_service_owner_abci.rounds import (
    Event as IdentifyServiceOwnerEvent,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    Event as MarketResolutionManagerEvent,
)
from packages.valory.skills.market_resolver_abci.models import (
    BenchmarkTool,
    ConditionalTokensSubgraph,
    MARGIN,
    MULTIPLIER,
    MechResponseSpecs,
    MechToolsSpecs,
    MechsSubgraph,
    OmenSubgraph,
    Params,
    RandomnessApi,
    RealitioSubgraph,
    Requests,
    SharedState,
)
from packages.valory.skills.mech_interact_abci.rounds import Event as MechInteractEvent
from packages.valory.skills.omen_ct_redeem_tokens_abci.rounds import (
    Event as OmenCtRedeemTokensEvent,
)
from packages.valory.skills.omen_fpmm_remove_liquidity_abci.rounds import (
    Event as OmenFpmmRemoveLiquidityEvent,
)
from packages.valory.skills.omen_realitio_withdraw_bonds_abci.rounds import (
    Event as OmenRealitioWithdrawBondEvent,
)
from packages.valory.skills.reset_pause_abci.rounds import Event as ResetPauseEvent
from packages.valory.skills.transaction_settlement_abci.rounds import Event as TSEvent


def _make_params_mock(
    round_timeout: float = 60.0,
    validate_timeout: float = 30.0,
    finalize_timeout: float = 30.0,
    history_check_timeout: float = 30.0,
    reset_pause_duration: int = 30,
    mech_interact_round_timeout: float = 1200.0,
) -> MagicMock:
    """Return a mock params object with the attributes SharedState.setup() reads."""
    params = MagicMock()
    params.round_timeout_seconds = round_timeout
    params.validate_timeout = validate_timeout
    params.finalize_timeout = finalize_timeout
    params.history_check_timeout = history_check_timeout
    params.reset_pause_duration = reset_pause_duration
    params.mech_interact_round_timeout_seconds = mech_interact_round_timeout
    return params


class TestSharedStateSetup:
    """Tests for SharedState.setup() event_to_timeout wiring."""

    def test_setup_wires_all_event_timeouts(self) -> None:
        """All 14 event_to_timeout entries are set by setup()."""
        context = MagicMock()
        context.params = _make_params_mock(
            round_timeout=60.0,
            validate_timeout=30.0,
            finalize_timeout=25.0,
            history_check_timeout=20.0,
            reset_pause_duration=10,
            mech_interact_round_timeout=1200.0,
        )

        state = SharedState(name="state", skill_context=context)

        # Patch super().setup() to avoid framework bootstrapping
        with patch(
            "packages.valory.skills.abstract_round_abci.models.SharedState.setup"
        ):
            from packages.valory.skills.market_resolver_abci.composition import (
                MarketResolverAbciApp,
            )

            state.setup()

        # Verify every timeout entry was set to the expected value
        assert MarketResolverAbciApp.event_to_timeout[TSEvent.ROUND_TIMEOUT] == 60.0
        assert (
            MarketResolverAbciApp.event_to_timeout[ResetPauseEvent.ROUND_TIMEOUT]
            == 60.0
        )
        assert (
            MarketResolverAbciApp.event_to_timeout[TSEvent.RESET_TIMEOUT]
            == 60.0 * MULTIPLIER
        )
        assert MarketResolverAbciApp.event_to_timeout[TSEvent.VALIDATE_TIMEOUT] == 30.0
        assert MarketResolverAbciApp.event_to_timeout[TSEvent.FINALIZE_TIMEOUT] == 25.0
        assert MarketResolverAbciApp.event_to_timeout[TSEvent.CHECK_TIMEOUT] == 20.0
        assert (
            MarketResolverAbciApp.event_to_timeout[
                ResetPauseEvent.RESET_AND_PAUSE_TIMEOUT
            ]
            == 10 + MARGIN
        )
        assert (
            MarketResolverAbciApp.event_to_timeout[
                IdentifyServiceOwnerEvent.ROUND_TIMEOUT
            ]
            == 60.0
        )
        assert (
            MarketResolverAbciApp.event_to_timeout[FundsForwarderEvent.ROUND_TIMEOUT]
            == 60.0
        )
        assert (
            MarketResolverAbciApp.event_to_timeout[
                OmenFpmmRemoveLiquidityEvent.ROUND_TIMEOUT
            ]
            == 60.0
        )
        assert (
            MarketResolverAbciApp.event_to_timeout[
                OmenCtRedeemTokensEvent.ROUND_TIMEOUT
            ]
            == 60.0
        )
        assert (
            MarketResolverAbciApp.event_to_timeout[
                OmenRealitioWithdrawBondEvent.ROUND_TIMEOUT
            ]
            == 60.0
        )
        assert (
            MarketResolverAbciApp.event_to_timeout[
                MarketResolutionManagerEvent.ROUND_TIMEOUT
            ]
            == 60.0
        )
        assert (
            MarketResolverAbciApp.event_to_timeout[MechInteractEvent.ROUND_TIMEOUT]
            == 1200.0
        )


class TestReExports:
    """Verify that all model re-exports point to the correct upstream types."""

    def test_requests_re_export(self) -> None:
        """Requests is re-exported from abstract_round_abci."""
        from packages.valory.skills.abstract_round_abci.models import (
            Requests as BaseRequests,
        )

        assert Requests is BaseRequests

    def test_benchmark_tool_re_export(self) -> None:
        """Check that BenchmarkTool is re-exported from abstract_round_abci."""
        from packages.valory.skills.abstract_round_abci.models import (
            BenchmarkTool as BaseBenchmarkTool,
        )

        assert BenchmarkTool is BaseBenchmarkTool

    def test_randomness_api_re_export(self) -> None:
        """Check that RandomnessApi is ApiSpecs from abstract_round_abci."""
        from packages.valory.skills.abstract_round_abci.models import ApiSpecs

        assert RandomnessApi is ApiSpecs

    def test_omen_subgraph_re_export(self) -> None:
        """Check that OmenSubgraph is re-exported from omen_ct_redeem_tokens_abci."""
        from packages.valory.skills.omen_ct_redeem_tokens_abci.models import (
            OmenSubgraph as BaseOmenSubgraph,
        )

        assert OmenSubgraph is BaseOmenSubgraph

    def test_conditional_tokens_subgraph_re_export(self) -> None:
        """Check that ConditionalTokensSubgraph is re-exported from omen_ct_redeem_tokens_abci."""
        from packages.valory.skills.omen_ct_redeem_tokens_abci.models import (
            ConditionalTokensSubgraph as BaseConditionalTokensSubgraph,
        )

        assert ConditionalTokensSubgraph is BaseConditionalTokensSubgraph

    def test_realitio_subgraph_re_export(self) -> None:
        """Check that RealitioSubgraph is re-exported from omen_realitio_withdraw_bonds_abci."""
        from packages.valory.skills.omen_realitio_withdraw_bonds_abci.models import (
            RealitioSubgraph as BaseRealitioSubgraph,
        )

        assert RealitioSubgraph is BaseRealitioSubgraph

    def test_mech_response_specs_re_export(self) -> None:
        """Check that MechResponseSpecs is re-exported from mech_interact_abci."""
        from packages.valory.skills.mech_interact_abci.models import (
            MechResponseSpecs as BaseMechResponseSpecs,
        )

        assert MechResponseSpecs is BaseMechResponseSpecs

    def test_mech_tools_specs_re_export(self) -> None:
        """Check that MechToolsSpecs is re-exported from mech_interact_abci."""
        from packages.valory.skills.mech_interact_abci.models import (
            MechToolsSpecs as BaseMechToolsSpecs,
        )

        assert MechToolsSpecs is BaseMechToolsSpecs

    def test_mechs_subgraph_re_export(self) -> None:
        """Check that MechsSubgraph is re-exported from mech_interact_abci."""
        from packages.valory.skills.mech_interact_abci.models import (
            MechsSubgraph as BaseMechsSubgraph,
        )

        assert MechsSubgraph is BaseMechsSubgraph


class TestConstants:
    """Tests for module-level constants."""

    def test_margin(self) -> None:
        """MARGIN is 5."""
        assert MARGIN == 5

    def test_multiplier(self) -> None:
        """MULTIPLIER is 2."""
        assert MULTIPLIER == 2


class TestParamsMRO:
    """Smoke test for the Params multi-inheritance class."""

    def test_params_class_exists(self) -> None:
        """Params class is importable."""
        assert Params is not None

    def test_params_inherits_from_mro(self) -> None:
        """Params inherits from all expected sub-skill Params classes."""
        from packages.valory.skills.funds_forwarder_abci.models import (
            FundsForwarderParams,
        )
        from packages.valory.skills.market_resolution_manager_abci.models import (
            MarketResolutionManagerParams,
        )
        from packages.valory.skills.mech_interact_abci.models import (
            Params as MechInteractAbciParams,
        )
        from packages.valory.skills.omen_ct_redeem_tokens_abci.models import (
            CtRedeemTokensParams,
        )
        from packages.valory.skills.omen_fpmm_remove_liquidity_abci.models import (
            FpmmRemoveLiquidityParams,
        )
        from packages.valory.skills.omen_realitio_withdraw_bonds_abci.models import (
            RealitioWithdrawBondsParams,
        )
        from packages.valory.skills.termination_abci.models import TerminationParams

        assert issubclass(Params, MarketResolutionManagerParams)
        assert issubclass(Params, FpmmRemoveLiquidityParams)
        assert issubclass(Params, CtRedeemTokensParams)
        assert issubclass(Params, RealitioWithdrawBondsParams)
        assert issubclass(Params, FundsForwarderParams)
        assert issubclass(Params, MechInteractAbciParams)
        assert issubclass(Params, TerminationParams)
