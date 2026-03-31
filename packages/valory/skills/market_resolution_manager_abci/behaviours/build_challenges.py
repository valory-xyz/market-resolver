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

import sys
from typing import Generator, Optional

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    BuildChallengesTxPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    BuildChallengesTxRound,
)

# Status constants
VERIFIED_OK = "VERIFIED_OK"
CHALLENGE_PENDING = "CHALLENGE_PENDING"

# Realitio answer encoding
MAX_PREVIOUS_UNANSWERED = 0


class BuildChallengesTxBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to build challenge/answer transactions.

    Payload convention:
    - questions_db=<json> → done_event → FinishedWithChallengeTxRound (tx built)
    - questions_db=None → none_event → CleanupTrackedMarketsRound (no tx needed)
    """

    matching_round = BuildChallengesTxRound

    def async_act(self) -> Generator:
        """Build challenge transaction or mark as verified."""
        market_id = self.synchronized_data.selected_market_id
        if market_id is None:
            self.context.logger.info("No selected question — nothing to build.")
            yield from self._send_payload(None)
            return

        questions_db = dict(self.questions_db)
        entry = questions_db.get(market_id)
        if entry is None:
            self.context.logger.error(f"Question {market_id} not found in DB.")
            yield from self._send_payload(None)
            return

        evaluation = entry.get("evaluation")
        if evaluation is None:
            self.context.logger.info(
                f"Question {market_id}: no evaluation data available."
            )
            yield from self._send_payload(None)
            return

        mech_answer = evaluation.get("answer")
        confidence = evaluation.get("confidence", 0.0)
        agrees = evaluation.get("agrees_with_on_chain", True)

        if agrees:
            self.context.logger.info(
                f"Question {market_id}: Mech agrees (confidence={confidence}). "
                f"Marking VERIFIED_OK."
            )
            entry["status"] = VERIFIED_OK
            questions_db[market_id] = entry
            yield from self._send_payload(None)
            return

        if confidence < self.params.challenge_confidence_threshold:
            self.context.logger.info(
                f"Question {market_id}: Mech disagrees but confidence "
                f"({confidence}) below threshold "
                f"({self.params.challenge_confidence_threshold}). Skipping."
            )
            entry["status"] = VERIFIED_OK
            questions_db[market_id] = entry
            yield from self._send_payload(None)
            return

        challenge = entry.get("challenge") or {}
        escalation_count = challenge.get("escalation_count", 0)
        if escalation_count >= self.params.max_escalation_rounds:
            self.context.logger.warning(
                f"Question {market_id}: max escalation rounds "
                f"({escalation_count}) reached."
            )
            yield from self._send_payload(None)
            return

        on_chain_bond = int(entry.get("on_chain_bond") or 0)
        if on_chain_bond == 0:
            required_bond = self.params.initial_answer_bond
            max_previous = MAX_PREVIOUS_UNANSWERED
        else:
            required_bond = on_chain_bond * 2
            max_previous = on_chain_bond

        if required_bond > self.params.max_challenge_bond:
            self.context.logger.warning(
                f"Question {market_id}: required bond ({required_bond}) "
                f"exceeds max ({self.params.max_challenge_bond})."
            )
            yield from self._send_payload(None)
            return

        # TODO: check safe balance >= required_bond
        # TODO: build actual submitAnswer tx via Realitio contract API

        self.context.logger.info(
            f"Question {market_id}: CHALLENGE — "
            f"answer={mech_answer}, bond={required_bond}, "
            f"max_previous={max_previous}"
        )

        # ---- DEBUG BREAK: stop before sending any real challenge tx ----
        self.context.logger.error(
            f"DEBUG BREAK: Would challenge question {market_id} "
            f"with answer={mech_answer}, bond={required_bond}. "
            f"Exiting to prevent actual tx submission."
        )
        sys.exit(1)
        # ---- END DEBUG BREAK ----

        entry["status"] = CHALLENGE_PENDING
        entry["challenge"] = {
            "tx_hash": None,
            "bond": required_bond,
            "answer": mech_answer,
            "escalation_count": escalation_count + 1,
        }
        questions_db[market_id] = entry

        # TODO: emit done_event with actual tx for TxSettlement
        # For now, emit none_event (no real tx built yet)
        yield from self._send_payload(None)

    def _send_payload(self, challenge_data: Optional[str]) -> Generator:
        """Send the build challenges payload."""
        # Save DB locally
        self.questions_db = dict(self.questions_db)
        sender = self.context.agent_address
        payload = BuildChallengesTxPayload(
            sender=sender,
            challenge_data=challenge_data,
        )
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
