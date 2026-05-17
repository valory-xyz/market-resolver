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
ANSWER_INVALID = "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

# GraphQL query template: prior Mech requests from our Safe for a given
# prompt, scoped to blocks after the market closed (openingTimestamp).
# Variables are inlined because the base helper only posts the `query` field.
#
# NOTE: the Mech Marketplace Gnosis subgraph stores the market question in
# ``parsedRequest.prompt``, NOT in ``parsedRequest.questionTitle`` (which is
# always empty for our requests). We filter on ``prompt`` accordingly.
#
# ``parsedRequest`` has no ``nonce`` field on this subgraph. Selecting it
# makes the whole query error with ``data: null`` -- a previous version of
# this template did exactly that, which silently disabled the retry-budget
# gate (the extractor walked ``data.sender.requests`` against ``null`` and
# got ``[]``, so ``len(requests) == 0`` every cycle no matter how many
# requests had really been fired). Keep the projection to ``prompt`` and
# ``tool`` only.
#
# Top-level ``requests`` (with ``sender:`` in the ``where`` clause) is
# equivalent to ``sender(id) { requests(...) }`` on this subgraph -- both
# return the same rows -- but the top-level form matches the pattern used
# by watchdog and trader, and is what the framework's ``response_key:
# data:requests`` walks.
MECH_CACHE_QUERY_TEMPLATE = """
{{
  requests(
    where: {{
      sender: "{sender}"
      parsedRequest_: {{ prompt: {prompt}, tool: "{tool}" }}
      blockTimestamp_gt: "{block_timestamp_gt}"
    }}
    orderBy: blockTimestamp
    orderDirection: asc
    first: 1000
  ) {{
    id
    blockTimestamp
    parsedRequest {{
      prompt
      tool
    }}
    deliveries(orderBy: blockTimestamp, orderDirection: asc) {{
      id
      blockTimestamp
      toolResponse
    }}
  }}
}}
"""


def parse_mech_response(  # pylint: disable=too-many-return-statements
    result: Optional[str],
) -> Optional[dict]:
    """Parse a resolve-market-jury-v1 Mech result with strict pattern matching.

    Only four exact ``(is_valid, is_determinable, has_occurred)`` patterns
    are recognised:

    - **Case A** ``(False, None, None)`` -> market is invalid, answer=INVALID
    - **Case B** ``(True, False, None)`` -> undeterminable, answer=None (retry)
    - **Case C1** ``(True, True, True)`` -> answer=YES
    - **Case C2** ``(True, True, False)`` -> answer=NO

    Any other combination is garbage (e.g. API errors returning
    ``(False, False, None)``) and returns ``None`` so callers retry.

    :param result: raw Mech response payload (JSON string) or ``None``.
    :return: parsed answer dict on a recognised pattern, else ``None``.
    """
    if result is None:
        return None
    try:
        data = json.loads(result)
        if not isinstance(data, dict):
            return None
        is_valid = data.get("is_valid")
        is_determinable = data.get("is_determinable")
        has_occurred = data.get("has_occurred")
        agreement_ratio = float(data.get("agreement_ratio") or 0.0)
        reasoning = data.get("judge_reasoning", "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return None

    def _make(answer: Optional[str]) -> dict:
        return {
            "answer": answer,
            "has_occurred": has_occurred,
            "is_valid": is_valid,
            "is_determinable": is_determinable,
            "agreement_ratio": agreement_ratio,
            "agrees_with_on_chain": None,
            "reasoning": reasoning,
        }

    # Case A: question is invalid -> submit INVALID answer
    if is_valid is False and is_determinable is None and has_occurred is None:
        return _make(ANSWER_INVALID)

    # Cases B, C1, C2 require is_valid=True
    if is_valid is not True:
        return None  # garbage

    # Case B: undeterminable -> retry later
    if is_determinable is False:
        return _make(None)

    # Case C1 / C2: determined
    if is_determinable is True and has_occurred is True:
        return _make(ANSWER_YES)
    if is_determinable is True and has_occurred is False:
        return _make(ANSWER_NO)

    # Anything else (e.g. is_determinable=True but has_occurred=None)
    return None


def jury_error_discriminator(result: Optional[str]) -> Optional[str]:
    """Return the jury's ``error`` discriminator if the payload reports one.

    The resolve-market-jury-v1 Mech tool emits a top-level ``error`` field
    on its off-contract / failure paths (``all_voters_failed``,
    ``judge_unparseable``, ``malformed_verdict``) alongside the
    ``(None, None, None)`` verdict tuple. ``parse_mech_response`` correctly
    routes these to the garbage path, but the discriminator itself is
    operationally valuable -- it lets the operator distinguish an API
    outage from a genuine parser failure. Returns ``None`` for non-JSON
    payloads, JSON without an ``error`` field, or non-dict JSON.

    :param result: raw Mech response payload (JSON string) or ``None``.
    :return: the ``error`` string if present, else ``None``.
    """
    if result is None:
        return None
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, str) and err:
        return err
    return None


def is_cached_evaluation_valid(evaluation: Optional[dict]) -> bool:
    """Return True if a cached evaluation has a definitive answer.

    Cases A (INVALID), C1 (YES), C2 (NO) have answer set and are cacheable.
    Case B (undeterminable, answer=None) and garbage (None) are not.

    :param evaluation: cached parse result from ``parse_mech_response``.
    :return: True if the evaluation carries a definitive answer.
    """
    if evaluation is None:
        return False
    return evaluation.get("answer") is not None


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

        try:
            return json.loads(response.body.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.context.logger.error(
                f"Omen subgraph returned 200 with non-JSON body: {exc}"
            )
            return None

    def get_mech_gnosis_subgraph_result(
        self,
        query: str,
    ) -> Generator[None, None, Optional[Any]]:
        """Query the Mech Marketplace Gnosis subgraph.

        :param query: the GraphQL query string.
        :yield: None
        :return: the parsed + key-walked response, or None on error.
        """
        subgraph = self.context.mech_gnosis_subgraph
        response = yield from self.get_http_response(
            content=to_content(query),
            **subgraph.get_spec(),
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

        return subgraph.process_response(response)

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

        try:
            return json.loads(response.body.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.context.logger.error(
                f"Realitio subgraph returned 200 with non-JSON body: {exc}"
            )
            return None

    def fetch_mech_requests_for_market(
        self, entry: Dict[str, Any]
    ) -> Generator[None, None, Optional[list]]:
        """Fetch Mech requests + deliveries for this market from the subgraph.

        Returns the verbatim subgraph request entries (each containing its
        nested ``deliveries`` list), filtered to those whose ``parsedRequest``
        matches the expected tool and the market title.

        :param entry: the per-market state dict.
        :yield: subgraph HTTP request.
        :return: list of matching verbatim subgraph request entries, or
            ``None`` on subgraph error.
        """
        title = entry.get("title") or ""
        closing_ts = entry.get("market_closing_timestamp") or 0
        safe_address = self.synchronized_data.safe_contract_address or ""

        expected_tool = self.params.mech_tool_resolve_market
        query = MECH_CACHE_QUERY_TEMPLATE.format(
            sender=safe_address.lower(),
            prompt=json.dumps(title),
            tool=expected_tool,
            block_timestamp_gt=int(closing_ts),
        )

        all_requests = yield from self.get_mech_gnosis_subgraph_result(query)
        if all_requests is None:
            return None

        return [
            req
            for req in all_requests
            if (req.get("parsedRequest") or {}).get("tool") == expected_tool
            and (req.get("parsedRequest") or {}).get("prompt") == title
        ]
