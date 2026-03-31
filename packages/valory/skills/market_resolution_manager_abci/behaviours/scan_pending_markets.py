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

"""This module contains the ScanPendingMarketsBehaviour."""

import json
from typing import Any, Generator, Set, Type

from packages.valory.skills.abstract_round_abci.base import BaseTxPayload
from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    ScanPendingMarketsPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    ScanPendingMarketsRound,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import Event


class ScanPendingMarketsBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to scan pending markets and classify questions."""

    matching_round = ScanPendingMarketsRound

    def async_act(self) -> Generator:
        """Scan markets and classify questions."""
        self.context.logger.info("Scanning pending markets...")

        # Stub: no-op, emit NONE
        payload_data = json.dumps(
            {
                "event": Event.NONE.value,
                "questions_db": json.dumps(self.synchronized_data.questions_db),
                "selected_question_id": None,
                "selected_question_action": None,
            }
        )
        sender = self.context.agent_address
        payload = ScanPendingMarketsPayload(sender=sender, content=payload_data)
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
