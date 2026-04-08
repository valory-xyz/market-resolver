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
