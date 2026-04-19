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

"""Tests for the payloads module."""

from packages.valory.skills.market_resolution_manager_abci.payloads import (
    BuildAnswerTxPayload,
    EvaluateAnswersPayload,
    PostTransactionPayload,
    ScanMarketsPayload,
)


def test_scan_markets_payload_defaults() -> None:
    """Test ScanMarketsPayload with default (None) fields."""
    p = ScanMarketsPayload(sender="0xabc")
    assert p.selected_market_id is None
    assert p.selected_market_action is None


def test_scan_markets_payload_with_values() -> None:
    """Test ScanMarketsPayload with explicit values."""
    p = ScanMarketsPayload(
        sender="0xabc",
        selected_market_id="0xMarket",
        selected_market_action="NEEDS_ANSWER",
    )
    assert p.selected_market_id == "0xMarket"
    assert p.selected_market_action == "NEEDS_ANSWER"


def test_evaluate_answers_payload_mech_requests() -> None:
    """Test EvaluateAnswersPayload with mech_requests set."""
    p = EvaluateAnswersPayload(sender="0xabc", mech_requests='[{"nonce":"x"}]')
    assert p.mech_requests == '[{"nonce":"x"}]'
    assert p.evaluation_result is None


def test_evaluate_answers_payload_evaluation_result() -> None:
    """Test EvaluateAnswersPayload with evaluation_result set."""
    p = EvaluateAnswersPayload(sender="0xabc", evaluation_result="NEEDS_ANSWER")
    assert p.evaluation_result == "NEEDS_ANSWER"
    assert p.mech_requests is None


def test_build_answer_tx_payload_with_tx() -> None:
    """Test BuildAnswerTxPayload with tx fields."""
    p = BuildAnswerTxPayload(
        sender="0xabc", tx_submitter="build_answer_tx_round", tx_hash="0xhash"
    )
    assert p.tx_submitter == "build_answer_tx_round"
    assert p.tx_hash == "0xhash"


def test_build_answer_tx_payload_no_tx() -> None:
    """Test BuildAnswerTxPayload with None tx_hash."""
    p = BuildAnswerTxPayload(sender="0xabc")
    assert p.tx_submitter is None
    assert p.tx_hash is None


def test_post_transaction_payload_default() -> None:
    """Test PostTransactionPayload with default empty content."""
    p = PostTransactionPayload(sender="0xabc")
    assert p.content == ""


def test_post_transaction_payload_with_content() -> None:
    """Test PostTransactionPayload with explicit content."""
    p = PostTransactionPayload(sender="0xabc", content="ANSWER_TX_DONE")
    assert p.content == "ANSWER_TX_DONE"
