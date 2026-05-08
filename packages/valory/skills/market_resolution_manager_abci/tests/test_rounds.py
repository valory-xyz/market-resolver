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

"""Tests for the rounds module."""

# pylint: disable=protected-access

import json
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

from packages.valory.skills.abstract_round_abci.base import (
    AbciAppDB,
    CollectSameUntilThresholdRound,
)
from packages.valory.skills.market_resolution_manager_abci.payloads import (
    EvaluateAnswersPayload,
    PostTransactionPayload,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    BuildAnswerTxRound,
    EvaluateAnswersRound,
    FinishedResolutionRound,
    FinishedWithAnswerTxRound,
    FinishedWithCtRedeemTokensPostTxRound,
    FinishedWithFpmmRemoveLiquidityPostTxRound,
    FinishedWithFundsForwarderPostTxRound,
    FinishedWithMechPollRound,
    FinishedWithMechRequestRound,
    FinishedWithRealitioWithdrawBondsPostTxRound,
    MarketResolutionManagerAbciApp,
    PostTransactionRound,
    ScanMarketsRound,
    SynchronizedData,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import Event

PARTICIPANTS = ("a", "b", "c")
SAFE_ADDRESS = "0xSafe0000000000000000000000000000000000001"


def _make_db(extra: Optional[Dict] = None) -> AbciAppDB:
    """Create a minimal AbciAppDB for testing."""
    data: Dict = {
        "participants": list(PARTICIPANTS),
        "all_participants": list(PARTICIPANTS),
        "safe_contract_address": SAFE_ADDRESS,
        "consensus_threshold": None,
    }
    if extra:
        data.update(extra)
    return AbciAppDB(setup_data=AbciAppDB.data_to_lists(data))


def _make_synced(extra: Optional[Dict] = None) -> SynchronizedData:
    """Return a SynchronizedData bound to a fresh DB."""
    return SynchronizedData(db=_make_db(extra))


def _cast_round(
    round_cls: Any, synced: SynchronizedData
) -> CollectSameUntilThresholdRound:
    """Instantiate a round for testing."""
    round_ = round_cls(synchronized_data=synced, context=MagicMock())
    return round_


def _collect_payloads(round_: Any, payloads: Any) -> None:
    """Feed all payloads into a round."""
    for p in payloads:
        round_.process_payload(p)


# ---------------------------------------------------------------------------
# SynchronizedData property tests
# ---------------------------------------------------------------------------


class TestSynchronizedData:
    """Tests for SynchronizedData properties."""

    def test_tx_submitter_default(self) -> None:
        """tx_submitter returns None when not set."""
        synced = _make_synced()
        assert synced.tx_submitter is None

    def test_tx_submitter_set(self) -> None:
        """tx_submitter returns the value when set."""
        synced = _make_synced({"tx_submitter": "build_answer_tx_round"})
        assert synced.tx_submitter == "build_answer_tx_round"

    def test_selected_market_id_default(self) -> None:
        """selected_market_id returns None when not set."""
        synced = _make_synced()
        assert synced.selected_market_id is None

    def test_selected_market_id_set(self) -> None:
        """selected_market_id returns value when set."""
        synced = _make_synced({"selected_market_id": "0xMarket"})
        assert synced.selected_market_id == "0xMarket"

    def test_selected_market_action_default(self) -> None:
        """selected_market_action returns None when not set."""
        synced = _make_synced()
        assert synced.selected_market_action is None

    def test_mech_requests_default(self) -> None:
        """mech_requests returns empty list when not set."""
        synced = _make_synced()
        assert synced.mech_requests == []

    def test_mech_requests_set(self) -> None:
        """mech_requests deserializes correctly."""
        data = json.dumps([{"nonce": "abc", "tool": "my-tool", "prompt": "Q?"}])
        synced = _make_synced({"mech_requests": data})
        result = synced.mech_requests
        assert len(result) == 1
        assert result[0].nonce == "abc"

    def test_mech_requests_none_fallback(self) -> None:
        """mech_requests handles None stored value gracefully."""
        synced = _make_synced({"mech_requests": None})
        assert synced.mech_requests == []

    def test_mech_responses_default(self) -> None:
        """mech_responses returns empty list when not set."""
        synced = _make_synced()
        assert synced.mech_responses == []

    def test_mech_responses_list_passthrough(self) -> None:
        """mech_responses handles list stored as list (cross-period path)."""
        synced = _make_synced({"mech_responses": []})
        assert synced.mech_responses == []

    def test_mech_responses_none_fallback(self) -> None:
        """mech_responses handles None stored value gracefully."""
        synced = _make_synced({"mech_responses": None})
        assert synced.mech_responses == []


# ---------------------------------------------------------------------------
# EvaluateAnswersRound.end_block custom routing
# ---------------------------------------------------------------------------


class TestEvaluateAnswersRoundEndBlock:
    """Tests for EvaluateAnswersRound.end_block."""

    def _make_round(self) -> EvaluateAnswersRound:
        return _cast_round(EvaluateAnswersRound, _make_synced())  # type: ignore[return-value]

    def test_mech_requests_set_emits_done(self) -> None:
        """When mech_requests is not None, end_block returns DONE event."""
        round_ = self._make_round()
        mech_json = json.dumps([{"nonce": "x", "tool": "t", "prompt": "Q"}])
        payloads = [
            EvaluateAnswersPayload(
                sender=p, mech_requests=mech_json, evaluation_result=None
            )
            for p in ("a", "b", "c")
        ]
        _collect_payloads(round_, payloads)
        result = round_.end_block()
        assert result is not None
        _, event = result
        assert event == Event.DONE

    def test_evaluation_result_set_emits_none(self) -> None:
        """When evaluation_result is not None, end_block returns NONE event."""
        round_ = self._make_round()
        payloads = [
            EvaluateAnswersPayload(
                sender=p, mech_requests=None, evaluation_result="NEEDS_ANSWER"
            )
            for p in ("a", "b", "c")
        ]
        _collect_payloads(round_, payloads)
        result = round_.end_block()
        assert result is not None
        _, event = result
        assert event == Event.NONE

    def test_both_none_emits_skip(self) -> None:
        """When both fields are None, end_block emits SKIP (intentional no-op)."""
        round_ = self._make_round()
        payloads = [
            EvaluateAnswersPayload(sender=p, mech_requests=None, evaluation_result=None)
            for p in ("a", "b", "c")
        ]
        _collect_payloads(round_, payloads)
        result = round_.end_block()
        assert result is not None
        _, event = result
        assert event == Event.SKIP

    def test_threshold_not_reached_returns_none(self) -> None:
        """end_block returns None when threshold not reached yet."""
        round_ = self._make_round()
        # Only one payload -- not enough for 2/3 threshold with 3 participants
        round_.process_payload(
            EvaluateAnswersPayload(
                sender="a", mech_requests=None, evaluation_result=None
            )
        )
        result = round_.end_block()
        assert result is None

    def test_no_majority_possible_emits_no_majority(self) -> None:
        """end_block returns NO_MAJORITY when majority is impossible."""
        round_ = self._make_round()
        # Send conflicting payloads so threshold can't be reached
        round_.process_payload(
            EvaluateAnswersPayload(
                sender="a", mech_requests='["x"]', evaluation_result=None
            )
        )
        round_.process_payload(
            EvaluateAnswersPayload(
                sender="b", mech_requests=None, evaluation_result="NEEDS_ANSWER"
            )
        )
        round_.process_payload(
            EvaluateAnswersPayload(
                sender="c", mech_requests=None, evaluation_result="NEEDS_VERIFICATION"
            )
        )
        result = round_.end_block()
        assert result is not None
        _, event = result
        assert event == Event.NO_MAJORITY


# ---------------------------------------------------------------------------
# PostTransactionRound.end_block routing
# ---------------------------------------------------------------------------


class TestPostTransactionRoundEndBlock:
    """Tests for PostTransactionRound.end_block multi-path routing."""

    def _make_round(self) -> PostTransactionRound:
        return _cast_round(PostTransactionRound, _make_synced())  # type: ignore[return-value]

    def _run_with_payload(self, content: str) -> Event:
        """Run the round with all agents sending the same content."""
        round_ = self._make_round()
        for sender in ("a", "b", "c"):
            round_.process_payload(
                PostTransactionPayload(sender=sender, content=content)
            )
        result = round_.end_block()
        assert result is not None
        _, event = result
        return event  # type: ignore[return-value]

    def test_mech_request_done(self) -> None:
        """MECH_REQUEST_DONE_PAYLOAD emits MECH_REQUEST_DONE."""
        assert (
            self._run_with_payload(PostTransactionRound.MECH_REQUEST_DONE_PAYLOAD)
            == Event.MECH_REQUEST_DONE
        )

    def test_answer_tx_done(self) -> None:
        """ANSWER_TX_DONE_PAYLOAD emits ANSWER_TX_DONE."""
        assert (
            self._run_with_payload(PostTransactionRound.ANSWER_TX_DONE_PAYLOAD)
            == Event.ANSWER_TX_DONE
        )

    def test_funds_forwarder_tx_done(self) -> None:
        """FUNDS_FORWARDER_TX_DONE_PAYLOAD emits FUNDS_FORWARDER_TX_DONE."""
        assert (
            self._run_with_payload(PostTransactionRound.FUNDS_FORWARDER_TX_DONE_PAYLOAD)
            == Event.FUNDS_FORWARDER_TX_DONE
        )

    def test_fpmm_remove_liquidity_tx_done(self) -> None:
        """FPMM_REMOVE_LIQUIDITY_TX_DONE_PAYLOAD emits FPMM_REMOVE_LIQUIDITY_TX_DONE."""
        assert (
            self._run_with_payload(
                PostTransactionRound.FPMM_REMOVE_LIQUIDITY_TX_DONE_PAYLOAD
            )
            == Event.FPMM_REMOVE_LIQUIDITY_TX_DONE
        )

    def test_ct_redeem_tokens_tx_done(self) -> None:
        """CT_REDEEM_TOKENS_TX_DONE_PAYLOAD emits CT_REDEEM_TOKENS_TX_DONE."""
        assert (
            self._run_with_payload(
                PostTransactionRound.CT_REDEEM_TOKENS_TX_DONE_PAYLOAD
            )
            == Event.CT_REDEEM_TOKENS_TX_DONE
        )

    def test_realitio_withdraw_bonds_tx_done(self) -> None:
        """REALITIO_WITHDRAW_BONDS_TX_DONE_PAYLOAD emits REALITIO_WITHDRAW_BONDS_TX_DONE."""
        assert (
            self._run_with_payload(
                PostTransactionRound.REALITIO_WITHDRAW_BONDS_TX_DONE_PAYLOAD
            )
            == Event.REALITIO_WITHDRAW_BONDS_TX_DONE
        )

    def test_unknown_payload_emits_done(self) -> None:
        """Unknown payload falls through to DONE event."""
        assert self._run_with_payload("UNKNOWN_PAYLOAD") == Event.DONE

    def test_threshold_not_reached_returns_none(self) -> None:
        """end_block returns None when threshold not reached."""
        round_ = self._make_round()
        round_.process_payload(
            PostTransactionPayload(
                sender="a", content=PostTransactionRound.ERROR_PAYLOAD
            )
        )
        result = round_.end_block()
        assert result is None

    def test_no_majority_possible_returns_no_majority(self) -> None:
        """end_block returns NO_MAJORITY when majority impossible."""
        round_ = self._make_round()
        round_.process_payload(PostTransactionPayload(sender="a", content="PAYLOAD_A"))
        round_.process_payload(PostTransactionPayload(sender="b", content="PAYLOAD_B"))
        round_.process_payload(PostTransactionPayload(sender="c", content="PAYLOAD_C"))
        result = round_.end_block()
        assert result is not None
        _, event = result
        assert event == Event.NO_MAJORITY


# ---------------------------------------------------------------------------
# FSM transition function structure tests
# ---------------------------------------------------------------------------


class TestMarketResolutionManagerAbciApp:
    """Smoke tests for FSM wiring."""

    def test_initial_round(self) -> None:
        """Initial round is ScanMarketsRound."""
        assert MarketResolutionManagerAbciApp.initial_round_cls is ScanMarketsRound

    def test_initial_states(self) -> None:
        """Initial states include the three expected re-entry points."""
        assert ScanMarketsRound in MarketResolutionManagerAbciApp.initial_states
        assert BuildAnswerTxRound in MarketResolutionManagerAbciApp.initial_states
        assert PostTransactionRound in MarketResolutionManagerAbciApp.initial_states

    def test_final_states(self) -> None:
        """All expected degenerate rounds are in final_states."""
        finals = MarketResolutionManagerAbciApp.final_states
        assert FinishedWithMechRequestRound in finals
        assert FinishedWithMechPollRound in finals
        assert FinishedWithAnswerTxRound in finals
        assert FinishedResolutionRound in finals
        assert FinishedWithFundsForwarderPostTxRound in finals
        assert FinishedWithFpmmRemoveLiquidityPostTxRound in finals
        assert FinishedWithCtRedeemTokensPostTxRound in finals
        assert FinishedWithRealitioWithdrawBondsPostTxRound in finals

    def test_scan_markets_transitions(self) -> None:
        """Scan-markets round has the correct outgoing transitions."""
        tf = MarketResolutionManagerAbciApp.transition_function
        scan_tf = tf[ScanMarketsRound]
        assert scan_tf[Event.DONE] is EvaluateAnswersRound
        assert scan_tf[Event.NONE] is FinishedResolutionRound
        assert scan_tf[Event.NO_MAJORITY] is ScanMarketsRound
        assert scan_tf[Event.ROUND_TIMEOUT] is ScanMarketsRound

    def test_evaluate_answers_transitions(self) -> None:
        """Evaluate-answers round transitions are correct."""
        tf = MarketResolutionManagerAbciApp.transition_function
        eval_tf = tf[EvaluateAnswersRound]
        assert eval_tf[Event.DONE] is FinishedWithMechRequestRound
        assert eval_tf[Event.NONE] is BuildAnswerTxRound
        assert eval_tf[Event.NO_MAJORITY] is FinishedResolutionRound
        assert eval_tf[Event.ROUND_TIMEOUT] is FinishedResolutionRound

    def test_build_answer_tx_transitions(self) -> None:
        """Build-answer-tx round transitions are correct."""
        tf = MarketResolutionManagerAbciApp.transition_function
        bat_tf = tf[BuildAnswerTxRound]
        assert bat_tf[Event.DONE] is FinishedWithAnswerTxRound
        assert bat_tf[Event.NONE] is FinishedResolutionRound
        assert bat_tf[Event.NO_MAJORITY] is FinishedResolutionRound
        assert bat_tf[Event.ROUND_TIMEOUT] is FinishedResolutionRound

    def test_post_transaction_transitions(self) -> None:
        """Post-transaction round has all expected outgoing transitions."""
        tf = MarketResolutionManagerAbciApp.transition_function
        post_tf = tf[PostTransactionRound]
        assert post_tf[Event.MECH_REQUEST_DONE] is FinishedWithMechPollRound
        assert post_tf[Event.ANSWER_TX_DONE] is FinishedResolutionRound
        assert (
            post_tf[Event.FUNDS_FORWARDER_TX_DONE]
            is FinishedWithFundsForwarderPostTxRound
        )
        assert (
            post_tf[Event.FPMM_REMOVE_LIQUIDITY_TX_DONE]
            is FinishedWithFpmmRemoveLiquidityPostTxRound
        )
        assert (
            post_tf[Event.CT_REDEEM_TOKENS_TX_DONE]
            is FinishedWithCtRedeemTokensPostTxRound
        )
        assert (
            post_tf[Event.REALITIO_WITHDRAW_BONDS_TX_DONE]
            is FinishedWithRealitioWithdrawBondsPostTxRound
        )
        assert post_tf[Event.DONE] is FinishedResolutionRound
        assert post_tf[Event.NONE] is FinishedResolutionRound
        assert post_tf[Event.NO_MAJORITY] is FinishedResolutionRound
        assert post_tf[Event.ROUND_TIMEOUT] is FinishedResolutionRound

    def test_event_to_timeout(self) -> None:
        """ROUND_TIMEOUT has a configured timeout."""
        assert Event.ROUND_TIMEOUT in MarketResolutionManagerAbciApp.event_to_timeout
        assert (
            MarketResolutionManagerAbciApp.event_to_timeout[Event.ROUND_TIMEOUT]
            == 180.0
        )
