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

"""This module contains the PostTransactionBehaviour."""

from typing import Generator

import packages.valory.skills.mech_interact_abci.states.request as MechRequestStates
from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    PostTransactionPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    BuildAnswerTxRound,
    PostTransactionRound,
)


class PostTransactionBehaviour(MarketResolutionManagerBaseBehaviour):
    """Route after TxSettlement based on which tx was submitted."""

    matching_round = PostTransactionRound

    def async_act(self) -> Generator:
        """Determine which tx was settled and emit the appropriate payload."""
        tx_submitter = self.synchronized_data.tx_submitter
        settled_tx_hash = self.synchronized_data.final_tx_hash

        self.context.logger.info(
            f"PostTransaction: tx_submitter={tx_submitter}, "
            f"settled_tx_hash={settled_tx_hash}"
        )

        if tx_submitter == MechRequestStates.MechRequestRound.auto_round_id():
            content = PostTransactionRound.MECH_REQUEST_DONE_PAYLOAD
        elif tx_submitter == BuildAnswerTxRound.auto_round_id():
            content = PostTransactionRound.ANSWER_TX_DONE_PAYLOAD
        else:
            # Recovery tx, funds forwarder, or unknown -- just proceed
            content = PostTransactionRound.ANSWER_TX_DONE_PAYLOAD

        sender = self.context.agent_address
        payload = PostTransactionPayload(sender=sender, content=content)
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
