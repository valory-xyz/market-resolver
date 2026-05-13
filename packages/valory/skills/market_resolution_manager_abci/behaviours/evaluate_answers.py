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
from dataclasses import asdict
from typing import Generator, Optional
from uuid import uuid4

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    EvaluateAnswersPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    EvaluateAnswersRound,
)
from packages.valory.skills.mech_interact_abci.states.base import MechMetadata


class EvaluateAnswersBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to decide: request Mech or reuse existing evaluation data.

    Payload convention (custom end_block in EvaluateAnswersRound):
    - mech_requests=<json> -> done_event -> FinishedWithMechRequestRound -> MechInteract
    - evaluation_result=<status> -> none_event -> BuildAnswerTxRound (skip Mech)
    """

    matching_round = EvaluateAnswersRound

    def async_act(self) -> Generator:
        """Evaluate the selected market -- request Mech or reuse existing data."""
        market_id = self.synchronized_data.selected_market_id
        action = self.synchronized_data.selected_market_action

        if market_id is None:
            self.context.logger.info("No selected market -- nothing to evaluate.")
            yield from self._send_payload(None, None)
            return

        questions_db = dict(self.questions_db)
        entry = questions_db.get(market_id)
        if entry is None:
            self.context.logger.error(f"Market {market_id} not found in DB.")
            yield from self._send_payload(None, None)
            return

        # Reuse existing evaluation (populated by scan_markets from the
        # Mech subgraph cache, or from a prior cycle's Mech response).
        if entry.get("evaluation") is not None:
            self.context.logger.info(
                f"Market {market_id}: reusing existing Mech evaluation "
                f"(action={action}), skipping Mech request."
            )
            yield from self._send_payload(None, action)
            return

        # Check retry limit
        retries = entry.get("mech_retries", 0)
        if retries >= self.params.max_mech_retries:
            self.context.logger.warning(
                f"Market {market_id}: max Mech retries ({retries}) reached."
            )
            yield from self._send_payload(None, None)
            return

        # Build Mech request -- prompt is the market title (human-readable question)
        title = entry.get("title", "")
        if not title:
            self.context.logger.error(
                f"Market {market_id}: no title in DB entry. Cannot request Mech."
            )
            yield from self._send_payload(None, None)
            return
        prompt = title

        nonce = str(uuid4())
        mech_request = MechMetadata(
            nonce=nonce,
            tool=self.params.mech_tool_resolve_market,
            prompt=prompt,
        )

        self.context.logger.info(
            f"Market {market_id}: requesting Mech evaluation "
            f"with tool '{self.params.mech_tool_resolve_market}', "
            f"nonce={nonce}, prompt={prompt[:60]}..."
        )

        mech_requests_json = json.dumps([asdict(mech_request)], sort_keys=True)

        # Increment retry counter immediately (local fact: we just fired a
        # request). scan_markets later does
        # ``mech_retries = max(mech_retries, len(mech_requests_from_subgraph))``
        # to converge once the subgraph indexes this request.
        entry["mech_retries"] = retries + 1
        # ``pending_nonce`` lets build_answer_tx match the in-process
        # MechInteract delivery (``SynchronizedData.mech_responses``) back to
        # this market before the subgraph has indexed the new request.
        entry["pending_nonce"] = nonce
        questions_db[market_id] = entry
        self.questions_db = questions_db

        # Send mech_requests -> done_event -> MechInteract
        yield from self._send_payload(mech_requests_json, None)

    def _send_payload(
        self,
        mech_requests: Optional[str],
        evaluation_result: Optional[str],
    ) -> Generator:
        """Send the evaluate payload."""
        sender = self.context.agent_address
        payload = EvaluateAnswersPayload(
            sender=sender,
            mech_requests=mech_requests,
            evaluation_result=evaluation_result,
        )
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
