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
import time
from dataclasses import asdict
from typing import Generator, Optional
from uuid import uuid4

from packages.valory.skills.market_resolution_manager_abci import mech_cache
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
        retries = int(entry.get("mech_retries") or 0)
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
        # Stashed so the delivery-side kv_store write (build_answer_tx)
        # can preserve fired_at without a READ round-trip. In-cycle only:
        # if the agent restarts between fire and delivery, questions_db
        # is rebuilt from scan_markets + the LIST'd kv rows themselves,
        # and this key isn't needed.
        fired_at = int(time.time())
        entry["mech_fired_at"] = fired_at
        questions_db[market_id] = entry
        self.questions_db = questions_db

        # Durable record of this fire so the next scan cycle sees "yes, I
        # already asked" without depending on the mech subgraph (which
        # under the off-chain path stops carrying prompt / toolResponse).
        # Best-effort: a kv_store failure here is logged but does not
        # block the mech request itself -- the FSM would just fall back
        # to the subgraph in Phase 1's coexistence window. Once the
        # subgraph read is removed in Phase 3, this write becomes the
        # single source of truth and a failure here means the next cycle
        # would re-fire the request, bounded by max_mech_retries.
        yield from self._buffer_mech_request_fired(
            market_id=market_id,
            nonce=nonce,
            prompt=prompt,
            fired_at=fired_at,
        )

        # Send mech_requests -> done_event -> MechInteract
        yield from self._send_payload(mech_requests_json, None)

    def _buffer_mech_request_fired(
        self,
        market_id: str,
        nonce: str,
        prompt: str,
        fired_at: int,
    ) -> Generator[None, None, None]:
        """Write the "just fired" row into kv_store.

        Ships dark alongside the subgraph read until Phase 3 removes the
        subgraph. Write failure is logged and swallowed so the FSM
        transitions into the mech request round regardless.

        :param market_id: the Omen market id being asked about.
        :param nonce: the uuid4 generated at fire time; used as the row PK
            AND for the later delivery match on ``resp.nonce``.
        :param prompt: the market title as sent to the mech.
        :param fired_at: epoch seconds; passed in rather than sampled here
            so the entry-side stash and the kv row share the same value.
        :yield: control to the FSM while the kv write is in flight.
        """
        safe_address = self.synchronized_data.safe_contract_address or ""
        key = mech_cache.cache_key(
            prefix=self.params.mech_cache_key_prefix,
            safe_address=safe_address,
            market_id=market_id,
            nonce=nonce,
        )
        value = mech_cache.serialize_row(
            safe_address=safe_address,
            market_id=market_id,
            nonce=nonce,
            tool=self.params.mech_tool_resolve_market,
            prompt=prompt,
            fired_at=fired_at,
        )
        ok = yield from self._send_kv_write(key=key, value=value)
        if not ok:
            self.context.logger.warning(
                "kv_store write for market=%s nonce=%s failed; the next "
                "scan cycle may re-request this market. Bounded by "
                "max_mech_retries.",
                market_id,
                nonce,
            )

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
