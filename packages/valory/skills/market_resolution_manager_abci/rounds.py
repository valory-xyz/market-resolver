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

"""This module contains the rounds for the market resolution manager."""

from typing import Dict, FrozenSet, Optional, Set

from packages.valory.skills.abstract_round_abci.base import (
    AbciApp,
    AbciAppTransitionFunction,
    AppState,
    BaseSynchronizedData,
    CollectSameUntilThresholdRound,
    DegenerateRound,
    get_name,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    BuildChallengesTxPayload,
    CleanupTrackedMarketsPayload,
    EvaluateAnswersPayload,
    ScanPendingMarketsPayload,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import (
    Event,
)


class SynchronizedData(BaseSynchronizedData):
    """Class to represent the synchronized data.

    The questions_db is NOT stored here — it lives on SharedState (context.state)
    to avoid passing large data through Tendermint consensus.
    Only lightweight consensus fields are stored here.
    """

    @property
    def selected_market_id(self) -> Optional[str]:
        """Get the selected question ID for this cycle."""
        return self.db.get("selected_market_id", None)

    @property
    def selected_market_action(self) -> Optional[str]:
        """Get the action for the selected question."""
        return self.db.get("selected_market_action", None)


class ScanPendingMarketsRound(CollectSameUntilThresholdRound):
    """Round to scan pending markets and classify questions.

    Agents agree on: n_markets, selected_market_id, selected_market_action.
    The full questions_db is computed deterministically by each agent locally.
    """

    payload_class = ScanPendingMarketsPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "participant_to_scan"
    selection_key = (
        "n_markets",
        get_name(SynchronizedData.selected_market_id),
        get_name(SynchronizedData.selected_market_action),
    )


class EvaluateAnswersRound(CollectSameUntilThresholdRound):
    """Round to evaluate answers — simulated Mech or reuse existing data.

    done_event → BuildChallengesTxRound (has evaluation)
    none_event → FinishedWithMechRequestRound (needs real Mech — not used yet)
    """

    payload_class = EvaluateAnswersPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "participant_to_evaluate"
    selection_key = ("evaluation_result",)


class BuildChallengesTxRound(CollectSameUntilThresholdRound):
    """Round to build challenge/answer transactions."""

    payload_class = BuildChallengesTxPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "participant_to_challenges"
    selection_key = ("challenge_data",)


class CleanupTrackedMarketsRound(CollectSameUntilThresholdRound):
    """Round to purge finalized questions from the database."""

    payload_class = CleanupTrackedMarketsPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "participant_to_cleanup"
    selection_key = ("n_cleaned",)


class FinishedWithMechRequestRound(DegenerateRound):
    """Degenerate round: transition to MechInteract."""


class FinishedWithChallengeTxRound(DegenerateRound):
    """Degenerate round: transition to TxSettlement."""


class FinishedResolutionRound(DegenerateRound):
    """Degenerate round: transition to ResetPause."""


class MarketResolutionManagerAbciApp(AbciApp[Event]):
    """MarketResolutionManagerAbciApp

    Initial round: ScanPendingMarketsRound

    Initial states: {BuildChallengesTxRound, CleanupTrackedMarketsRound, ScanPendingMarketsRound}

    Transition states:
        0. ScanPendingMarketsRound
            - done: 1.
            - none: 3.
            - no majority: 0.
            - round timeout: 0.
        1. EvaluateAnswersRound
            - done: 2.
            - none: 4.
            - no majority: 3.
            - round timeout: 3.
        2. BuildChallengesTxRound
            - done: 5.
            - none: 3.
            - no majority: 3.
            - round timeout: 3.
        3. CleanupTrackedMarketsRound
            - done: 6.
            - none: 6.
            - no majority: 6.
            - round timeout: 6.
        4. FinishedWithMechRequestRound
        5. FinishedWithChallengeTxRound
        6. FinishedResolutionRound

    Final states: {FinishedResolutionRound, FinishedWithChallengeTxRound, FinishedWithMechRequestRound}

    Timeouts:
        round timeout: 180.0
    """

    initial_round_cls: AppState = ScanPendingMarketsRound
    initial_states: Set[AppState] = {
        ScanPendingMarketsRound,
        BuildChallengesTxRound,
        CleanupTrackedMarketsRound,
    }
    transition_function: AbciAppTransitionFunction = {
        ScanPendingMarketsRound: {
            Event.DONE: EvaluateAnswersRound,
            Event.NONE: CleanupTrackedMarketsRound,
            Event.NO_MAJORITY: ScanPendingMarketsRound,
            Event.ROUND_TIMEOUT: ScanPendingMarketsRound,
        },
        EvaluateAnswersRound: {
            Event.DONE: BuildChallengesTxRound,
            Event.NONE: CleanupTrackedMarketsRound,
            Event.NO_MAJORITY: CleanupTrackedMarketsRound,
            Event.ROUND_TIMEOUT: CleanupTrackedMarketsRound,
        },
        BuildChallengesTxRound: {
            Event.DONE: FinishedWithChallengeTxRound,
            Event.NONE: CleanupTrackedMarketsRound,
            Event.NO_MAJORITY: CleanupTrackedMarketsRound,
            Event.ROUND_TIMEOUT: CleanupTrackedMarketsRound,
        },
        CleanupTrackedMarketsRound: {
            Event.DONE: FinishedResolutionRound,
            Event.NONE: FinishedResolutionRound,
            Event.NO_MAJORITY: FinishedResolutionRound,
            Event.ROUND_TIMEOUT: FinishedResolutionRound,
        },
        # FinishedWithMechRequestRound: {},  # TODO: re-enable with real Mech
        FinishedWithChallengeTxRound: {},
        FinishedResolutionRound: {},
    }
    final_states: Set[AppState] = {
        # FinishedWithMechRequestRound,  # TODO: re-enable with real Mech
        FinishedWithChallengeTxRound,
        FinishedResolutionRound,
    }
    event_to_timeout: Dict[Event, float] = {
        Event.ROUND_TIMEOUT: 180.0,
    }
    cross_period_persisted_keys: FrozenSet[str] = frozenset()
    db_pre_conditions: Dict[AppState, Set[str]] = {
        ScanPendingMarketsRound: set(),
        BuildChallengesTxRound: set(),
        CleanupTrackedMarketsRound: set(),
    }
    db_post_conditions: Dict[AppState, Set[str]] = {
        FinishedWithChallengeTxRound: {"most_voted_tx_hash"},
        FinishedResolutionRound: set(),
    }
