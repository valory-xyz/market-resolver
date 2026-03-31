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

"""This module contains the BuildChallengesTxBehaviour."""

import json
from typing import Any, Dict, Generator, Optional

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    BuildChallengesTxPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    BuildChallengesTxRound,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import Event

# Status constants
TRUSTED_ANSWER = "TRUSTED_ANSWER"
NEEDS_EVALUATION = "NEEDS_EVALUATION"
VERIFIED_OK = "VERIFIED_OK"
CHALLENGE_PENDING = "CHALLENGE_PENDING"

# Realitio answer encoding
MAX_PREVIOUS_UNANSWERED = 0


class BuildChallengesTxBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to build challenge/answer transactions.

    Entered in two ways:
    - After MechInteract returns (fresh Mech data to process)
    - From EvaluateAnswers NONE event (existing Mech data, re-challenge)
    """

    matching_round = BuildChallengesTxRound

    def async_act(self) -> Generator:
        """Build challenge transaction or mark as verified."""
        question_id = self.synchronized_data.selected_question_id
        if question_id is None:
            self.context.logger.info("No selected question — nothing to build.")
            yield from self._send_payload(Event.NONE, {})
            return

        questions_db = dict(self.synchronized_data.questions_db)
        entry = questions_db.get(question_id)
        if entry is None:
            self.context.logger.error(f"Question {question_id} not found in DB.")
            yield from self._send_payload(Event.NONE, questions_db)
            return

        # Get Mech evaluation — either fresh from MechInteract or existing in DB
        evaluation = entry.get("evaluation")

        # TODO: if coming from MechInteract, parse mech_responses from
        # synchronized_data and populate evaluation in the DB entry.
        # For now, if evaluation is None, we haven't implemented the
        # MechInteract integration yet — emit NONE.
        if evaluation is None:
            self.context.logger.info(
                f"Question {question_id}: no evaluation data available yet."
            )
            yield from self._send_payload(Event.NONE, questions_db)
            return

        mech_answer = evaluation.get("answer")
        confidence = evaluation.get("confidence", 0.0)
        agrees = evaluation.get("agrees_with_on_chain", True)

        if agrees:
            # Mech agrees with on-chain answer — mark as verified, no challenge
            self.context.logger.info(
                f"Question {question_id}: Mech agrees with on-chain answer "
                f"(confidence={confidence}). Marking as VERIFIED_OK."
            )
            entry["status"] = VERIFIED_OK
            questions_db[question_id] = entry
            yield from self._send_payload(Event.NONE, questions_db)
            return

        # Mech disagrees — check if we should challenge
        if confidence < self.params.challenge_confidence_threshold:
            self.context.logger.info(
                f"Question {question_id}: Mech disagrees but confidence "
                f"({confidence}) below threshold "
                f"({self.params.challenge_confidence_threshold}). Skipping."
            )
            entry["status"] = VERIFIED_OK  # treat low-confidence as "OK enough"
            questions_db[question_id] = entry
            yield from self._send_payload(Event.NONE, questions_db)
            return

        # Check escalation limit
        challenge = entry.get("challenge") or {}
        escalation_count = challenge.get("escalation_count", 0)
        if escalation_count >= self.params.max_escalation_rounds:
            self.context.logger.warning(
                f"Question {question_id}: max escalation rounds "
                f"({escalation_count}) reached. Giving up."
            )
            yield from self._send_payload(Event.NONE, questions_db)
            return

        # Check bond economics
        on_chain_bond = int(entry.get("on_chain_bond") or 0)
        if on_chain_bond == 0:
            # Unanswered question — use initial bond
            required_bond = self.params.initial_answer_bond
            max_previous = MAX_PREVIOUS_UNANSWERED
        else:
            # Challenge — double the current bond
            required_bond = on_chain_bond * 2
            max_previous = on_chain_bond

        if required_bond > self.params.max_challenge_bond:
            self.context.logger.warning(
                f"Question {question_id}: required bond ({required_bond}) "
                f"exceeds max ({self.params.max_challenge_bond}). Skipping."
            )
            yield from self._send_payload(Event.NONE, questions_db)
            return

        # TODO: check safe balance >= required_bond
        # TODO: build the actual submitAnswer transaction via contract API
        # For now, log the intent and update DB

        self.context.logger.info(
            f"Question {question_id}: building submitAnswer tx — "
            f"answer={mech_answer}, bond={required_bond}, "
            f"max_previous={max_previous}"
        )

        # Update DB entry
        entry["status"] = CHALLENGE_PENDING
        entry["challenge"] = {
            "tx_hash": None,  # will be set after tx settlement
            "bond": required_bond,
            "answer": mech_answer,
            "escalation_count": escalation_count + 1,
        }
        questions_db[question_id] = entry

        # TODO: emit DONE with actual tx data for TxSettlement
        # For now, emit NONE since we can't build the tx yet
        yield from self._send_payload(Event.NONE, questions_db)

    def _send_payload(
        self,
        event: Event,
        questions_db: Dict[str, Any],
    ) -> Generator:
        """Send the build challenges payload."""
        payload_data = json.dumps(
            {
                "event": event.value,
                "questions_db": json.dumps(questions_db),
            }
        )
        sender = self.context.agent_address
        payload = BuildChallengesTxPayload(sender=sender, content=payload_data)
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
