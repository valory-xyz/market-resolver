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

"""This module contains the EvaluateAnswersBehaviour."""

import json
from typing import Generator, Optional

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    EvaluateAnswersPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    EvaluateAnswersRound,
)

# Status constants
NEEDS_EVALUATION = "NEEDS_EVALUATION"
CHALLENGE_PENDING = "CHALLENGE_PENDING"

# Simulated Mech answer: always NO
# Realitio encoding: 0x00...00 = NO, 0x00...01 = YES
SIMULATED_MECH_ANSWER = (
    "0x0000000000000000000000000000000000000000000000000000000000000000"
)
SIMULATED_MECH_CONFIDENCE = 0.92


class EvaluateAnswersBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to decide: request Mech or reuse existing evaluation data.

    Currently simulates Mech by hardcoding answer=NO.
    TODO: integrate with MechInteract for real AI evaluation.

    Payload convention (CollectSameUntilThresholdRound):
    - questions_db=<json> → done_event → BuildChallengesTxRound (has evaluation data)
    - questions_db=None → none_event → FinishedWithMechRequestRound (needs real Mech)
    """

    matching_round = EvaluateAnswersRound

    def async_act(self) -> Generator:
        """Evaluate the selected question — simulate Mech or reuse existing data."""
        market_id = self.synchronized_data.selected_market_id
        action = self.synchronized_data.selected_market_action

        if market_id is None:
            self.context.logger.info("No selected question — nothing to evaluate.")
            yield from self._send_payload(None)
            return

        questions_db = dict(self.questions_db)
        entry = questions_db.get(market_id)
        if entry is None:
            self.context.logger.error(
                f"Question {market_id} not found in DB."
            )
            yield from self._send_payload(None)
            return

        # Re-challenge scenario: already have Mech data, skip to BuildChallenges
        if action == CHALLENGE_PENDING and entry.get("evaluation") is not None:
            self.context.logger.info(
                f"Question {market_id}: reusing existing Mech evaluation "
                f"for re-challenge (skipping Mech request)."
            )
            yield from self._send_payload(CHALLENGE_PENDING)
            return

        # Check retry limit
        retries = entry.get("mech_retries", 0)
        if retries >= self.params.max_mech_retries:
            self.context.logger.warning(
                f"Question {market_id}: max Mech retries ({retries}) reached."
            )
            yield from self._send_payload(None)
            return

        # --- SIMULATED MECH EVALUATION ---
        # TODO: replace with real MechInteract (send questions_db to trigger done_event)
        on_chain_answer = entry.get("on_chain_answer")
        agrees = on_chain_answer == SIMULATED_MECH_ANSWER

        on_chain_label = (
            "NO" if on_chain_answer == SIMULATED_MECH_ANSWER
            else "YES" if on_chain_answer else "unanswered"
        )
        self.context.logger.info(
            f"Question {market_id}: [SIMULATED MECH] answer=NO, "
            f"confidence={SIMULATED_MECH_CONFIDENCE}, "
            f"on_chain={on_chain_label}, agrees={agrees}"
        )

        entry["evaluation"] = {
            "answer": SIMULATED_MECH_ANSWER,
            "confidence": SIMULATED_MECH_CONFIDENCE,
            "agrees_with_on_chain": agrees,
            "reasoning": "[SIMULATED] Mech always answers NO for testing.",
        }
        entry["mech_request"] = {
            "prompt": f"[SIMULATED] Evaluate question {market_id}",
            "tool": self.params.mech_tool,
            "nonce": market_id,
        }
        entry["mech_response"] = {
            "nonce": market_id,
            "data": "simulated",
            "requestId": 0,
            "result": json.dumps({
                "answer": SIMULATED_MECH_ANSWER,
                "confidence": SIMULATED_MECH_CONFIDENCE,
            }),
            "error": "Unknown",
        }

        if on_chain_answer is None:
            entry["status"] = CHALLENGE_PENDING
            self.context.logger.info(
                f"Question {market_id}: unanswered, will submit answer NO."
            )
        elif agrees:
            entry["status"] = "VERIFIED_OK"
            self.context.logger.info(
                f"Question {market_id}: Mech agrees. Marked VERIFIED_OK."
            )
        else:
            entry["status"] = CHALLENGE_PENDING
            entry["detected_at"] = self.last_synced_timestamp
            self.context.logger.info(
                f"Question {market_id}: Mech DISAGREES. "
                f"Marked CHALLENGE_PENDING."
            )

        questions_db[market_id] = entry
        self.questions_db = questions_db

        # done_event → BuildChallengesTxRound
        yield from self._send_payload(entry["status"])

    def _send_payload(self, result: Optional[str]) -> Generator:
        """Send the evaluate payload.

        result=<status> → done_event → BuildChallengesTxRound
        result=None → none_event → FinishedWithMechRequestRound
        """
        sender = self.context.agent_address
        payload = EvaluateAnswersPayload(
            sender=sender,
            evaluation_result=result,
        )
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
