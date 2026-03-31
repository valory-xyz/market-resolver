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

"""This module contains the CleanupTrackingBehaviour."""

import json
from typing import Generator

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    CleanupTrackingPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    CleanupTrackingRound,
)


class CleanupTrackingBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to purge finalized questions from the database."""

    matching_round = CleanupTrackingRound

    def async_act(self) -> Generator:
        """Clean up finalized questions."""
        self.context.logger.info("Cleaning up tracking database...")

        # Stub: no-op, pass through current DB
        payload_data = json.dumps(
            {"questions_db": json.dumps(self.synchronized_data.questions_db)}
        )
        sender = self.context.agent_address
        payload = CleanupTrackingPayload(sender=sender, content=payload_data)
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
