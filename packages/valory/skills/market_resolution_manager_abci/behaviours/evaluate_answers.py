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
from typing import Any, Dict, Generator, Optional

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    EvaluateAnswersPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    EvaluateAnswersRound,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import Event

# Status constants
NEEDS_EVALUATION = "NEEDS_EVALUATION"
CHALLENGE_PENDING = "CHALLENGE_PENDING"


class EvaluateAnswersBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to decide: request Mech or reuse existing evaluation data."""

    matching_round = EvaluateAnswersRound

    def async_act(self) -> Generator:
        """Build Mech request or skip to BuildChallenges if data exists."""
        question_id = self.synchronized_data.selected_question_id
        action = self.synchronized_data.selected_question_action

        if question_id is None:
            self.context.logger.info("No selected question — nothing to evaluate.")
            yield from self._send_payload(Event.NONE)
            return

        questions_db = self.synchronized_data.questions_db
        entry = questions_db.get(question_id)
        if entry is None:
            self.context.logger.error(
                f"Question {question_id} not found in DB."
            )
            yield from self._send_payload(Event.NONE)
            return

        # Check if we already have Mech evaluation data
        if action == CHALLENGE_PENDING and entry.get("evaluation") is not None:
            # Re-challenge scenario: skip Mech, go straight to BuildChallenges
            self.context.logger.info(
                f"Question {question_id}: reusing existing Mech evaluation "
                f"for re-challenge (skipping Mech request)."
            )
            yield from self._send_payload(Event.NONE)
            return

        # Check retry limit
        retries = entry.get("mech_retries", 0)
        if retries >= self.params.max_mech_retries:
            self.context.logger.warning(
                f"Question {question_id}: max Mech retries ({retries}) reached. "
                f"Skipping."
            )
            yield from self._send_payload(Event.NONE)
            return

        # Build Mech request
        question_data = entry.get("on_chain_answer")
        question_title = self._get_question_title(question_id)

        prompt = self._build_mech_prompt(
            question_id=question_id,
            question_title=question_title,
            current_answer=question_data,
            current_bond=entry.get("on_chain_bond"),
        )

        mech_request = {
            "prompt": prompt,
            "tool": self.params.mech_tool,
            "nonce": question_id,
        }

        self.context.logger.info(
            f"Question {question_id}: requesting Mech evaluation "
            f"with tool '{self.params.mech_tool}'."
        )

        # Store mech_requests for MechInteract to pick up
        # The MechInteract skill reads from synchronized_data.mech_requests
        mech_requests = json.dumps([mech_request])
        # TODO: write mech_requests to synchronized data via payload
        # For now, emit DONE to route to MechInteract
        yield from self._send_payload(Event.DONE)

    def _build_mech_prompt(
        self,
        question_id: str,
        question_title: Optional[str],
        current_answer: Optional[str],
        current_bond: Optional[str],
    ) -> str:
        """Build the Mech prompt for market evaluation."""
        parts = []
        if question_title:
            parts.append(f"Question: {question_title}")
        else:
            parts.append(f"Question ID: {question_id}")

        if current_answer is not None:
            parts.append(f"Current on-chain answer: {current_answer}")
            if current_bond:
                parts.append(f"Current bond: {current_bond} wei")
            parts.append(
                "Evaluate whether this answer is correct. "
                "Return your answer and confidence."
            )
        else:
            parts.append(
                "This question has no answer yet. "
                "Provide the correct answer with confidence."
            )

        return "\n".join(parts)

    def _get_question_title(self, question_id: str) -> Optional[str]:
        """Get the question title from the DB or subgraph data."""
        # TODO: store title in DB during scan phase
        return None

    def _send_payload(self, event: Event) -> Generator:
        """Send the evaluate payload."""
        payload_data = json.dumps({"event": event.value})
        sender = self.context.agent_address
        payload = EvaluateAnswersPayload(sender=sender, content=payload_data)
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
