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

"""This module contains the base behaviour for the market resolution manager."""

import json
from abc import ABC
from typing import Any, Dict, Generator, Optional, cast

from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.behaviours import BaseBehaviour
from packages.valory.skills.market_resolution_manager_abci.models import (
    MarketResolutionManagerParams,
    SharedState,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    SynchronizedData,
)

HTTP_OK = 200

# Realitio answer encoding (outcome index for binary Yes/No markets).
ANSWER_YES = "0x0000000000000000000000000000000000000000000000000000000000000000"
ANSWER_NO = "0x0000000000000000000000000000000000000000000000000000000000000001"

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
      orderDirection: asc
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


def parse_mech_response(result: Optional[str]) -> Optional[dict]:
    """Parse a resolve-market-jury-v1 Mech result into an evaluation dict.

    Expected result JSON schema::

        {
            "is_valid": true/false,
            "is_determinable": true/false,
            "has_occurred": true/false/null,
            "votes": [...],
            "judge_reasoning": "...",
            "agreement_ratio": 0.0-1.0
        }

    :param result: raw string (JSON) returned by the Mech tool, or None.
    :return: evaluation dict (with "answer" set to the Realitio-encoded hex
        for determinable results, or None for undeterminable/invalid), or
        None if the payload is absent or unparseable.
    """
    if result is None:
        return None
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None

    is_valid = data.get("is_valid", False)
    is_determinable = data.get("is_determinable", False)
    has_occurred = data.get("has_occurred")
    agreement_ratio = float(data.get("agreement_ratio", 0.0))
    reasoning = data.get("judge_reasoning", "")

    if not is_valid or not is_determinable or has_occurred is None:
        return {
            "answer": None,
            "has_occurred": has_occurred,
            "is_valid": is_valid,
            "is_determinable": is_determinable,
            "agreement_ratio": agreement_ratio,
            "agrees_with_on_chain": None,
            "reasoning": reasoning,
        }

    mech_answer = ANSWER_YES if has_occurred else ANSWER_NO

    return {
        "answer": mech_answer,
        "has_occurred": has_occurred,
        "is_valid": is_valid,
        "is_determinable": is_determinable,
        "agreement_ratio": agreement_ratio,
        "agrees_with_on_chain": None,
        "reasoning": reasoning,
    }


def is_cached_evaluation_valid(evaluation: Optional[dict]) -> bool:
    """Return True if a cached evaluation is definitive (determinable).

    A "valid" cached evaluation blocks a fresh Mech request. Undeterminable
    or invalid cached evaluations are stored but do not block new calls.

    :param evaluation: the evaluation dict produced by parse_mech_response.
    :return: True if the evaluation is determinable and actionable.
    """
    if evaluation is None:
        return False
    return bool(
        evaluation.get("is_valid")
        and evaluation.get("is_determinable")
        and evaluation.get("has_occurred") is not None
    )


def to_content(query: str) -> bytes:
    """Convert the given query string to payload content."""
    finalized_query = {"query": query}
    encoded_query = json.dumps(finalized_query, sort_keys=True).encode("utf-8")
    return encoded_query


class MarketResolutionManagerBaseBehaviour(BaseBehaviour, ABC):
    """Base behaviour for the market resolution manager skill."""

    @property
    def synchronized_data(self) -> SynchronizedData:
        """Return the synchronized data."""
        return cast(SynchronizedData, super().synchronized_data)

    @property
    def params(self) -> MarketResolutionManagerParams:
        """Return the params."""
        return cast(MarketResolutionManagerParams, super().params)

    @property
    def questions_db(self) -> Dict[str, Any]:
        """Get the questions database from shared state."""
        return self.context.state.questions_db

    @questions_db.setter
    def questions_db(self, value: Dict[str, Any]) -> None:
        """Set the questions database on shared state."""
        self.context.state.questions_db = value

    @property
    def last_synced_timestamp(self) -> int:
        """Get last synced timestamp."""
        state = cast(SharedState, self.context.state)
        last_timestamp = (
            state.round_sequence.last_round_transition_timestamp.timestamp()
        )
        return int(last_timestamp)

    def get_native_balance(self, address: str) -> Generator[None, None, Optional[int]]:
        """Get the native xDAI balance of the provided address."""
        ledger_api_response = yield from self.get_ledger_api_response(
            performative=LedgerApiMessage.Performative.GET_STATE,  # type: ignore
            ledger_callable="get_balance",
            account=address,
            chain_id=self.params.default_chain_id,
        )
        if ledger_api_response.performative != LedgerApiMessage.Performative.STATE:
            self.context.logger.error(
                f"Could not get balance for {address}. "
                f"Expected STATE, got {ledger_api_response.performative.value}."
            )
            return None
        balance = cast(int, ledger_api_response.state.body.get("get_balance_result"))
        return balance

    def get_omen_subgraph_result(
        self,
        query: str,
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Query the Omen subgraph.

        :param query: the GraphQL query string.
        :yield: None
        :return: the parsed JSON response, or None on error.
        """
        response = yield from self.get_http_response(
            content=to_content(query),
            **self.context.omen_subgraph.get_spec(),
        )

        if response is None:
            self.context.logger.error(
                "Could not retrieve response from Omen subgraph. Response was None."
            )
            return None
        if response.status_code != HTTP_OK:
            self.context.logger.error(
                f"Could not retrieve response from Omen subgraph. "
                f"Received status code {response.status_code}.\n{response}"
            )
            return None

        return json.loads(response.body.decode())

    def get_mech_gnosis_subgraph_result(
        self,
        query: str,
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Query the Mech Marketplace Gnosis subgraph.

        :param query: the GraphQL query string.
        :yield: None
        :return: the parsed JSON response, or None on error.
        """
        response = yield from self.get_http_response(
            content=to_content(query),
            **self.context.mech_gnosis_subgraph.get_spec(),
        )

        if response is None:
            self.context.logger.error(
                "Could not retrieve response from Mech Gnosis subgraph. "
                "Response was None."
            )
            return None
        if response.status_code != HTTP_OK:
            self.context.logger.error(
                f"Could not retrieve response from Mech Gnosis subgraph. "
                f"Received status code {response.status_code}.\n{response}"
            )
            return None

        return json.loads(response.body.decode())

    def get_realitio_subgraph_result(
        self,
        query: str,
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Query the Realitio subgraph.

        :param query: the GraphQL query string.
        :yield: None
        :return: the parsed JSON response, or None on error.
        """
        response = yield from self.get_http_response(
            content=to_content(query),
            **self.context.realitio_subgraph.get_spec(),
        )

        if response is None:
            self.context.logger.error(
                "Could not retrieve response from Realitio subgraph. "
                "Response was None."
            )
            return None
        if response.status_code != HTTP_OK:
            self.context.logger.error(
                f"Could not retrieve response from Realitio subgraph. "
                f"Received status code {response.status_code}.\n{response}"
            )
            return None

        return json.loads(response.body.decode())

    def find_cached_valid_mech_request(  # pylint: disable=too-many-locals
        self, market_id: str, entry: Dict[str, Any]
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Look up a prior valid Mech response for this market from our Safe.

        Query the Mech Marketplace Gnosis subgraph for requests made by our
        Safe on the exact question title, with a block timestamp after the
        market closing time. Returns the earliest matching request whose
        delivery parses cleanly, matches the configured resolver tool, and
        yields a valid (determinable) evaluation.

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
            if not is_cached_evaluation_valid(evaluation):
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
            f"requests, none matched tool/title/delivery/validity filters."
        )
        return None
