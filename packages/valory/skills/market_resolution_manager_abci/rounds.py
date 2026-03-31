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

import json
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional, Set, Tuple

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

    This data is replicated by the tendermint application.
    """

    @property
    def questions_db(self) -> Dict[str, Any]:
        """Get the questions database."""
        serialized = self.db.get("questions_db", "{}")
        if serialized is None:
            return {}
        return json.loads(serialized)

    @property
    def selected_question_id(self) -> Optional[str]:
        """Get the selected question ID for this cycle."""
        return self.db.get("selected_question_id", None)

    @property
    def selected_question_action(self) -> Optional[str]:
        """Get the action for the selected question."""
        return self.db.get("selected_question_action", None)


class ScanPendingMarketsRound(CollectSameUntilThresholdRound):
    """Round to scan pending markets and classify questions."""

    payload_class = ScanPendingMarketsPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "scan_pending_markets"
    selection_key = ("content",)

    def end_block(self) -> Optional[Tuple[BaseSynchronizedData, Enum]]:
        """Process the end of the block."""
        if self.threshold_reached:
            payload = json.loads(self.most_voted_payload)
            event = Event(payload["event"])
            synchronized_data = self.synchronized_data.update(
                synchronized_data_class=SynchronizedData,
                **{
                    get_name(SynchronizedData.questions_db): payload.get(
                        "questions_db", "{}"
                    ),
                    get_name(SynchronizedData.selected_question_id): payload.get(
                        "selected_question_id"
                    ),
                    get_name(SynchronizedData.selected_question_action): payload.get(
                        "selected_question_action"
                    ),
                },
            )
            return synchronized_data, event
        if not self.is_majority_possible(
            self.collection, self.synchronized_data.nb_participants
        ):
            return self.synchronized_data, Event.NO_MAJORITY
        return None


class EvaluateAnswersRound(CollectSameUntilThresholdRound):
    """Round to build Mech requests for questions needing evaluation."""

    payload_class = EvaluateAnswersPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "evaluate_answers"
    selection_key = ("content",)

    def end_block(self) -> Optional[Tuple[BaseSynchronizedData, Enum]]:
        """Process the end of the block."""
        if self.threshold_reached:
            payload = json.loads(self.most_voted_payload)
            event = Event(payload["event"])
            return self.synchronized_data, event
        if not self.is_majority_possible(
            self.collection, self.synchronized_data.nb_participants
        ):
            return self.synchronized_data, Event.NO_MAJORITY
        return None


class BuildChallengesTxRound(CollectSameUntilThresholdRound):
    """Round to build challenge/answer transactions."""

    payload_class = BuildChallengesTxPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "build_challenges"
    selection_key = ("content",)

    def end_block(self) -> Optional[Tuple[BaseSynchronizedData, Enum]]:
        """Process the end of the block."""
        if self.threshold_reached:
            payload = json.loads(self.most_voted_payload)
            event = Event(payload["event"])
            synchronized_data = self.synchronized_data.update(
                synchronized_data_class=SynchronizedData,
                **{
                    get_name(SynchronizedData.questions_db): payload.get(
                        "questions_db", "{}"
                    ),
                },
            )
            return synchronized_data, event
        if not self.is_majority_possible(
            self.collection, self.synchronized_data.nb_participants
        ):
            return self.synchronized_data, Event.NO_MAJORITY
        return None


class CleanupTrackedMarketsRound(CollectSameUntilThresholdRound):
    """Round to purge finalized questions from the database."""

    payload_class = CleanupTrackedMarketsPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "cleanup_tracked_markets"
    selection_key = ("content",)

    def end_block(self) -> Optional[Tuple[BaseSynchronizedData, Enum]]:
        """Process the end of the block."""
        if self.threshold_reached:
            payload = json.loads(self.most_voted_payload)
            synchronized_data = self.synchronized_data.update(
                synchronized_data_class=SynchronizedData,
                **{
                    get_name(SynchronizedData.questions_db): payload.get(
                        "questions_db", "{}"
                    ),
                },
            )
            return synchronized_data, Event.DONE
        if not self.is_majority_possible(
            self.collection, self.synchronized_data.nb_participants
        ):
            return self.synchronized_data, Event.NO_MAJORITY
        return None


class FinishedWithMechRequestRound(DegenerateRound):
    """Degenerate round: transition to MechInteract."""


class FinishedWithChallengeTxRound(DegenerateRound):
    """Degenerate round: transition to TxSettlement."""


class FinishedResolutionRound(DegenerateRound):
    """Degenerate round: transition to ResetPause."""


class MarketResolutionManagerAbciApp(AbciApp[Event]):
    """MarketResolutionManagerAbciApp

    Initial round: ScanPendingMarketsRound

    Initial states: {BuildChallengesTxRound, ScanPendingMarketsRound}

    Transition states:
        0. ScanPendingMarketsRound
            - done: 1.
            - none: 3.
            - no majority: 0.
            - round timeout: 0.
        1. EvaluateAnswersRound
            - done: 4.
            - none: 2.
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
            Event.DONE: FinishedWithMechRequestRound,
            Event.NONE: BuildChallengesTxRound,
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
        FinishedWithMechRequestRound: {},
        FinishedWithChallengeTxRound: {},
        FinishedResolutionRound: {},
    }
    final_states: Set[AppState] = {
        FinishedWithMechRequestRound,
        FinishedWithChallengeTxRound,
        FinishedResolutionRound,
    }
    event_to_timeout: Dict[Event, float] = {
        Event.ROUND_TIMEOUT: 180.0,
    }
    cross_period_persisted_keys: FrozenSet[str] = frozenset(
        {get_name(SynchronizedData.questions_db)}
    )
    db_pre_conditions: Dict[AppState, Set[str]] = {
        ScanPendingMarketsRound: set(),
        BuildChallengesTxRound: set(),
        CleanupTrackedMarketsRound: set(),
    }
    db_post_conditions: Dict[AppState, Set[str]] = {
        FinishedWithMechRequestRound: set(),
        FinishedWithChallengeTxRound: {"most_voted_tx_hash"},
        FinishedResolutionRound: set(),
    }
