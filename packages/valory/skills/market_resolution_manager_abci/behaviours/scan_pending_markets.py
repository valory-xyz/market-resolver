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

from string import Template
from typing import Any, Dict, Generator, List, Optional

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    ScanPendingMarketsPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    ScanPendingMarketsRound,
)

# Status constants
TRUSTED_ANSWER = "TRUSTED_ANSWER"
NEEDS_EVALUATION = "NEEDS_EVALUATION"
VERIFIED_OK = "VERIFIED_OK"
CHALLENGE_PENDING = "CHALLENGE_PENDING"

# Omen subgraph query: pending markets from watched creators
# answerFinalizedTimestamp: null = not yet finalized
# openingTimestamp_lt: now = market is past opening time
# Pending markets (no answer yet)
PENDING_MARKETS_QUERY = Template(
    """{
    fixedProductMarketMakers(
        where: {
            creator_in: [${creators}]
            openingTimestamp_lt: ${current_timestamp}
            answerFinalizedTimestamp: null
        }
        first: 1000
        orderBy: openingTimestamp
        orderDirection: asc
    ) {
        id
        creator
        currentAnswer
        currentAnswerBond
        currentAnswerTimestamp
        openingTimestamp
        timeout
        question {
            id
            data
            currentAnswerBond
        }
        title
    }
}"""
)

# Finalizing markets (answered but finalization still in the future — can still be challenged)
FINALIZING_MARKETS_QUERY = Template(
    """{
    fixedProductMarketMakers(
        where: {
            creator_in: [${creators}]
            openingTimestamp_lt: ${current_timestamp}
            answerFinalizedTimestamp_gt: ${current_timestamp}
        }
        first: 1000
        orderBy: openingTimestamp
        orderDirection: asc
    ) {
        id
        creator
        currentAnswer
        currentAnswerBond
        currentAnswerTimestamp
        openingTimestamp
        timeout
        question {
            id
            data
            currentAnswerBond
        }
        title
    }
}"""
)

# Realitio subgraph query: get latest answerer for each question
LATEST_ANSWERERS_QUERY = Template(
    """{
    questions(
        where: {questionId_in: [${question_ids}]}
        first: 1000
    ) {
        questionId
        responses(orderBy: timestamp, orderDirection: desc, first: 1) {
            user
            timestamp
        }
    }
}"""
)

SUBGRAPH_BATCH_SIZE = 1000


class ScanPendingMarketsBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to scan pending markets and classify questions."""

    matching_round = ScanPendingMarketsRound

    def async_act(self) -> Generator:
        """Scan markets and classify questions."""
        watched = self.params.watched_creator_addresses
        if not watched:
            self.context.logger.info("No watched creator addresses configured.")
            yield from self._send_none_payload()
            return

        # Step 1: Query Omen subgraph for pending markets
        markets = yield from self._fetch_pending_markets(watched)
        if markets is None or len(markets) == 0:
            self.context.logger.info("No pending markets found.")
            yield from self._send_none_payload()
            return

        self.context.logger.info(f"Found {len(markets)} pending market(s).")

        # Step 2: Query Realitio subgraph for latest answerers
        question_ids = [m["question"]["id"] for m in markets if m.get("currentAnswer")]
        answerers = yield from self._fetch_latest_answerers(question_ids)

        # Step 3: Classify questions and update DB
        questions_db = dict(self.questions_db)
        trusted = set(
            addr.lower() for addr in self.params.trusted_addresses
        )
        trusted.add(self.synchronized_data.safe_contract_address.lower())

        actionable: List[Dict[str, Any]] = []
        now = self.last_synced_timestamp

        for market in markets:
            market_id = market["id"]
            question_id = market["question"]["id"]
            current_answer = market.get("currentAnswer")
            current_bond = market.get("currentAnswerBond")
            current_answer_ts = market.get("currentAnswerTimestamp")
            timeout = int(market.get("timeout", 86400))
            latest_answerer = answerers.get(question_id, "").lower()

            # Re-scan: update existing DB entries if answer changed
            if question_id in questions_db:
                entry = questions_db[market_id]
                stored_ts = entry.get("last_answer_timestamp")

                if current_answer_ts and stored_ts != current_answer_ts:
                    # Answer changed on-chain
                    if latest_answerer in trusted:
                        questions_db[market_id] = self._update_entry(
                            entry, market, latest_answerer, TRUSTED_ANSWER
                        )
                        continue
                    elif entry.get("evaluation") is not None:
                        # Have prior Mech data — go to CHALLENGE_PENDING
                        questions_db[market_id] = self._update_entry(
                            entry, market, latest_answerer, CHALLENGE_PENDING
                        )
                    else:
                        # Need fresh evaluation
                        questions_db[market_id] = self._update_entry(
                            entry, market, latest_answerer, NEEDS_EVALUATION
                        )
                # else: answer unchanged, keep current status

                entry = questions_db[market_id]
            else:
                # New question — not in DB yet
                if current_answer is None:
                    # Unanswered — needs initial answer via Mech
                    entry = self._new_entry(market, "", NEEDS_EVALUATION, now)
                    questions_db[market_id] = entry
                elif latest_answerer in trusted:
                    # Trusted answerer — track but no action
                    entry = self._new_entry(
                        market, latest_answerer, TRUSTED_ANSWER, now
                    )
                    questions_db[market_id] = entry
                    continue
                else:
                    # Non-trusted answerer — needs evaluation
                    entry = self._new_entry(
                        market, latest_answerer, NEEDS_EVALUATION, now
                    )
                    questions_db[market_id] = entry

            # Determine if actionable
            status = entry["status"]
            if status == NEEDS_EVALUATION:
                actionable.append(
                    {"market_id": market_id, "action": NEEDS_EVALUATION}
                )
            elif status == CHALLENGE_PENDING:
                cooldown = timeout * self.params.challenge_cooldown_fraction
                detected_at = entry.get("detected_at", 0)
                finalization_deadline = (
                    int(entry.get("last_answer_timestamp") or 0) + timeout
                )

                if now >= finalization_deadline:
                    # Too late — answer already finalized
                    continue

                urgency = now >= finalization_deadline - self.params.challenge_urgency_buffer
                cooldown_elapsed = now >= detected_at + cooldown

                if urgency or cooldown_elapsed:
                    actionable.append(
                        {"market_id": market_id, "action": CHALLENGE_PENDING}
                    )

        # Sort: unanswered first, then by finalization urgency
        def sort_key(item: Dict[str, Any]) -> tuple:
            entry = questions_db[item["market_id"]]
            is_unanswered = entry.get("on_chain_answer") is None
            last_ts = int(entry.get("last_answer_timestamp") or 0)
            timeout = int(entry.get("realitio_timeout", 86400))
            finalization = last_ts + timeout if last_ts else float("inf")
            return (0 if is_unanswered else 1, finalization)

        actionable.sort(key=sort_key)

        # Log summary
        n_trusted = sum(
            1 for e in questions_db.values() if e["status"] == TRUSTED_ANSWER
        )
        n_verified = sum(
            1 for e in questions_db.values() if e["status"] == "VERIFIED_OK"
        )
        n_eval = sum(
            1 for e in questions_db.values() if e["status"] == NEEDS_EVALUATION
        )
        n_challenge = sum(
            1 for e in questions_db.values() if e["status"] == CHALLENGE_PENDING
        )
        self.context.logger.info(
            f"Scan complete: {len(questions_db)} markets tracked — "
            f"{n_trusted} trusted, {n_verified} verified, "
            f"{n_eval} need evaluation, {n_challenge} challenge pending"
        )
        for mid, entry in questions_db.items():
            status = entry["status"]
            if status in (TRUSTED_ANSWER, "VERIFIED_OK"):
                continue
            answer = entry.get("on_chain_answer") or "unanswered"
            bond = entry.get("on_chain_bond") or "0"
            answerer = entry.get("last_answerer", "")[:10]
            self.context.logger.info(
                f"  [{status}] {mid[:18]}... "
                f"answer={answer} bond={bond} answerer={answerer}..."
            )

        if not actionable:
            self.context.logger.info("No actionable questions found.")
            yield from self._send_payload(questions_db)
            return

        # Pick the first actionable market
        selected = actionable[0]
        self.context.logger.info(
            f"Selected market {selected['market_id']} "
            f"for action: {selected['action']}"
        )
        yield from self._send_payload(
            questions_db,
            selected["market_id"],
            selected["action"],
        )

    def _fetch_pending_markets(
        self, watched: List[str]
    ) -> Generator[None, None, Optional[List[Dict[str, Any]]]]:
        """Fetch pending + finalizing markets from Omen subgraph.

        Two queries:
        1. Pending: answerFinalizedTimestamp is null (no answer yet)
        2. Finalizing: answerFinalizedTimestamp > now (answered but not yet finalized)
        """
        creators_str = ", ".join(f'"{c.lower()}"' for c in watched)
        now = self.last_synced_timestamp

        # Query 1: pending (unanswered)
        query1 = PENDING_MARKETS_QUERY.substitute(
            creators=creators_str,
            current_timestamp=now,
        )
        result1 = yield from self.get_omen_subgraph_result(query1)
        pending = (
            result1.get("data", {}).get("fixedProductMarketMakers", [])
            if result1 else []
        )

        # Query 2: finalizing (answered, finalization in the future)
        query2 = FINALIZING_MARKETS_QUERY.substitute(
            creators=creators_str,
            current_timestamp=now,
        )
        result2 = yield from self.get_omen_subgraph_result(query2)
        finalizing = (
            result2.get("data", {}).get("fixedProductMarketMakers", [])
            if result2 else []
        )

        # Merge and deduplicate by market id
        seen = set()
        markets = []
        for m in pending + finalizing:
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                markets.append(m)

        self.context.logger.info(
            f"Fetched {len(pending)} pending + "
            f"{len(finalizing)} finalizing = "
            f"{len(markets)} total pending markets."
        )
        return markets

    def _fetch_latest_answerers(
        self, question_ids: List[str]
    ) -> Generator[None, None, Dict[str, str]]:
        """Fetch latest answerer for each question from Realitio subgraph."""
        answerers: Dict[str, str] = {}
        if not question_ids:
            return answerers

        for i in range(0, len(question_ids), SUBGRAPH_BATCH_SIZE):
            batch = question_ids[i: i + SUBGRAPH_BATCH_SIZE]
            ids_str = ", ".join(f'"{qid}"' for qid in batch)
            query = LATEST_ANSWERERS_QUERY.substitute(question_ids=ids_str)
            result = yield from self.get_realitio_subgraph_result(query)
            if result is None:
                continue
            for q in result.get("data", {}).get("questions", []):
                responses = q.get("responses", [])
                if responses:
                    answerers[q["questionId"]] = responses[0]["user"].lower()

        return answerers

    def _new_entry(
        self,
        market: Dict[str, Any],
        answerer: str,
        status: str,
        now: int,
    ) -> Dict[str, Any]:
        """Create a new questions_db entry."""
        return {
            "status": status,
            "market_id": market.get("id"),
            "question_id": market.get("question", {}).get("id"),
            "detected_at": now,
            "on_chain_answer": market.get("currentAnswer"),
            "on_chain_bond": market.get("currentAnswerBond"),
            "last_answerer": answerer,
            "last_answer_timestamp": market.get("currentAnswerTimestamp"),
            "market_closing_timestamp": int(market.get("openingTimestamp", 0)),
            "realitio_timeout": int(market.get("timeout", 86400)),
            "mech_request": None,
            "mech_response": None,
            "evaluation": None,
            "challenge": None,
            "mech_retries": 0,
        }

    def _update_entry(
        self,
        entry: Dict[str, Any],
        market: Dict[str, Any],
        answerer: str,
        new_status: str,
    ) -> Dict[str, Any]:
        """Update an existing entry with new on-chain data and status."""
        updated = dict(entry)
        updated["status"] = new_status
        updated["on_chain_answer"] = market.get("currentAnswer")
        updated["on_chain_bond"] = market.get("currentAnswerBond")
        updated["last_answerer"] = answerer
        updated["last_answer_timestamp"] = market.get("currentAnswerTimestamp")
        updated["realitio_timeout"] = int(market.get("timeout", 86400))

        # Clear Mech data if re-evaluating (new non-trusted answer)
        if new_status == NEEDS_EVALUATION and entry.get("evaluation") is not None:
            updated["mech_request"] = None
            updated["mech_response"] = None
            updated["evaluation"] = None
            updated["mech_retries"] = 0

        return updated

    def _send_payload(
        self,
        questions_db: Dict[str, Any],
        selected_id: Optional[str] = None,
        selected_action: Optional[str] = None,
    ) -> Generator:
        """Save DB locally and send lightweight payload for consensus."""
        # Persist DB on shared state (local, not through Tendermint)
        self.questions_db = questions_db

        sender = self.context.agent_address
        payload = ScanPendingMarketsPayload(
            sender=sender,
            n_markets=len(questions_db),
            selected_market_id=selected_id,
            selected_market_action=selected_action,
        )
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()

    def _send_none_payload(self) -> Generator:
        """Send a NONE payload (nothing to do)."""
        sender = self.context.agent_address
        payload = ScanPendingMarketsPayload(sender=sender)
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
