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
from typing import Any, Dict, Generator, Optional
from uuid import uuid4

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
    is_cached_evaluation_valid,
    parse_mech_response,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    EvaluateAnswersPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    EvaluateAnswersRound,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import (
    AnswerStatus,
)
from packages.valory.skills.mech_interact_abci.states.base import MechMetadata

# GraphQL query template: prior Mech requests from our Safe for a given
# prompt, scoped to blocks after the market closed (openingTimestamp).
# Variables are inlined because the base helper only posts the `query` field.
#
# NOTE: the Mech Marketplace Gnosis subgraph stores the market question in
# ``parsedRequest.prompt``, NOT in ``parsedRequest.questionTitle`` (which is
# always empty for our requests). We filter on ``prompt`` accordingly.
MECH_CACHE_QUERY_TEMPLATE = """
{{
  sender(id: "{sender}") {{
    requests(
      where: {{
        parsedRequest_: {{ prompt: {prompt} }}
        blockTimestamp_gt: "{block_timestamp_gt}"
      }}
      orderBy: blockTimestamp
      orderDirection: desc
      first: 5
    ) {{
      id
      blockTimestamp
      parsedRequest {{
        prompt
        tool
      }}
      deliveries(first: 1) {{
        toolResponse
      }}
    }}
  }}
}}
"""


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

        # Re-challenge scenario: already have Mech data, skip Mech
        if (
            action == AnswerStatus.CHALLENGE_PENDING
            and entry.get("evaluation") is not None
        ):
            self.context.logger.info(
                f"Market {market_id}: reusing existing Mech evaluation "
                f"for re-challenge (skipping Mech request)."
            )
            yield from self._send_payload(None, AnswerStatus.CHALLENGE_PENDING)
            return

        # Subgraph cache lookup: only when local evaluation is missing.
        # Intended for service restart / state loss recovery.
        if entry.get("evaluation") is None:
            cached = yield from self._fetch_mech_response_from_subgraph(
                market_id, entry
            )
            if cached is not None:
                entry["evaluation"] = cached["evaluation"]
                entry["mech_response"] = cached["mech_response"]
                questions_db[market_id] = entry
                self.questions_db = questions_db

                mech_resp = cached["mech_response"]
                evaluation = cached["evaluation"]
                if is_cached_evaluation_valid(evaluation):
                    self.context.logger.info(
                        f"Market {market_id}: using cached Mech response "
                        f"from subgraph "
                        f"(request_id={mech_resp.get('subgraph_request_id')}, "
                        f"block_timestamp={mech_resp.get('block_timestamp')}, "
                        f"has_occurred={evaluation.get('has_occurred')}, "
                        f"answer={evaluation.get('answer')}, "
                        f"agreement_ratio={evaluation.get('agreement_ratio')}). "
                        f"Skipping fresh Mech request."
                    )
                    yield from self._send_payload(None, action)
                    return
                self.context.logger.info(
                    f"Market {market_id}: cached Mech response from subgraph "
                    f"is undeterminable "
                    f"(request_id={mech_resp.get('subgraph_request_id')}, "
                    f"block_timestamp={mech_resp.get('block_timestamp')}, "
                    f"is_valid={evaluation.get('is_valid')}, "
                    f"is_determinable={evaluation.get('is_determinable')}, "
                    f"has_occurred={evaluation.get('has_occurred')}). "
                    f"Stored; falling through to fresh Mech request."
                )

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

        # Increment retry counter
        entry["mech_retries"] = retries + 1
        entry["mech_request"] = asdict(mech_request)
        questions_db[market_id] = entry
        self.questions_db = questions_db

        # Send mech_requests -> done_event -> MechInteract
        yield from self._send_payload(mech_requests_json, None)

    def _fetch_mech_response_from_subgraph(  # pylint: disable=too-many-locals
        self, market_id: str, entry: Dict[str, Any]
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Look up a prior Mech response for this market from our Safe.

        Query the Mech Marketplace Gnosis subgraph for requests made by our
        Safe on the exact question title, with a block timestamp after the
        market closing time. Returns the latest matching request whose
        delivery parses cleanly and whose tool matches the configured resolver
        tool.

        :param market_id: the market id (for logging).
        :param entry: the DB entry for the market.
        :yield: None
        :return: dict with {"evaluation": ..., "mech_response": ...} on hit,
            or None on miss / error.
        """
        title = entry.get("title")
        closing_ts = entry.get("market_closing_timestamp")
        if not title or not closing_ts:
            return None

        safe_address = self.synchronized_data.safe_contract_address
        if not safe_address:
            return None

        # The subgraph indexes sender IDs in lowercase hex.
        sender_id = safe_address.lower()
        # json.dumps handles escaping of quotes/backslashes in the title.
        query = MECH_CACHE_QUERY_TEMPLATE.format(
            sender=sender_id,
            prompt=json.dumps(title),
            block_timestamp_gt=int(closing_ts),
        )

        self.context.logger.info(
            f"Market {market_id}: querying Mech Gnosis subgraph for prior "
            f"responses (sender={sender_id}, closing_ts={closing_ts})."
        )

        result = yield from self.get_mech_gnosis_subgraph_result(query)
        if result is None:
            return None

        sender_data = (result.get("data") or {}).get("sender")
        if not sender_data:
            self.context.logger.info(
                f"Market {market_id}: no prior Mech requests from our Safe "
                f"on the subgraph."
            )
            return None

        requests = sender_data.get("requests") or []
        expected_tool = self.params.mech_tool_resolve_market

        for req in requests:
            parsed = req.get("parsedRequest") or {}
            if parsed.get("tool") != expected_tool:
                continue
            if parsed.get("prompt") != title:
                continue
            deliveries = req.get("deliveries") or []
            if not deliveries:
                continue
            tool_response = deliveries[0].get("toolResponse")
            evaluation = parse_mech_response(tool_response)
            if evaluation is None:
                continue

            return {
                "evaluation": evaluation,
                "mech_response": {
                    "source": "subgraph",
                    "subgraph_request_id": req.get("id"),
                    "block_timestamp": int(req.get("blockTimestamp", 0)),
                    "result": tool_response,
                    "tool": parsed.get("tool"),
                },
            }

        self.context.logger.info(
            f"Market {market_id}: subgraph returned {len(requests)} prior "
            f"requests, none matched tool/title/delivery filters."
        )
        return None

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
