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

"""This module contains the CleanupTrackedMarketsBehaviour."""

from string import Template
from typing import Generator, Set

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    CleanupTrackedMarketsPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    CleanupTrackedMarketsRound,
)

# Query finalized markets by FPMM id
FINALIZED_MARKETS_QUERY = Template("""{
    fixedProductMarketMakers(
        where: {
            id_in: [${market_ids}]
            answerFinalizedTimestamp_gt: 0
            answerFinalizedTimestamp_lt: ${current_timestamp}
        }
        first: 1000
    ) {
        id
        answerFinalizedTimestamp
    }
}""")

SUBGRAPH_BATCH_SIZE = 100


class CleanupTrackedMarketsBehaviour(MarketResolutionManagerBaseBehaviour):
    """Purge finalized and verified markets from the database."""

    matching_round = CleanupTrackedMarketsRound

    def async_act(self) -> Generator:
        """Clean up finalized and verified markets."""
        questions_db = dict(self.questions_db)
        if not questions_db:
            self.context.logger.info("DB is empty, nothing to clean up.")
            yield from self._send_payload(0)
            return

        removed: Set[str] = set()
        now = self.last_synced_timestamp

        # Only remove markets that are FINALIZED on-chain.
        # VERIFIED markets stay in DB so scan recognizes them and skips
        # (otherwise they'd be re-discovered and trigger useless Mech requests).

        # Query subgraph for finalized markets (answerFinalizedTimestamp > 0 and < now)
        # Any finalized market is removed regardless of status -- no more answers accepted
        tracked_ids = list(questions_db.keys())
        if tracked_ids:
            for i in range(0, len(tracked_ids), SUBGRAPH_BATCH_SIZE):
                batch = tracked_ids[i : i + SUBGRAPH_BATCH_SIZE]
                ids_str = ", ".join(f'"{mid}"' for mid in batch)
                query = FINALIZED_MARKETS_QUERY.substitute(
                    market_ids=ids_str,
                    current_timestamp=now,
                )
                result = yield from self.get_omen_subgraph_result(query)
                if result is None:
                    self.context.logger.warning(
                        "Could not query subgraph for finalized markets. "
                        "Skipping on-chain cleanup."
                    )
                    break

                markets = result.get("data", {}).get("fixedProductMarketMakers", [])
                for market in markets:
                    mid = market.get("id")
                    if mid and mid in questions_db:
                        old_status = questions_db[mid].get("status", "?")
                        self.context.logger.info(
                            f"  Removing finalized market {mid} " f"(was {old_status})"
                        )
                        removed.add(mid)
                        del questions_db[mid]

        if removed:
            self.context.logger.info(
                f"Cleaned up {len(removed)} finalized market(s) from DB."
            )
        else:
            self.context.logger.info("No markets to clean up.")

        self.questions_db = questions_db
        yield from self._send_payload(len(removed))

    def _send_payload(self, n_cleaned: int) -> Generator:
        """Send cleanup payload."""
        sender = self.context.agent_address
        payload = CleanupTrackedMarketsPayload(
            sender=sender,
            n_cleaned=n_cleaned,
        )
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
