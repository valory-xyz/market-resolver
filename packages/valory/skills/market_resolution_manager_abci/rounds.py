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
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

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
    BuildAnswerTxPayload,
    CleanupTrackedMarketsPayload,
    EvaluateAnswersPayload,
    PostTransactionPayload,
    ScanMarketsPayload,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import (
    Event,
)
from packages.valory.skills.mech_interact_abci.states.base import (
    MechInteractionResponse,
    MechMetadata,
)


class SynchronizedData(BaseSynchronizedData):
    """Class to represent the synchronized data.

    The questions_db lives on SharedState (not here) — too heavy for Tendermint.
    Mech requests/responses are stored here for MechInteract integration.
    """

    @property
    def tx_submitter(self) -> Optional[str]:
        """Get the round that submitted the tx through TxSettlement."""
        return self.db.get("tx_submitter", None)

    @property
    def final_tx_hash(self) -> Optional[str]:
        """Get the final settled tx hash."""
        return self.db.get("final_tx_hash", None)

    @property
    def selected_market_id(self) -> Optional[str]:
        """Get the selected market ID for this cycle."""
        return self.db.get("selected_market_id", None)

    @property
    def selected_market_action(self) -> Optional[str]:
        """Get the action for the selected market."""
        return self.db.get("selected_market_action", None)

    @property
    def mech_requests(self) -> List[MechMetadata]:
        """Get the mech requests."""
        serialized = self.db.get("mech_requests", "[]")
        if serialized is None:
            serialized = "[]"
        requests = json.loads(serialized)
        return [MechMetadata(**item) for item in requests]

    @property
    def mech_responses(self) -> List[MechInteractionResponse]:
        """Get the mech responses."""
        serialized = self.db.get("mech_responses", "[]")
        if serialized is None:
            serialized = "[]"
        responses = json.loads(serialized)
        return [MechInteractionResponse(**item) for item in responses]


class ScanMarketsRound(CollectSameUntilThresholdRound):
    """Round to scan pending markets and classify questions."""

    payload_class = ScanMarketsPayload
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
    """Round to evaluate answers — request Mech or reuse existing data.

    Custom end_block to handle two "data present" paths:
    - mech_requests set → DONE → FinishedWithMechRequestRound (needs Mech)
    - evaluation_result set → NONE → BuildAnswerTxRound (has data, skip Mech)
    - both None → NO_MAJORITY fallback
    """

    payload_class = EvaluateAnswersPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "participant_to_evaluate"
    selection_key = (
        get_name(SynchronizedData.mech_requests),
        "evaluation_result",
    )

    def end_block(self) -> Optional[Tuple[BaseSynchronizedData, Enum]]:
        """Process the end of the block.

        Routes based on which field is set:
        - mech_requests → DONE → FinishedWithMechRequestRound
        - evaluation_result → NONE → BuildAnswerTxRound
        """
        if self.threshold_reached:
            values = dict(
                zip(self.selection_key, self.most_voted_payload_values)
            )
            values[self.collection_key] = self.serialized_collection
            synchronized_data = self.synchronized_data.update(
                synchronized_data_class=self.synchronized_data_class,
                **values,
            )
            if values.get(get_name(SynchronizedData.mech_requests)) is not None:
                return synchronized_data, self.done_event
            if values.get("evaluation_result") is not None:
                return synchronized_data, self.none_event
            return self.synchronized_data, self.no_majority_event
        if not self.is_majority_possible(
            self.collection, self.synchronized_data.nb_participants
        ):
            return self.synchronized_data, self.no_majority_event
        return None


class BuildAnswerTxRound(CollectSameUntilThresholdRound):
    """Round to build challenge/answer transactions.

    Payload has tx_submitter and tx_hash. When tx_hash is set,
    done_event fires and TxSettlement picks up most_voted_tx_hash.
    """

    payload_class = BuildAnswerTxPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "participant_to_answer_tx"
    selection_key = (
        "tx_submitter",
        "most_voted_tx_hash",
    )


class CleanupTrackedMarketsRound(CollectSameUntilThresholdRound):
    """Round to purge finalized questions from the database."""

    payload_class = CleanupTrackedMarketsPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "participant_to_cleanup"
    selection_key = ("n_cleaned",)


class PostTransactionRound(CollectSameUntilThresholdRound):
    """Route after TxSettlement based on which tx was submitted.

    Custom end_block checks the payload content to emit the right event:
    - MECH_REQUEST_DONE → MechResponseRound (poll for Mech delivery)
    - ANSWER_TX_DONE → CleanupTrackedMarketsRound
    - ERROR / anything else → CleanupTrackedMarketsRound
    """

    MECH_REQUEST_DONE_PAYLOAD = "MECH_REQUEST_DONE"
    ANSWER_TX_DONE_PAYLOAD = "ANSWER_TX_DONE"
    ERROR_PAYLOAD = "ERROR"

    payload_class = PostTransactionPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = "participant_to_post_tx"
    selection_key: Tuple[str, ...] = ("ignored",)

    def end_block(self) -> Optional[Tuple[BaseSynchronizedData, Enum]]:
        """Route based on which tx was settled."""
        if self.threshold_reached:
            if self.most_voted_payload == self.MECH_REQUEST_DONE_PAYLOAD:
                return self.synchronized_data, Event.MECH_REQUEST_DONE
            if self.most_voted_payload == self.ANSWER_TX_DONE_PAYLOAD:
                return self.synchronized_data, Event.ANSWER_TX_DONE
            return self.synchronized_data, Event.DONE
        if not self.is_majority_possible(
            self.collection, self.synchronized_data.nb_participants
        ):
            return self.synchronized_data, self.no_majority_event
        return None


class FinishedWithMechRequestRound(DegenerateRound):
    """Degenerate round: transition to MechInteract (start Mech flow)."""


class FinishedWithMechPollRound(DegenerateRound):
    """Degenerate round: transition to MechResponseRound (poll for delivery)."""


class FinishedWithAnswerTxRound(DegenerateRound):
    """Degenerate round: transition to TxSettlement."""


class FinishedResolutionRound(DegenerateRound):
    """Degenerate round: transition to ResetPause."""


class MarketResolutionManagerAbciApp(AbciApp[Event]):
    """MarketResolutionManagerAbciApp

    Initial round: ScanMarketsRound

    Initial states: {BuildAnswerTxRound, CleanupTrackedMarketsRound, ScanMarketsRound}

    Transition states:
        0. ScanMarketsRound
            - done: 1.
            - none: 3.
            - no majority: 0.
            - round timeout: 0.
        1. EvaluateAnswersRound
            - done: 4.
            - none: 2.
            - no majority: 3.
            - round timeout: 3.
        2. BuildAnswerTxRound
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
        5. FinishedWithAnswerTxRound
        6. FinishedResolutionRound

    Final states: {FinishedResolutionRound, FinishedWithAnswerTxRound, FinishedWithMechRequestRound}

    Timeouts:
        round timeout: 180.0
    """

    initial_round_cls: AppState = ScanMarketsRound
    initial_states: Set[AppState] = {
        ScanMarketsRound,
        BuildAnswerTxRound,
        CleanupTrackedMarketsRound,
        PostTransactionRound,
    }
    transition_function: AbciAppTransitionFunction = {
        ScanMarketsRound: {
            Event.DONE: EvaluateAnswersRound,
            Event.NONE: CleanupTrackedMarketsRound,
            Event.NO_MAJORITY: ScanMarketsRound,
            Event.ROUND_TIMEOUT: ScanMarketsRound,
        },
        EvaluateAnswersRound: {
            Event.DONE: FinishedWithMechRequestRound,
            Event.NONE: BuildAnswerTxRound,
            Event.NO_MAJORITY: CleanupTrackedMarketsRound,
            Event.ROUND_TIMEOUT: CleanupTrackedMarketsRound,
        },
        BuildAnswerTxRound: {
            Event.DONE: FinishedWithAnswerTxRound,
            Event.NONE: CleanupTrackedMarketsRound,
            Event.NO_MAJORITY: CleanupTrackedMarketsRound,
            Event.ROUND_TIMEOUT: CleanupTrackedMarketsRound,
        },
        PostTransactionRound: {
            Event.MECH_REQUEST_DONE: FinishedWithMechPollRound,
            Event.ANSWER_TX_DONE: CleanupTrackedMarketsRound,
            Event.DONE: CleanupTrackedMarketsRound,
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
        FinishedWithMechPollRound: {},
        FinishedWithAnswerTxRound: {},
        FinishedResolutionRound: {},
    }
    final_states: Set[AppState] = {
        FinishedWithMechRequestRound,
        FinishedWithMechPollRound,
        FinishedWithAnswerTxRound,
        FinishedResolutionRound,
    }
    event_to_timeout: Dict[Event, float] = {
        Event.ROUND_TIMEOUT: 180.0,
    }
    cross_period_persisted_keys: FrozenSet[str] = frozenset()
    db_pre_conditions: Dict[AppState, Set[str]] = {
        ScanMarketsRound: set(),
        BuildAnswerTxRound: set(),
        CleanupTrackedMarketsRound: set(),
        PostTransactionRound: set(),
    }
    db_post_conditions: Dict[AppState, Set[str]] = {
        FinishedWithMechRequestRound: {
            get_name(SynchronizedData.mech_requests)
        },
        FinishedWithMechPollRound: set(),
        FinishedWithAnswerTxRound: {"most_voted_tx_hash"},
        FinishedResolutionRound: set(),
    }
