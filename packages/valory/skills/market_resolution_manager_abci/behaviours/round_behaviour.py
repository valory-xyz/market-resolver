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

"""This module contains the round behaviour for the market resolution manager."""

from typing import Set, Type

from packages.valory.skills.abstract_round_abci.behaviours import (
    AbstractRoundBehaviour,
    BaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.build_challenges import (
    BuildChallengesTxBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.cleanup_tracked_markets import (
    CleanupTrackedMarketsBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.evaluate_answers import (
    EvaluateAnswersBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.scan_pending_markets import (
    ScanPendingMarketsBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    MarketResolutionManagerAbciApp,
)


class MarketResolutionManagerRoundBehaviour(AbstractRoundBehaviour):
    """This behaviour manages the consensus stages for market resolution."""

    initial_behaviour_cls = ScanPendingMarketsBehaviour
    abci_app_cls = MarketResolutionManagerAbciApp  # type: ignore
    behaviours: Set[Type[BaseBehaviour]] = {
        ScanPendingMarketsBehaviour,
        EvaluateAnswersBehaviour,
        BuildChallengesTxBehaviour,
        CleanupTrackedMarketsBehaviour,
    }
