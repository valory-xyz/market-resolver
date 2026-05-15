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

"""This module contains the ScanMarketsBehaviour."""

from string import Template
from typing import Any, Dict, Generator, List, Optional

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
    is_cached_evaluation_valid,
    parse_mech_response,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    ScanMarketsPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    ScanMarketsRound,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import (
    AnswerStatus,
)

# Omen subgraph query: pending markets from watched creators.
# answerFinalizedTimestamp: null = not yet finalized
# openingTimestamp_lt: now = market is past opening time
# openingTimestamp_gt: oldest_allowed = discard markets older than the
#   configured max age. The Omen subgraph gateway load-balances across
#   indexer replicas, and some replicas are desynchronised: they serve
#   old finalized markets with answerFinalizedTimestamp=null and all
#   answer fields as null. Bounding openingTimestamp at the query level
#   prevents these phantom markets from ever entering the pipeline.
PENDING_MARKETS_QUERY = Template("""{
    fixedProductMarketMakers(
        where: {
            creator_in: [${creators}]
            openingTimestamp_lt: ${current_timestamp}
            openingTimestamp_gt: ${oldest_allowed_timestamp}
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
}""")

# Finalizing markets (answered but finalization still in the future -- can still be challenged).
# Same openingTimestamp_gt guard as above against desynchronised replicas.
FINALIZING_MARKETS_QUERY = Template("""{
    fixedProductMarketMakers(
        where: {
            creator_in: [${creators}]
            openingTimestamp_lt: ${current_timestamp}
            openingTimestamp_gt: ${oldest_allowed_timestamp}
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
}""")

# Realitio subgraph query: get latest answerer for each question
LATEST_ANSWERERS_QUERY = Template("""{
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
}""")

SUBGRAPH_BATCH_SIZE = 1000


class ScanMarketsBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to scan pending markets and classify questions."""

    matching_round = ScanMarketsRound

    def async_act(  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
        self,
    ) -> Generator:
        """Scan markets and classify questions."""
        watched_addresses = self.params.watched_creator_addresses
        if not watched_addresses:
            self.context.logger.info("No watched creator addresses configured.")
            yield from self._send_none_payload()
            return

        # Step 1: Query Omen subgraph for pending markets
        fetched_markets = yield from self._fetch_pending_and_finalizing_markets(
            watched_addresses
        )
        if fetched_markets is None:
            self.context.logger.error("Subgraph error fetching markets.")
            yield from self._send_none_payload()
            return

        self.context.logger.info(
            f"Found {len(fetched_markets)} pending/finalizing market(s)."
        )

        # Step 2: Query Realitio subgraph for latest answerers
        question_ids = [
            m["question"]["id"] for m in fetched_markets if m.get("currentAnswer")
        ]
        answerers = yield from self._fetch_current_answerers(question_ids)

        # Step 3: Purge stale DB entries (markets no longer pending/finalizing)
        questions_db = dict(self.questions_db)
        fetched_ids = {m["id"] for m in fetched_markets}
        for mid in set(questions_db) - fetched_ids:
            del questions_db[mid]

        trusted = set(addr.lower() for addr in self.params.trusted_addresses)
        trusted.add(self.synchronized_data.safe_contract_address.lower())
        now = self.last_synced_timestamp

        # Step 4: Upsert -- sync on-chain state into DB
        for market in fetched_markets:
            market_id = market["id"]
            question_id = market["question"]["id"]
            current_answerer = answerers.get(question_id, "").lower()
            if market_id in questions_db:
                entry = questions_db[market_id]
                entry["on_chain_answer"] = market.get("currentAnswer")
                entry["on_chain_bond"] = market.get("currentAnswerBond")
                entry["last_answerer"] = current_answerer
                entry["last_answer_timestamp"] = market.get("currentAnswerTimestamp")
                entry["realitio_timeout"] = int(market.get("timeout", 86400))
            else:
                questions_db[market_id] = self._new_entry(market, current_answerer, now)

        # Step 5: Refresh mech_requests cache from the Mech Gnosis subgraph.
        # We re-query every scan so that:
        # 1. Late deliveries (responses that arrive after our in-process
        #    ``mech_response_round`` timed out) are still picked up.
        #    Otherwise a Mech that is slow but eventually responsive forces
        #    us to re-request the same market forever.
        # 2. ``mech_retries`` converges to ``len(mech_requests)`` once the
        #    subgraph catches up, so the exhaustion gate survives restarts.
        #    The ``max()`` below guarantees the counter is monotonic: a brief
        #    subgraph-indexing-lag right after ``evaluate_answers`` fires
        #    a request can never lower the count back below the local value.
        for _, entry in questions_db.items():
            requests = yield from self.fetch_mech_requests_for_market(entry)
            if requests is None:
                continue
            entry["mech_requests"] = requests
            if entry.get("evaluation") is None:
                evaluation = self._earliest_valid_evaluation(requests)
                if evaluation is not None:
                    entry["evaluation"] = evaluation
            entry["mech_retries"] = max(
                int(entry.get("mech_retries", 0)),
                len(requests),
            )

        # Step 6: Classify statuses from current data
        for entry in questions_db.values():
            answerer = entry.get("last_answerer", "").lower()
            on_chain_answer = entry.get("on_chain_answer")
            evaluation = entry.get("evaluation")
            mech_answer = evaluation.get("answer") if evaluation else None

            if on_chain_answer is None:
                entry["status"] = AnswerStatus.NEEDS_ANSWER
            elif answerer in trusted:
                entry["status"] = AnswerStatus.TRUSTED_ANSWER
            elif mech_answer is not None and mech_answer == on_chain_answer:
                entry["status"] = AnswerStatus.VERIFIED
            elif mech_answer is not None:
                entry["status"] = AnswerStatus.TRANSACTION_PENDING
            else:
                # No evaluation, or undeterminable (answer=None).
                # Clear stale undeterminable evaluations so a fresh Mech
                # request is made once retry_after expires. ``mech_requests``
                # is sourced from the subgraph each scan; do not mutate it
                # here.
                entry["evaluation"] = None
                entry["status"] = AnswerStatus.NEEDS_VERIFICATION

        # Step 7: Select actionable market
        safe_address = self.synchronized_data.safe_contract_address
        safe_balance = yield from self.get_native_balance(safe_address)
        if safe_balance is None:
            safe_balance = 0
        self.context.logger.info(
            f"Safe {safe_address} balance: {safe_balance / 10 ** 18:.4f} xDAI"
        )

        actionable: List[Dict[str, Any]] = []
        for market_id, entry in questions_db.items():
            status = entry["status"]
            if status in (AnswerStatus.TRUSTED_ANSWER, AnswerStatus.VERIFIED):
                continue

            # Bond affordability -- skip when prefetch is off.
            # When prefetch_mech_evaluations is on, we always select
            # markets so every market gets a Mech evaluation.
            # build_answer_tx gates the actual tx submission on bond.
            if not self.params.prefetch_mech_evaluations:
                on_chain_bond = int(entry.get("on_chain_bond") or 0)
                required_bond = (
                    on_chain_bond * 2
                    if on_chain_bond > 0
                    else self.params.initial_answer_bond
                )
                if required_bond > self.params.max_challenge_bond:
                    continue
                if required_bond > safe_balance:
                    continue

            # Retry / cooldown gates
            if status in (AnswerStatus.NEEDS_ANSWER, AnswerStatus.NEEDS_VERIFICATION):
                retry_after = entry.get("retry_after", 0)
                if retry_after and now < retry_after:
                    continue
                if entry.get("mech_retries", 0) >= self.params.max_mech_retries:
                    continue
            elif status == AnswerStatus.TRANSACTION_PENDING:  # pragma: no branch
                timeout = int(entry.get("realitio_timeout", 86400))
                finalization_deadline = (
                    int(entry.get("last_answer_timestamp") or 0) + timeout
                )
                if now >= finalization_deadline:
                    continue
                prior_tx = entry.get("pending_tx") or {}
                if prior_tx.get("escalation_count", 0) > 0:
                    cooldown = timeout * self.params.challenge_cooldown_fraction
                    last_ts = prior_tx.get("timestamp", 0)
                    urgency = (
                        now
                        >= finalization_deadline - self.params.challenge_urgency_buffer
                    )
                    if not (urgency or now >= last_ts + cooldown):
                        continue

            actionable.append({"market_id": market_id, "action": status})

        # Sort: urgent challenges first (closest to finalization), then unanswered
        actionable.sort(key=lambda item: self._actionable_sort_key(questions_db, item))

        # Log summary
        self._log_scan_summary(questions_db)

        if not actionable:
            self.context.logger.info("No actionable questions found.")
            yield from self._send_payload(questions_db)
            return

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

    def _fetch_pending_and_finalizing_markets(  # pylint: disable=too-many-locals
        self, watched: List[str]
    ) -> Generator[None, None, Optional[List[Dict[str, Any]]]]:
        """Fetch pending + finalizing markets from Omen subgraph.

        Two queries:
        1. Pending: answerFinalizedTimestamp is null (no answer yet)
        2. Finalizing: answerFinalizedTimestamp > now (answered but not yet finalized)

        :param watched: lowercased list of market creator addresses to scope by.
        :return: combined market list, or ``None`` on subgraph error.
        :yield: HTTP requests to the Omen subgraph.
        """
        creators_str = ", ".join(f'"{c.lower()}"' for c in watched)
        now = self.last_synced_timestamp
        max_age = self.params.omen_subgraph_max_market_age_seconds
        oldest_allowed = now - max_age

        # Query 1: pending (unanswered)
        query1 = PENDING_MARKETS_QUERY.substitute(
            creators=creators_str,
            current_timestamp=now,
            oldest_allowed_timestamp=oldest_allowed,
        )
        result1 = yield from self.get_omen_subgraph_result(query1)
        pending = (
            result1.get("data", {}).get("fixedProductMarketMakers", [])
            if result1
            else []
        )

        # Query 2: finalizing (answered, finalization in the future)
        query2 = FINALIZING_MARKETS_QUERY.substitute(
            creators=creators_str,
            current_timestamp=now,
            oldest_allowed_timestamp=oldest_allowed,
        )
        result2 = yield from self.get_omen_subgraph_result(query2)
        finalizing = (
            result2.get("data", {}).get("fixedProductMarketMakers", [])
            if result2
            else []
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

    def _fetch_current_answerers(
        self, question_ids: List[str]
    ) -> Generator[None, None, Dict[str, str]]:
        """Fetch latest answerer for each question from Realitio subgraph."""
        answerers: Dict[str, str] = {}
        if not question_ids:
            return answerers

        for i in range(0, len(question_ids), SUBGRAPH_BATCH_SIZE):
            batch = question_ids[i : i + SUBGRAPH_BATCH_SIZE]
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

    @staticmethod
    def _earliest_valid_evaluation(
        mech_requests: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Earliest valid evaluation across requests + their deliveries."""
        # ``base.py:69`` fetches deliveries unbounded by design; iterate
        # ALL deliveries per request so a Mech-internal retry whose first
        # delivery is garbage but a later one is valid still resolves.
        for req in mech_requests:
            for delivery in req.get("deliveries") or []:
                evaluation = parse_mech_response(delivery.get("toolResponse"))
                if evaluation is None:
                    continue
                if not is_cached_evaluation_valid(evaluation):
                    continue
                return evaluation
        return None

    @staticmethod
    def _new_entry(
        market: Dict[str, Any],
        answerer: str,
        now: int,
    ) -> Dict[str, Any]:
        """Create a new questions_db entry. Status is set later in step 6."""
        return {
            "status": None,
            "market_id": market.get("id"),
            "title": market.get("title", ""),
            "question_id": market.get("question", {}).get("id"),
            "detected_at": now,
            "on_chain_answer": market.get("currentAnswer"),
            "on_chain_bond": market.get("currentAnswerBond"),
            "last_answerer": answerer,
            "last_answer_timestamp": market.get("currentAnswerTimestamp"),
            "market_closing_timestamp": int(market.get("openingTimestamp", 0)),
            "realitio_timeout": int(market.get("timeout", 86400)),
            "mech_requests": [],
            "pending_nonce": None,
            "evaluation": None,
            "pending_tx": None,
            "mech_retries": 0,
        }

    @staticmethod
    def _actionable_sort_key(
        questions_db: Dict[str, Any], item: Dict[str, Any]
    ) -> tuple:
        """Sort key: challenges first (soonest deadline), then unanswered."""
        entry = questions_db[item["market_id"]]
        is_unanswered = entry.get("on_chain_answer") is None
        last_ts = int(entry.get("last_answer_timestamp") or 0)
        timeout = int(entry.get("realitio_timeout", 86400))
        finalization = last_ts + timeout if last_ts else float("inf")
        return (1 if is_unanswered else 0, finalization)

    def _log_scan_summary(self, questions_db: Dict[str, Any]) -> None:
        """Log scan summary counts and actionable market details."""
        counts: Dict[str, int] = {}
        for entry in questions_db.values():
            status = entry["status"]
            counts[status] = counts.get(status, 0) + 1
        self.context.logger.info(
            f"Scan complete: {len(questions_db)} markets tracked -- "
            f"{counts.get(AnswerStatus.TRUSTED_ANSWER, 0)} trusted, "
            f"{counts.get(AnswerStatus.VERIFIED, 0)} verified, "
            f"{counts.get(AnswerStatus.NEEDS_ANSWER, 0)} need answer, "
            f"{counts.get(AnswerStatus.NEEDS_VERIFICATION, 0)} need evaluation, "
            f"{counts.get(AnswerStatus.TRANSACTION_PENDING, 0)} transaction pending"
        )
        for mid, entry in questions_db.items():
            if entry["status"] in (
                AnswerStatus.TRUSTED_ANSWER,
                AnswerStatus.VERIFIED,
            ):
                continue
            self.context.logger.info(
                f"  [{entry['status']}] {mid} "
                f"answer={entry.get('on_chain_answer') or 'unanswered'} "
                f"bond={entry.get('on_chain_bond') or '0'} "
                f"answerer={entry.get('last_answerer', '')}"
            )

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
        payload = ScanMarketsPayload(
            sender=sender,
            selected_market_id=selected_id,
            selected_market_action=selected_action,
        )
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()

    def _send_none_payload(self) -> Generator:
        """Send a NONE payload (nothing to do)."""
        sender = self.context.agent_address
        payload = ScanMarketsPayload(sender=sender)
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
