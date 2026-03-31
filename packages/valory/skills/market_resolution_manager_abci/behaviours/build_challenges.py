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
TRUSTED_ANSWER = "TRUSTED_ANSWER"
VERIFIED_OK = "VERIFIED_OK"
CHALLENGE_PENDING = "CHALLENGE_PENDING"

# Realitio answer encoding
ANSWER_NO = "0x0000000000000000000000000000000000000000000000000000000000000000"
ANSWER_YES = "0x0000000000000000000000000000000000000000000000000000000000000001"
MAX_PREVIOUS_UNANSWERED = 0


def _decode_answer(answer_hex: str) -> str:
    """Decode Realitio answer to human-readable label."""
    if answer_hex == ANSWER_NO:
        return "NO"
    if answer_hex == ANSWER_YES:
        return "YES"
    return answer_hex[:18] + "..."


class BuildChallengesTxBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to build challenge/answer transactions.

    Entered in two ways:
    - After MechInteract returns: reads mech_responses from SynchronizedData
    - From EvaluateAnswers (NONE event): existing evaluation in DB, skip Mech
    """

    matching_round = BuildChallengesTxRound

    def async_act(self) -> Generator:
        """Build challenge transaction or mark as verified."""
        market_id = self.synchronized_data.selected_market_id
        if market_id is None:
            self.context.logger.info("No selected market — nothing to build.")
            yield from self._send_payload(None)
            return

        questions_db = dict(self.questions_db)
        entry = questions_db.get(market_id)
        if entry is None:
            self.context.logger.error(f"Market {market_id} not found in DB.")
            yield from self._send_payload(None)
            return

        # Check if we have fresh Mech responses (from MechInteract)
        mech_responses = self.synchronized_data.mech_responses
        if mech_responses:
            # Find the response matching our market (by nonce)
            for resp in mech_responses:
                if resp.nonce == market_id:
                    self.context.logger.info(
                        f"Market {market_id}: received Mech response — "
                        f"result={resp.result}, error={resp.error}"
                    )
                    # Parse the Mech result
                    evaluation = self._parse_mech_response(resp.result)
                    if evaluation is not None:
                        entry["evaluation"] = evaluation
                        entry["mech_response"] = {
                            "nonce": resp.nonce,
                            "result": resp.result,
                            "error": resp.error,
                            "requestId": resp.requestId,
                        }
                    break

        evaluation = entry.get("evaluation")
        if evaluation is None:
            self.context.logger.info(
                f"Market {market_id}: no evaluation data available."
            )
            yield from self._send_payload(None)
            return

        mech_answer = evaluation.get("answer")
        confidence = evaluation.get("confidence", 0.0)
        on_chain_answer = entry.get("on_chain_answer")
        agrees = on_chain_answer == mech_answer if on_chain_answer else False

        # Update evaluation agreement
        evaluation["agrees_with_on_chain"] = agrees
        entry["evaluation"] = evaluation

        if on_chain_answer is not None and agrees:
            self.context.logger.info(
                f"Market {market_id}: Mech agrees with on-chain answer "
                f"({_decode_answer(on_chain_answer)}). Marking VERIFIED_OK."
            )
            entry["status"] = VERIFIED_OK
            questions_db[market_id] = entry
            self.questions_db = questions_db
            yield from self._send_payload(None)
            return

        if confidence < self.params.challenge_confidence_threshold:
            self.context.logger.info(
                f"Market {market_id}: Mech confidence ({confidence}) below "
                f"threshold ({self.params.challenge_confidence_threshold}). "
                f"Skipping."
            )
            entry["status"] = VERIFIED_OK
            questions_db[market_id] = entry
            self.questions_db = questions_db
            yield from self._send_payload(None)
            return

        challenge = entry.get("challenge") or {}
        escalation_count = challenge.get("escalation_count", 0)
        if escalation_count >= self.params.max_escalation_rounds:
            self.context.logger.warning(
                f"Market {market_id}: max escalation rounds "
                f"({escalation_count}) reached."
            )
            yield from self._send_payload(None)
            return

        on_chain_bond = int(entry.get("on_chain_bond") or 0)
        if on_chain_bond == 0:
            required_bond = self.params.initial_answer_bond
            max_previous = MAX_PREVIOUS_UNANSWERED
            action = "ANSWER"
        else:
            required_bond = on_chain_bond * 2
            max_previous = on_chain_bond
            action = "CHALLENGE"

        if required_bond > self.params.max_challenge_bond:
            self.context.logger.warning(
                f"Market {market_id}: required bond ({required_bond}) "
                f"exceeds max ({self.params.max_challenge_bond})."
            )
            yield from self._send_payload(None)
            return

        from web3 import Web3  # pylint: disable=import-outside-toplevel
        bond_xdai = Web3.from_wei(required_bond, "ether")
        answerer = entry.get("last_answerer", "unknown")
        question_id = entry.get("question_id", "unknown")

        self.context.logger.info(
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            f"║  {action} TX WOULD BE SENT"
            f"{' ' * (44 - len(action))}║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            f"║  Market:       {market_id[:46]}...║\n"
            f"║  Question:     {question_id[:46]}...║\n"
            f"║  On-chain:     {_decode_answer(on_chain_answer or 'none'):<46s}║\n"
            f"║  Answerer:     {answerer[:46]}...║\n"
            f"║  Mech answer:  {_decode_answer(mech_answer or 'none'):<46s}║\n"
            f"║  Confidence:   {str(confidence):<46s}║\n"
            f"║  Bond:         {str(bond_xdai) + ' xDAI':<46s}║\n"
            f"║  Max previous: {str(max_previous):<46s}║\n"
            f"║  Escalation:   #{str(escalation_count + 1):<45s}║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            "║  >>> BREAKPOINT: sys.exit(1) — tx NOT sent <<<             ║\n"
            "╚══════════════════════════════════════════════════════════════╝"
        )

        sys.exit(1)

        # --- BELOW NOT REACHED (breakpoint above) ---
        entry["status"] = CHALLENGE_PENDING
        entry["challenge"] = {
            "tx_hash": None,
            "bond": required_bond,
            "answer": mech_answer,
            "escalation_count": escalation_count + 1,
        }
        questions_db[market_id] = entry
        self.questions_db = questions_db
        yield from self._send_payload(None)

    def _parse_mech_response(self, result: Optional[str]) -> Optional[dict]:
        """Parse Mech result string into evaluation dict."""
        if result is None:
            return None
        try:
            data = json.loads(result)
            return {
                "answer": data.get("answer", data.get("prediction")),
                "confidence": data.get("confidence", data.get("p_yes", 0.5)),
                "agrees_with_on_chain": None,
                "reasoning": data.get("reasoning", str(data)),
            }
        except (json.JSONDecodeError, TypeError):
            self.context.logger.warning(
                f"Could not parse Mech result as JSON: {result[:100]}"
            )
            return {
                "answer": None,
                "confidence": 0.0,
                "agrees_with_on_chain": None,
                "reasoning": result,
            }

    def _send_payload(self, challenge_data: Optional[str]) -> Generator:
        """Send the build challenges payload."""
        self.questions_db = dict(self.questions_db)
        sender = self.context.agent_address
        payload = BuildChallengesTxPayload(
            sender=sender,
            challenge_data=challenge_data,
        )
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
