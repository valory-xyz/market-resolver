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

"""This module contains the handlers for the market resolution manager."""

from typing import Optional, cast

from aea.configurations.data_types import PublicId
from aea.protocols.base import Message

from packages.valory.protocols.kv_store.message import KvStoreMessage
from packages.valory.skills.abstract_round_abci.handlers import (
    ABCIRoundHandler as BaseABCIRoundHandler,
)
from packages.valory.skills.abstract_round_abci.handlers import (
    AbstractResponseHandler,
)
from packages.valory.skills.abstract_round_abci.handlers import (
    ContractApiHandler as BaseContractApiHandler,
)
from packages.valory.skills.abstract_round_abci.handlers import (
    HttpHandler as BaseHttpHandler,
)
from packages.valory.skills.abstract_round_abci.handlers import (
    IpfsHandler as BaseIpfsHandler,
)
from packages.valory.skills.abstract_round_abci.handlers import (
    LedgerApiHandler as BaseLedgerApiHandler,
)
from packages.valory.skills.abstract_round_abci.handlers import (
    SigningHandler as BaseSigningHandler,
)
from packages.valory.skills.abstract_round_abci.handlers import (
    TendermintHandler as BaseTendermintHandler,
)

ABCIHandler = BaseABCIRoundHandler
HttpHandler = BaseHttpHandler
SigningHandler = BaseSigningHandler
LedgerApiHandler = BaseLedgerApiHandler
ContractApiHandler = BaseContractApiHandler
TendermintHandler = BaseTendermintHandler
IpfsHandler = BaseIpfsHandler


class KvStoreHandler(AbstractResponseHandler):
    """Route kv_store replies to per-nonce callbacks registered by behaviours."""

    SUPPORTED_PROTOCOL: Optional[PublicId] = KvStoreMessage.protocol_id
    allowed_response_performatives = frozenset(
        {
            KvStoreMessage.Performative.READ_REQUEST,
            KvStoreMessage.Performative.CREATE_OR_UPDATE_REQUEST,
            KvStoreMessage.Performative.DELETE_REQUEST,
            KvStoreMessage.Performative.LIST_REQUEST,
            KvStoreMessage.Performative.READ_RESPONSE,
            KvStoreMessage.Performative.LIST_RESPONSE,
            KvStoreMessage.Performative.SUCCESS,
            KvStoreMessage.Performative.ERROR,
        }
    )

    def handle(self, message: Message) -> None:
        """Dispatch by dialogue nonce.

        Mirrors the optimus / mech pattern. A behaviour that fires a
        kv_store request registers ``(callback, kwargs)`` on
        ``state.req_to_callback`` keyed by the dialogue reference
        nonce, then yields until ``state.in_flight_req`` clears. The
        handler pops the callback, invokes it with the reply message,
        and clears the gate.

        :param message: the incoming kv_store message.
        """
        kv_store_msg = cast(KvStoreMessage, message)
        if kv_store_msg.performative not in self.allowed_response_performatives:
            self.context.logger.warning(
                f"KvStore performative not recognized: {kv_store_msg.performative}"
            )
            self.context.state.in_flight_req = False
            return

        if kv_store_msg.performative in (
            KvStoreMessage.Performative.SUCCESS,
            KvStoreMessage.Performative.READ_RESPONSE,
            KvStoreMessage.Performative.LIST_RESPONSE,
            KvStoreMessage.Performative.ERROR,
        ):
            nonce = kv_store_msg.dialogue_reference[0]
            callback, kwargs = self.context.state.req_to_callback.pop(
                nonce, (None, {})
            )
            if callback is not None:
                dialogue = self.context.kv_store_dialogues.update(kv_store_msg)
                callback(kv_store_msg, dialogue, **kwargs)
                self.context.state.in_flight_req = False
                return

        super().handle(message)
