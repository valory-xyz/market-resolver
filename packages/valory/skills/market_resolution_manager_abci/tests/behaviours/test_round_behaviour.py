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

"""Tests for behaviours/round_behaviour.py."""

from packages.valory.skills.market_resolution_manager_abci.behaviours.build_answer_tx import (
    BuildAnswerTxBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.evaluate_answers import (
    EvaluateAnswersBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.post_transaction import (
    PostTransactionBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.round_behaviour import (
    MarketResolutionManagerRoundBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.scan_markets import (
    ScanMarketsBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    MarketResolutionManagerAbciApp,
)


class TestMarketResolutionManagerRoundBehaviour:
    """Tests for MarketResolutionManagerRoundBehaviour."""

    def test_initial_behaviour_is_scan_markets(self) -> None:
        """Initial behaviour is ScanMarketsBehaviour."""
        assert (
            MarketResolutionManagerRoundBehaviour.initial_behaviour_cls
            is ScanMarketsBehaviour
        )

    def test_abci_app_cls(self) -> None:
        """abci_app_cls is MarketResolutionManagerAbciApp."""
        assert (
            MarketResolutionManagerRoundBehaviour.abci_app_cls
            is MarketResolutionManagerAbciApp
        )

    def test_all_behaviours_registered(self) -> None:
        """All four behaviour classes are in the behaviours set."""
        behaviours = MarketResolutionManagerRoundBehaviour.behaviours
        assert ScanMarketsBehaviour in behaviours
        assert EvaluateAnswersBehaviour in behaviours
        assert BuildAnswerTxBehaviour in behaviours
        assert PostTransactionBehaviour in behaviours
