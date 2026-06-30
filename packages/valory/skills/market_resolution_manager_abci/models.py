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

"""This module contains the models for the market resolution manager."""

from typing import Any, Dict, List, Type

from packages.valory.skills.abstract_round_abci.base import AbciApp
from packages.valory.skills.abstract_round_abci.models import (
    ApiSpecs,
    BaseParams,
)
from packages.valory.skills.abstract_round_abci.models import (
    BenchmarkTool as BaseBenchmarkTool,
)
from packages.valory.skills.abstract_round_abci.models import Requests as BaseRequests
from packages.valory.skills.abstract_round_abci.models import (
    SharedState as BaseSharedState,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    MarketResolutionManagerAbciApp,
)
from packages.valory.skills.mech_interact_abci.models import (
    SharedState as MechInteractSharedState,
)
from packages.valory.skills.omen_ct_redeem_tokens_abci.models import (
    SharedState as CtRedeemTokensSharedState,
)
from packages.valory.skills.omen_realitio_withdraw_bonds_abci.models import (
    SharedState as RealitioWithdrawBondsSharedState,
)

Requests = BaseRequests
BenchmarkTool = BaseBenchmarkTool


class MechGnosisSubgraph(ApiSpecs):
    """ApiSpecs wrapper for the Mech Marketplace Gnosis subgraph."""


class SharedState(
    MechInteractSharedState,
    CtRedeemTokensSharedState,
    RealitioWithdrawBondsSharedState,
    BaseSharedState,
):
    """Keep the current shared state of the skill."""

    abci_app_cls: Type[AbciApp] = MarketResolutionManagerAbciApp  # type: ignore

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize."""
        super().__init__(*args, **kwargs)
        self.questions_db: Dict[str, Any] = {}


class MarketResolutionManagerParams(  # pylint: disable=too-many-instance-attributes
    BaseParams,
):
    """Parameters for the market_resolution_manager_abci skill."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the parameters object."""
        self.service_endpoint_base: str = self._ensure(
            "service_endpoint_base", kwargs, str
        )
        self.watched_creator_addresses: List[str] = kwargs.pop(
            "watched_creator_addresses", []
        )
        self.trusted_addresses: List[str] = kwargs.pop("trusted_addresses", [])
        self.mech_tool_resolve_market: str = kwargs.pop(
            "mech_tool_resolve_market", "resolve-market-jury-v1"
        )
        self.initial_answer_bond: int = kwargs.pop(
            "initial_answer_bond", 1000000000000000000  # 1 xDAI
        )
        self.challenge_cooldown_fraction: float = kwargs.pop(
            "challenge_cooldown_fraction", 0.25
        )
        self.challenge_urgency_buffer: int = kwargs.pop(
            "challenge_urgency_buffer", 3600
        )
        self.max_challenge_bond: int = kwargs.pop(
            "max_challenge_bond", 16000000000000000000  # 16 xDAI
        )
        self.max_mech_retries: int = kwargs.pop("max_mech_retries", 10)
        # Minimum cooldown between Mech retries for the same market,
        # applied to garbage/unparseable responses (error discriminators
        # like ``all_voters_failed`` / ``judge_api_error``). Default 4h:
        # long enough for a transient OpenRouter credit lapse or API
        # outage to clear before burning the next retry, but short enough
        # that ``max_mech_retries=10`` still spans <2 days total.
        self.mech_retry_cooldown: int = kwargs.pop("mech_retry_cooldown", 14400)  # 4h
        self.omen_subgraph_max_market_age_seconds: int = kwargs.pop(
            "omen_subgraph_max_market_age_seconds",
            365 * 86400,  # 1 year
        )
        # Read without popping -- MechParams also needs these
        self.mech_interact_round_timeout_seconds: int = kwargs.get(
            "mech_interact_round_timeout_seconds", 1200
        )
        super().__init__(*args, **kwargs)
