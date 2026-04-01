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

from typing import Any, Dict, List

from packages.valory.skills.abstract_round_abci.models import BaseParams
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


Requests = BaseRequests
BenchmarkTool = BaseBenchmarkTool


class SharedState(BaseSharedState):
    """Keep the current shared state of the skill."""

    abci_app_cls = MarketResolutionManagerAbciApp

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize."""
        super().__init__(*args, **kwargs)
        self.questions_db: Dict[str, Any] = {}


class MarketResolutionManagerParams(BaseParams):
    """Parameters for the market_resolution_manager_abci skill."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the parameters object."""
        self.watched_creator_addresses: List[str] = kwargs.pop(
            "watched_creator_addresses", []
        )
        self.trusted_addresses: List[str] = kwargs.pop("trusted_addresses", [])
        self.mech_tool: str = kwargs.pop(
            "mech_tool", "resolve-market-jury-v1"
        )
        self.initial_answer_bond: int = kwargs.pop(
            "initial_answer_bond", 1000000000000000  # 0.001 xDAI
        )
        self.challenge_cooldown_fraction: float = kwargs.pop(
            "challenge_cooldown_fraction", 0.25
        )
        self.challenge_urgency_buffer: int = kwargs.pop(
            "challenge_urgency_buffer", 3600
        )
        self.max_challenge_bond: int = kwargs.pop(
            "max_challenge_bond", 10000000000000000000  # 10 xDAI
        )
        self.max_escalation_rounds: int = kwargs.pop("max_escalation_rounds", 5)
        self.max_mech_retries: int = kwargs.pop("max_mech_retries", 10)
        # Read without popping — MechParams also needs these
        self.mech_interact_round_timeout_seconds: int = kwargs.get(
            "mech_interact_round_timeout_seconds", 1200
        )
        super().__init__(*args, **kwargs)
