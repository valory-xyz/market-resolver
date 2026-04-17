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

"""This module contains the BuildAnswerTxBehaviour."""

from typing import Generator, Optional, Tuple, cast

from packages.valory.contracts.gnosis_safe.contract import GnosisSafeContract
from packages.valory.contracts.multisend.contract import (
    MultiSendContract,
    MultiSendOperation,
)
from packages.valory.contracts.realitio.contract import RealitioContract
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    MarketResolutionManagerBaseBehaviour,
    parse_mech_response,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    BuildAnswerTxPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    BuildAnswerTxRound,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import (
    AnswerStatus,
)
from packages.valory.skills.transaction_settlement_abci.payload_tools import (
    hash_payload_to_hex,
)

# Realitio answer encoding
ANSWER_YES = "0x0000000000000000000000000000000000000000000000000000000000000000"
ANSWER_NO = "0x0000000000000000000000000000000000000000000000000000000000000001"
ANSWER_INVALID = "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
MAX_PREVIOUS_UNANSWERED = 0

ETHER_VALUE = 0
SAFE_TX_GAS = 0
SAFE_OPERATION_DELEGATE_CALL = 1


def _decode_answer(answer_hex: str) -> str:
    """Decode Realitio answer to human-readable label."""
    if answer_hex == ANSWER_NO:
        return "NO"
    if answer_hex == ANSWER_YES:
        return "YES"
    if answer_hex == ANSWER_INVALID:
        return "INVALID"
    return answer_hex


class BuildAnswerTxBehaviour(MarketResolutionManagerBaseBehaviour):
    """Behaviour to build challenge/answer transactions.

    Entered in two ways:
    - After MechInteract returns: reads mech_responses from SynchronizedData
    - From EvaluateAnswers (NONE event): existing evaluation in DB, skip Mech
    """

    matching_round = BuildAnswerTxRound

    def async_act(  # pylint: disable=too-many-locals,too-many-return-statements,too-many-statements,too-many-branches
        self,
    ) -> Generator:
        """Build challenge transaction or mark as verified."""
        market_id = self.synchronized_data.selected_market_id
        if market_id is None:
            self.context.logger.info("No selected market -- nothing to build.")
            yield from self._send_payload(None)
            return

        questions_db = dict(self.questions_db)
        entry = questions_db.get(market_id)
        if entry is None:
            self.context.logger.error(f"Market {market_id} not found in DB.")
            yield from self._send_payload(None)
            return

        # Check if we have fresh Mech responses (from MechInteract).
        # Skip if the evaluation was already populated by the subgraph
        # cache path — MechInteract didn't run in that case.
        expected_nonce = (entry.get("mech_request") or {}).get("nonce")
        if entry.get("evaluation") is None and expected_nonce:
            mech_responses = self.synchronized_data.mech_responses
        else:
            mech_responses = []
        if mech_responses and expected_nonce:
            # Find the response matching our request (by nonce)
            for resp in mech_responses:
                if resp.nonce == expected_nonce:
                    self.context.logger.info(
                        f"Market {market_id}: received Mech response -- "
                        f"result={resp.result}, error={resp.error}"
                    )
                    # Parse the Mech result
                    evaluation = parse_mech_response(resp.result)
                    if evaluation is None:
                        self.context.logger.warning(
                            f"Could not parse Mech result as JSON: "
                            f"{(resp.result or '')[:200]}"
                        )
                    else:
                        entry["evaluation"] = evaluation
                        entry["mech_response"] = {
                            "source": "mech_interact",
                            "nonce": resp.nonce,
                            "result": resp.result,
                            "error": resp.error,
                            "requestId": resp.requestId,
                        }
                    break
            # Save DB with mech response data
            questions_db[market_id] = entry
            self.questions_db = questions_db

        evaluation = entry.get("evaluation")
        now = int(self.last_synced_timestamp)
        cooldown = self.params.mech_retry_cooldown

        # No usable evaluation — either no Mech response yet, or the
        # response was garbage (parse_mech_response returned None).
        # When we just processed a garbage response (expected_nonce was
        # present), apply a retry cooldown so we don't hammer Mech.
        if evaluation is None:
            if expected_nonce:
                retry_after = now + cooldown
                entry["retry_after"] = retry_after
                questions_db[market_id] = entry
                self.questions_db = questions_db
                self.context.logger.info(
                    f"Market {market_id}: Mech returned garbage. "
                    f"Retry after {retry_after} ({cooldown}s cooldown)."
                )
            else:
                self.context.logger.info(
                    f"Market {market_id}: no evaluation data available."
                )
            yield from self._send_payload(None)
            return

        mech_answer = evaluation.get("answer")
        on_chain_answer = entry.get("on_chain_answer")

        # Case B (undeterminable): answer=None — set retry cooldown
        if mech_answer is None:
            on_chain_bond = int(entry.get("on_chain_bond") or 0)
            timeout = int(entry.get("realitio_timeout", 86400))
            last_answer_ts = int(entry.get("last_answer_timestamp") or 0)

            if on_chain_bond > 0 and last_answer_ts > 0:
                finalization = last_answer_ts + timeout
                retry_buffer = int(timeout * self.params.challenge_cooldown_fraction)
                # Minimum cooldown floor: never less than mech_retry_cooldown,
                # even close to finalization, to avoid tight retry loops.
                retry_after = max(
                    now + cooldown,
                    finalization - retry_buffer,
                )
                self.context.logger.info(
                    f"Market {market_id}: undeterminable, untrusted answer "
                    f"present. Retry after {retry_after}."
                )
            else:
                retry_after = now + 86400
                self.context.logger.info(
                    f"Market {market_id}: undeterminable, no answer yet. "
                    f"Retry after {retry_after} (24h)."
                )

            entry["retry_after"] = retry_after
            questions_db[market_id] = entry
            self.questions_db = questions_db
            yield from self._send_payload(None)
            return

        # Pre-flight: refresh on-chain answer & bond directly from
        # Realitio. The values stored in ``entry`` came from the
        # previous subgraph scan and can be several minutes stale. In
        # that window a third party (often a front-running bot) may
        # have posted an answer on the same question. Without this
        # check we would either (a) waste gas posting a tx that reverts
        # on the Realitio bond check, or (b) donate our bond to the
        # front-runner by defending an already-correct answer.
        question_id_hex = entry.get("question_id")
        if question_id_hex:
            fresh = yield from self._read_onchain_answer_and_bond(question_id_hex)
            if fresh is not None:
                fresh_answer, fresh_bond = fresh
                if fresh_answer != on_chain_answer or fresh_bond != int(
                    entry.get("on_chain_bond") or 0
                ):
                    self.context.logger.info(
                        f"Market {market_id}: on-chain state changed since "
                        f"scan. scan={_decode_answer(on_chain_answer or 'none')} "
                        f"bond={int(entry.get('on_chain_bond') or 0)} -> "
                        f"live={_decode_answer(fresh_answer or 'none')} "
                        f"bond={fresh_bond}"
                    )
                on_chain_answer = fresh_answer
                entry["on_chain_answer"] = fresh_answer
                entry["on_chain_bond"] = fresh_bond

        agrees = on_chain_answer == mech_answer if on_chain_answer else False
        evaluation["agrees_with_on_chain"] = agrees
        evaluation["answer"] = mech_answer
        entry["evaluation"] = evaluation

        self.context.logger.info(
            f"Market {market_id}: Mech={_decode_answer(mech_answer)}, "
            f"on-chain={_decode_answer(on_chain_answer or 'none')}, "
            f"agrees={agrees}"
        )

        if on_chain_answer is not None and agrees:
            self.context.logger.info(
                f"Market {market_id}: Mech agrees with on-chain answer "
                f"({_decode_answer(on_chain_answer)}). Marking VERIFIED."
            )
            entry["status"] = AnswerStatus.VERIFIED
            questions_db[market_id] = entry
            self.questions_db = questions_db
            yield from self._send_payload(None)
            return

        challenge = entry.get("pending_tx") or {}
        escalation_count = challenge.get("escalation_count", 0)

        on_chain_bond = int(entry.get("on_chain_bond") or 0)
        if on_chain_bond == 0:
            required_bond = self.params.initial_answer_bond
            max_previous = MAX_PREVIOUS_UNANSWERED
            action = "ANSWER"
        else:
            required_bond = on_chain_bond * 2
            max_previous = on_chain_bond
            action = "CHALLENGE"

        if required_bond > self.params.max_challenge_bond:
            self.context.logger.info(
                f"Market {market_id}: required bond "
                f"{required_bond / 10**18:.1f} xDAI exceeds max "
                f"{self.params.max_challenge_bond / 10**18:.1f} xDAI. "
                f"Evaluation cached, skipping tx."
            )
            questions_db[market_id] = entry
            self.questions_db = questions_db
            yield from self._send_payload(None)
            return

        safe_address = self.synchronized_data.safe_contract_address
        safe_balance = yield from self.get_native_balance(safe_address)
        if safe_balance is None:
            safe_balance = 0
        if required_bond > safe_balance:
            self.context.logger.info(
                f"Market {market_id}: required bond "
                f"{required_bond / 10**18:.1f} xDAI exceeds safe balance "
                f"{safe_balance / 10**18:.1f} xDAI. "
                f"Evaluation cached, skipping tx."
            )
            questions_db[market_id] = entry
            self.questions_db = questions_db
            yield from self._send_payload(None)
            return

        bond_xdai = required_bond / 10**18
        question_id = entry.get("question_id", "unknown")

        self.context.logger.info(
            f"Building {action} tx: market={market_id}, "
            f"answer={_decode_answer(mech_answer)}, "
            f"on_chain={_decode_answer(on_chain_answer or 'none')}, "
            f"bond={bond_xdai} xDAI, escalation=#{escalation_count + 1}"
        )

        # Build the Realitio submitAnswer tx
        tx_hash = yield from self._build_submit_answer_tx(
            question_id=question_id,
            answer=mech_answer,
            max_previous=max_previous,
            bond=required_bond,
        )

        if tx_hash is None:
            self.context.logger.error(
                f"Market {market_id}: failed to build submitAnswer tx."
            )
            yield from self._send_payload(None)
            return

        # Update DB
        entry["status"] = AnswerStatus.TRANSACTION_PENDING
        entry["pending_tx"] = {
            "tx_hash": tx_hash,
            "bond": required_bond,
            "answer": mech_answer,
            "escalation_count": escalation_count + 1,
            "timestamp": self.last_synced_timestamp,
        }
        questions_db[market_id] = entry
        self.questions_db = questions_db

        self.context.logger.info(
            f"Market {market_id}: status -> TRANSACTION_PENDING, "
            f"submitting tx to TxSettlement"
        )

        # Send tx_hash -> done_event -> FinishedWithAnswerTxRound -> TxSettlement
        yield from self._send_payload(tx_hash)

    def _read_onchain_answer_and_bond(
        self, question_id_hex: str
    ) -> Generator[None, None, Optional[Tuple[Optional[str], int]]]:
        """Read the current best answer and bond from Realitio.

        Returns a tuple ``(best_answer_hex_or_None, bond)``. The answer
        is None when the question has never been answered (best answer
        is the zero sentinel AND bond is zero). On contract-api error,
        returns None so the caller falls back to the stale scan values.

        :param question_id_hex: the Realitio question id as a 0x-prefixed
            hex string.
        :yield: contract-api requests.
        :return: (answer, bond) tuple, or None on failure.
        """
        try:
            question_id_bytes = bytes.fromhex(question_id_hex[2:])
        except (ValueError, TypeError):
            return None

        answer_resp = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.params.realitio_contract,
            contract_id=str(RealitioContract.contract_id),
            contract_callable="get_best_answer",
            question_id=question_id_bytes,
            chain_id=self.params.default_chain_id,
        )
        if (
            answer_resp is None
            or answer_resp.performative != ContractApiMessage.Performative.STATE
        ):
            self.context.logger.warning(
                f"Market question {question_id_hex[:18]}...: "
                f"get_best_answer failed, using stale scan values"
            )
            return None

        bond_resp = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.params.realitio_contract,
            contract_id=str(RealitioContract.contract_id),
            contract_callable="get_bond",
            question_id=question_id_bytes,
            chain_id=self.params.default_chain_id,
        )
        if (
            bond_resp is None
            or bond_resp.performative != ContractApiMessage.Performative.STATE
        ):
            self.context.logger.warning(
                f"Market question {question_id_hex[:18]}...: "
                f"get_bond failed, using stale scan values"
            )
            return None

        best_answer = cast(str, answer_resp.state.body.get("best_answer"))
        bond = int(bond_resp.state.body.get("bond") or 0)

        # Zero sentinel answer + zero bond == unanswered.
        zero_answer = "0x" + "00" * 32
        if best_answer == zero_answer and bond == 0:
            return None, 0
        return best_answer, bond

    def _build_submit_answer_tx(
        self,
        question_id: str,
        answer: str,
        max_previous: int,
        bond: int,
    ) -> Generator[None, None, Optional[str]]:
        """Build a submitAnswer tx wrapped in multisend for Safe execution.

        Returns the payload hash string for TxSettlement, or None on error.
        """
        # Step 1: Get the Realitio submitAnswer calldata
        try:
            question_id_bytes = bytes.fromhex(question_id[2:])
            answer_bytes = bytes.fromhex(answer[2:])
        except (ValueError, TypeError):
            self.context.logger.error(
                f"Invalid hex for question_id={question_id!r} or answer={answer!r}"
            )
            return None

        response = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.params.realitio_contract,
            contract_id=str(RealitioContract.contract_id),
            contract_callable="get_submit_answer_tx",
            question_id=question_id_bytes,
            answer=answer_bytes,
            max_previous=max_previous,
            chain_id=self.params.default_chain_id,
        )

        if (
            response is None
            or response.performative != ContractApiMessage.Performative.STATE
        ):
            self.context.logger.error(
                f"Could not get submitAnswer calldata. " f"Response: {response}"
            )
            return None

        submit_data = cast(bytes, response.state.body["data"])

        # Step 2: Wrap in multisend (single tx, but Safe requires multisend)
        multi_send_txs = [
            {
                "operation": MultiSendOperation.CALL,
                "to": self.params.realitio_contract,
                "value": bond,
                "data": submit_data,
            }
        ]

        response = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.params.multisend_address,
            contract_id=str(MultiSendContract.contract_id),
            contract_callable="get_tx_data",
            multi_send_txs=multi_send_txs,
            chain_id=self.params.default_chain_id,
        )

        if response.performative != ContractApiMessage.Performative.RAW_TRANSACTION:
            self.context.logger.error(
                f"Could not compile multisend tx. " f"Response: {response}"
            )
            return None

        multisend_data_str = cast(str, response.raw_transaction.body["data"])[2:]
        tx_data = bytes.fromhex(multisend_data_str)

        # Step 3: Get Safe tx hash
        tx_hash = yield from self._get_safe_tx_hash(
            to_address=self.params.multisend_address,
            data=tx_data,
            value=ETHER_VALUE,
            operation=SAFE_OPERATION_DELEGATE_CALL,
        )

        if tx_hash is None:
            self.context.logger.error("Could not compute Safe tx hash.")
            return None

        # Step 4: Build the payload hash for TxSettlement
        payload_data = hash_payload_to_hex(
            safe_tx_hash=tx_hash,
            ether_value=ETHER_VALUE,
            safe_tx_gas=SAFE_TX_GAS,
            operation=SAFE_OPERATION_DELEGATE_CALL,
            to_address=self.params.multisend_address,
            data=tx_data,
        )

        self.context.logger.info(
            f"Built submitAnswer tx -- payload_hash={payload_data}"
        )
        return payload_data

    def _get_safe_tx_hash(
        self,
        to_address: str,
        data: bytes,
        value: int = 0,
        operation: int = 0,
    ) -> Generator[None, None, Optional[str]]:
        """Get Safe transaction hash via GnosisSafe contract."""
        response = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.synchronized_data.safe_contract_address,
            contract_id=str(GnosisSafeContract.contract_id),
            contract_callable="get_raw_safe_transaction_hash",
            to_address=to_address,
            value=value,
            data=data,
            operation=operation,
            chain_id=self.params.default_chain_id,
        )

        if (
            response is None
            or response.performative != ContractApiMessage.Performative.STATE
        ):
            self.context.logger.error(
                f"Could not get safe tx hash. Response: {response}"
            )
            return None

        tx_hash = cast(str, response.state.body["tx_hash"])
        tx_hash = tx_hash[2:] if tx_hash.startswith("0x") else tx_hash
        return tx_hash

    def _send_payload(self, tx_hash: Optional[str]) -> Generator:
        """Send the build challenges payload.

        tx_hash not None -> done_event -> FinishedWithAnswerTxRound -> TxSettlement
        tx_hash None -> none_event -> CleanupTrackedMarketsRound
        """
        self.questions_db = dict(self.questions_db)
        sender = self.context.agent_address

        if tx_hash is not None:
            tx_submitter = self.matching_round.auto_round_id()
        else:
            tx_submitter = None

        payload = BuildAnswerTxPayload(
            sender=sender,
            tx_submitter=tx_submitter,
            tx_hash=tx_hash,
        )
        yield from self.send_a2a_transaction(payload)
        yield from self.wait_until_round_end()
        self.set_done()
