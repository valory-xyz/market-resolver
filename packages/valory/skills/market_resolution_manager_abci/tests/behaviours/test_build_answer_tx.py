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

"""Tests for behaviours/build_answer_tx.py."""

# pylint: disable=protected-access,unused-argument
# pylint: disable=too-many-arguments,too-many-positional-arguments

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, PropertyMock, patch

from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    ANSWER_INVALID,
    ANSWER_NO,
    ANSWER_YES,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.build_answer_tx import (
    BuildAnswerTxBehaviour,
    _decode_answer,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import (
    AnswerStatus,
)

from .conftest import _exhaust_gen, _make_context, _make_gen, _make_synced_data

NOW = 1_700_000_000
QUESTION_ID = "0x" + "a1" * 32  # 32-byte hex
VALID_TX_HASH = "0x" + "b2" * 32


def _make_mech_response(
    nonce: str = "test-nonce",
    result: Optional[str] = None,
    error: Optional[str] = None,
    request_id: str = "0xReqId",
) -> MagicMock:
    resp = MagicMock()
    resp.nonce = nonce
    resp.result = result
    resp.error = error
    resp.requestId = request_id
    return resp


def _make_entry(
    evaluation: Optional[Dict] = None,
    mech_request: Optional[Dict] = None,
    on_chain_answer: Optional[str] = None,
    on_chain_bond: Optional[str] = None,
    last_answer_timestamp: Optional[str] = None,
    status: Any = AnswerStatus.NEEDS_ANSWER,
    pending_tx: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Create a minimal questions_db entry."""
    return {
        "status": status,
        "market_id": "0xM",
        "title": "Will X?",
        "question_id": QUESTION_ID,
        "detected_at": NOW,
        "on_chain_answer": on_chain_answer,
        "on_chain_bond": on_chain_bond,
        "last_answerer": "",
        "last_answer_timestamp": last_answer_timestamp,
        "market_closing_timestamp": 1_699_000_000,
        "realitio_timeout": 86400,
        "mech_request": mech_request,
        "mech_response": None,
        "evaluation": evaluation,
        "pending_tx": pending_tx,
        "mech_retries": 0,
    }


def _make_behaviour(
    questions_db: Optional[Dict] = None,
    selected_market_id: Optional[str] = "0xM",
    mech_responses: Optional[List] = None,
) -> BuildAnswerTxBehaviour:
    """Instantiate BuildAnswerTxBehaviour with mocked context + synchronized_data patched."""
    context = _make_context(questions_db or {})
    behaviour = BuildAnswerTxBehaviour(name="build_tx", skill_context=context)
    sd = _make_synced_data(
        selected_market_id=selected_market_id,
        mech_responses=mech_responses or [],
    )
    patcher = patch.object(
        type(behaviour),
        "synchronized_data",
        new_callable=PropertyMock,
        return_value=sd,
    )
    patcher.start()
    behaviour._sd_patcher = patcher
    return behaviour


def _run_async_act(behaviour: BuildAnswerTxBehaviour) -> None:
    """Drive async_act to completion."""
    gen = behaviour.async_act()
    try:
        while True:
            next(gen)
    except StopIteration:
        pass


# ---------------------------------------------------------------------------
# _decode_answer helper
# ---------------------------------------------------------------------------


class TestDecodeAnswer:
    """Tests for _decode_answer helper."""

    def test_yes(self) -> None:
        """YES answer decodes correctly."""
        assert _decode_answer(ANSWER_YES) == "YES"

    def test_no(self) -> None:
        """NO answer decodes correctly."""
        assert _decode_answer(ANSWER_NO) == "NO"

    def test_invalid(self) -> None:
        """INVALID answer decodes correctly."""
        assert _decode_answer(ANSWER_INVALID) == "INVALID"

    def test_unknown_returns_raw(self) -> None:
        """Unknown hex is returned as-is."""
        raw = "0x1234"
        assert _decode_answer(raw) == raw


# ---------------------------------------------------------------------------
# async_act -- entry-point guards
# ---------------------------------------------------------------------------


class TestBuildAnswerTxBehaviourGuards:
    """Tests for early-exit guards in async_act."""

    def test_no_selected_market_sends_none_payload(self) -> None:
        """No selected_market_id -> payload with tx_hash=None."""
        b = _make_behaviour(selected_market_id=None)
        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].tx_hash is None

    def test_market_not_in_db_sends_none_payload(self) -> None:
        """Market ID not found in DB -> payload with tx_hash=None."""
        b = _make_behaviour(questions_db={}, selected_market_id="0xM")
        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].tx_hash is None


# ---------------------------------------------------------------------------
# Mech response processing
# ---------------------------------------------------------------------------


class TestMechResponseProcessing:
    """Tests for Mech response handling in async_act."""

    def test_mech_response_matching_nonce_parsed(self) -> None:
        """Matching Mech response is parsed and stored in evaluation."""
        mech_resp = _make_mech_response(
            nonce="nonce-123",
            result=json.dumps(
                {"is_valid": True, "is_determinable": True, "has_occurred": True}
            ),
        )
        entry = _make_entry(
            mech_request={"nonce": "nonce-123", "tool": "t", "prompt": "Q"},
        )
        b = _make_behaviour(
            questions_db={"0xM": entry},
            mech_responses=[mech_resp],
        )

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)),
            patch.object(b, "_build_submit_answer_tx", new=_make_gen(VALID_TX_HASH)),
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        entry_after = b.questions_db["0xM"]
        assert entry_after["evaluation"] is not None
        assert entry_after["evaluation"]["answer"] == ANSWER_YES

    def test_garbage_mech_response_sets_retry_cooldown(self) -> None:
        """Unparseable Mech response sets retry_after cooldown."""
        mech_resp = _make_mech_response(nonce="nonce-x", result="{{not json")
        entry = _make_entry(
            mech_request={"nonce": "nonce-x", "tool": "t", "prompt": "Q"},
        )
        b = _make_behaviour(
            questions_db={"0xM": entry},
            mech_responses=[mech_resp],
        )
        b.context.params.mech_retry_cooldown = 3600

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        entry_after = b.questions_db["0xM"]
        assert entry_after["retry_after"] == NOW + 3600
        assert payload_sent[0].tx_hash is None

    def test_jury_error_discriminator_logged_distinctly(self) -> None:
        """A jury error-discriminator payload logs the discriminator, not 'could not parse'.

        The jury's ``all_voters_failed`` payload is *valid* JSON (so the
        old "Could not parse as JSON" warning was misleading); the new
        log path surfaces the discriminator so operators can tell an API
        outage from a real parser failure. Behaviour-wise this still
        routes through the garbage path (retry cooldown applied).
        """
        import json as _json

        jury_payload = _json.dumps(
            {
                "is_valid": None,
                "is_determinable": None,
                "has_occurred": None,
                "error": "all_voters_failed",
                "judge_reasoning": "All voters failed.",
            }
        )
        mech_resp = _make_mech_response(nonce="nonce-y", result=jury_payload)
        entry = _make_entry(
            mech_request={"nonce": "nonce-y", "tool": "t", "prompt": "Q"},
        )
        # The garbage path in build_answer_tx checks ``pending_nonce`` (not
        # ``mech_request["nonce"]``) to decide whether to apply a retry
        # cooldown -- see behaviours/build_answer_tx.py:107.
        entry["pending_nonce"] = "nonce-y"
        b = _make_behaviour(
            questions_db={"0xM": entry},
            mech_responses=[mech_resp],
        )
        b.context.params.mech_retry_cooldown = 3600

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # Behaviour unchanged: still garbage path -> retry cooldown.
        entry_after = b.questions_db["0xM"]
        assert entry_after["retry_after"] == NOW + 3600
        assert payload_sent[0].tx_hash is None

        # Log surface: at least one warning names the discriminator and
        # NO warning mentions "Could not parse as JSON" for this payload
        # (it was valid JSON, just off-contract).
        warnings = [str(c.args) for c in b.context.logger.warning.call_args_list]
        assert any("all_voters_failed" in w for w in warnings), warnings
        assert not any("Could not parse Mech result as JSON" in w for w in warnings)

    def test_no_mech_response_with_nonce_logs_no_evaluation(self) -> None:
        """Mech request nonce set but no matching response -> no tx, no cooldown."""
        entry = _make_entry(
            mech_request={"nonce": "nonce-missing", "tool": "t", "prompt": "Q"},
        )
        b = _make_behaviour(
            questions_db={"0xM": entry},
            mech_responses=[],  # empty -- no responses
        )

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # No nonce match -> evaluation still None -> sends none payload
        assert payload_sent[0].tx_hash is None

    def test_evaluation_already_set_skips_mech_response(self) -> None:
        """If evaluation is already populated, mech_responses are not consulted."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            mech_request={"nonce": "nonce-x", "tool": "t", "prompt": "Q"},
        )
        mech_resp = _make_mech_response(
            nonce="nonce-x", result=json.dumps({"is_valid": False})
        )
        b = _make_behaviour(
            questions_db={"0xM": entry},
            mech_responses=[mech_resp],
        )

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)),
            patch.object(b, "_build_submit_answer_tx", new=_make_gen(VALID_TX_HASH)),
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # evaluation should remain as the original YES, not get overwritten
        assert b.questions_db["0xM"]["evaluation"]["answer"] == ANSWER_YES

    def test_no_expected_nonce_skips_mech_response_lookup(self) -> None:
        """Entry with no mech_request nonce skips response lookup entirely."""
        mech_resp = _make_mech_response(nonce="some-nonce", result='{"is_valid":false}')
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
        )
        b = _make_behaviour(
            questions_db={"0xM": entry},
            mech_responses=[mech_resp],
        )

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)),
            patch.object(b, "_build_submit_answer_tx", new=_make_gen(VALID_TX_HASH)),
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # evaluation unchanged
        assert b.questions_db["0xM"]["evaluation"]["answer"] == ANSWER_YES

    def test_non_matching_nonce_iterates_without_match(self) -> None:
        """Mech responses present but none match the expected nonce."""
        other_resp = _make_mech_response(
            nonce="other-nonce", result='{"is_valid":true}'
        )
        entry = _make_entry(
            mech_request={"nonce": "expected-nonce", "tool": "t", "prompt": "Q"},
        )
        b = _make_behaviour(
            questions_db={"0xM": entry},
            mech_responses=[other_resp],
        )

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # Loop iterated over non-matching response -> evaluation still None
        # -> garbage-cooldown branch (expected_nonce present) -> none payload
        assert b.questions_db["0xM"]["evaluation"] is None
        assert payload_sent[0].tx_hash is None

    def test_no_evaluation_no_nonce_logs_unavailable(self) -> None:
        """No evaluation AND no mech_request nonce -> just log, no retry_after."""
        entry = _make_entry(evaluation=None, mech_request=None)
        b = _make_behaviour(questions_db={"0xM": entry})

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # No expected_nonce path: no retry_after set, just a log + none payload
        assert "retry_after" not in b.questions_db["0xM"]
        assert payload_sent[0].tx_hash is None


# ---------------------------------------------------------------------------
# Case B: undeterminable (answer=None)
# ---------------------------------------------------------------------------


class TestUndeterminableAnswer:
    """Tests for the undeterminable (answer=None) path."""

    def test_undeterminable_with_on_chain_answer_uses_cooldown_formula(self) -> None:
        """Undeterminable + existing answer -> retry_after uses cooldown formula."""
        last_ts = NOW - 1000
        entry = _make_entry(
            evaluation={
                "answer": None,
                "is_valid": True,
                "is_determinable": False,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=ANSWER_NO,
            on_chain_bond="1000000000000000000",
            last_answer_timestamp=str(last_ts),
        )
        b = _make_behaviour(questions_db={"0xM": entry})
        b.context.params.mech_retry_cooldown = 3600
        b.context.params.challenge_cooldown_fraction = 0.25

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].tx_hash is None
        entry_after = b.questions_db["0xM"]
        # retry_after >= now + cooldown (floor applied)
        assert entry_after["retry_after"] >= NOW + 3600

    def test_undeterminable_no_on_chain_answer_sets_24h_retry(self) -> None:
        """Undeterminable with no on-chain answer -> retry_after = now + 86400."""
        entry = _make_entry(
            evaluation={
                "answer": None,
                "is_valid": True,
                "is_determinable": False,
                "agrees_with_on_chain": None,
            },
        )
        b = _make_behaviour(questions_db={"0xM": entry})
        b.context.params.mech_retry_cooldown = 3600

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].tx_hash is None
        assert b.questions_db["0xM"]["retry_after"] == NOW + 86400


# ---------------------------------------------------------------------------
# Pre-flight on-chain refresh
# ---------------------------------------------------------------------------


class TestPreFlightRefresh:
    """Tests for _read_onchain_answer_and_bond pre-flight check."""

    def test_updated_on_chain_state_is_logged(self) -> None:
        """When on-chain state changed since scan, it is refreshed."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=ANSWER_NO,
            on_chain_bond="1000000000000000000",
        )
        # Fresh read returns same answer but different bond
        b = _make_behaviour(questions_db={"0xM": entry})

        with (
            patch.object(
                b,
                "_read_onchain_answer_and_bond",
                new=_make_gen((ANSWER_YES, 2000000000000000000)),
            ),
            patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)),
            patch.object(b, "_build_submit_answer_tx", new=_make_gen(None)),
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # logger.info should have been called (we don't assert exact text)
        assert b.context.logger.info.called

    def test_unchanged_on_chain_state_skips_refresh_log(self) -> None:
        """When fresh on-chain values match the scan, no 'changed' log is emitted."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=ANSWER_NO,
            on_chain_bond="1000000000000000000",
        )
        b = _make_behaviour(questions_db={"0xM": entry})

        with (
            patch.object(
                b,
                "_read_onchain_answer_and_bond",
                new=_make_gen((ANSWER_NO, 1000000000000000000)),
            ),
            patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)),
            patch.object(b, "_build_submit_answer_tx", new=_make_gen(VALID_TX_HASH)),
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # The "on-chain state changed since scan" branch must NOT have fired.
        logged = [str(c.args) for c in b.context.logger.info.call_args_list]
        assert not any("on-chain state changed since scan" in msg for msg in logged)

    def test_read_onchain_failure_uses_stale_values(self) -> None:
        """When _read_onchain_answer_and_bond returns None, stale scan values used."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=None,
        )
        b = _make_behaviour(questions_db={"0xM": entry})

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)),
            patch.object(b, "_build_submit_answer_tx", new=_make_gen(VALID_TX_HASH)),
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # Proceeds with stale values (no crash)
        assert b.questions_db["0xM"]["evaluation"] is not None

    def test_no_question_id_skips_preflight(self) -> None:
        """Entry with no question_id skips _read_onchain_answer_and_bond."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
        )
        entry["question_id"] = None  # no question id
        b = _make_behaviour(questions_db={"0xM": entry})

        mock_read = MagicMock()

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=mock_read),
            patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)),
            patch.object(b, "_build_submit_answer_tx", new=_make_gen(VALID_TX_HASH)),
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        mock_read.assert_not_called()


# ---------------------------------------------------------------------------
# Agreement check
# ---------------------------------------------------------------------------


class TestAgreementCheck:
    """Tests for agrees / disagrees with on-chain answer."""

    def test_agrees_with_on_chain_marks_verified(self) -> None:
        """Mech agrees with on-chain answer -> VERIFIED, no tx."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=ANSWER_YES,
            on_chain_bond="1000000000000000000",
        )
        b = _make_behaviour(questions_db={"0xM": entry})

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert b.questions_db["0xM"]["status"] == AnswerStatus.VERIFIED
        assert payload_sent[0].tx_hash is None

    def test_no_on_chain_answer_submits_fresh_answer(self) -> None:
        """No on-chain answer -> submit fresh answer tx."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=None,
            on_chain_bond=None,
        )
        b = _make_behaviour(questions_db={"0xM": entry})

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)),
            patch.object(b, "_build_submit_answer_tx", new=_make_gen(VALID_TX_HASH)),
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].tx_hash == VALID_TX_HASH
        assert b.questions_db["0xM"]["status"] == AnswerStatus.TRANSACTION_PENDING


# ---------------------------------------------------------------------------
# Bond gate
# ---------------------------------------------------------------------------


class TestBondGate:
    """Tests for required_bond > max_challenge_bond and balance checks."""

    def test_bond_exceeds_max_skips_tx(self) -> None:
        """required_bond > max_challenge_bond -> no tx, evaluation cached."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=ANSWER_NO,
            on_chain_bond=str(9 * 10**18),  # required = 18 xDAI
        )
        b = _make_behaviour(questions_db={"0xM": entry})
        b.context.params.max_challenge_bond = 16 * 10**18

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].tx_hash is None

    def test_bond_exceeds_safe_balance_skips_tx(self) -> None:
        """required_bond > safe_balance -> no tx."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=ANSWER_NO,
            on_chain_bond=str(10**18),  # required = 2 xDAI
        )
        b = _make_behaviour(questions_db={"0xM": entry})
        b.context.params.max_challenge_bond = 16 * 10**18

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "get_native_balance", new=_make_gen(10**17)),  # 0.1 xDAI
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].tx_hash is None

    def test_safe_balance_none_treated_as_zero(self) -> None:
        """get_native_balance returning None -> safe_balance=0 -> tx skipped."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=ANSWER_NO,
            on_chain_bond=str(10**18),
        )
        b = _make_behaviour(questions_db={"0xM": entry})
        b.context.params.max_challenge_bond = 16 * 10**18

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "get_native_balance", new=_make_gen(None)),
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].tx_hash is None

    def test_build_tx_failure_sends_none_payload(self) -> None:
        """_build_submit_answer_tx returning None -> no tx."""
        entry = _make_entry(
            evaluation={
                "answer": ANSWER_YES,
                "is_valid": True,
                "agrees_with_on_chain": None,
            },
            on_chain_answer=None,
        )
        b = _make_behaviour(questions_db={"0xM": entry})

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "_read_onchain_answer_and_bond", new=_make_gen(None)),
            patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)),
            patch.object(b, "_build_submit_answer_tx", new=_make_gen(None)),
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].tx_hash is None


# ---------------------------------------------------------------------------
# _read_onchain_answer_and_bond
# ---------------------------------------------------------------------------


class TestReadOnchainAnswerAndBond:
    """Tests for _read_onchain_answer_and_bond."""

    def _make_behaviour(self) -> BuildAnswerTxBehaviour:
        context = _make_context({})
        b = BuildAnswerTxBehaviour(name="build_tx", skill_context=context)
        sd = _make_synced_data()
        patcher = patch.object(
            type(b),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=sd,
        )
        patcher.start()
        b._sd_patcher = patcher
        return b

    def _make_state_resp(self, body: Dict) -> MagicMock:
        resp = MagicMock()
        resp.performative = ContractApiMessage.Performative.STATE
        resp.state = MagicMock()
        resp.state.body = body
        return resp

    def test_invalid_hex_returns_none(self) -> None:
        """Malformed question_id hex returns None."""
        b = self._make_behaviour()
        result = _exhaust_gen(b._read_onchain_answer_and_bond("not-hex"))
        assert result is None

    def test_answer_api_error_returns_none(self) -> None:
        """get_best_answer API failure returns None."""
        b = self._make_behaviour()
        err_resp = MagicMock()
        err_resp.performative = ContractApiMessage.Performative.ERROR

        with patch.object(b, "get_contract_api_response", new=_make_gen(err_resp)):
            result = _exhaust_gen(b._read_onchain_answer_and_bond(QUESTION_ID))

        assert result is None

    def test_bond_api_error_returns_none(self) -> None:
        """get_bond API failure returns None."""
        b = self._make_behaviour()
        answer_resp = self._make_state_resp({"best_answer": ANSWER_YES})
        err_resp = MagicMock()
        err_resp.performative = ContractApiMessage.Performative.ERROR

        call_count = 0

        def mock_contract_api(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return answer_resp
            return err_resp
            yield  # noqa

        with patch.object(b, "get_contract_api_response", new=mock_contract_api):
            result = _exhaust_gen(b._read_onchain_answer_and_bond(QUESTION_ID))

        assert result is None

    def test_zero_sentinel_and_zero_bond_returns_none_zero(self) -> None:
        """Zero answer + zero bond means unanswered -> returns (None, 0)."""
        b = self._make_behaviour()
        zero_answer = "0x" + "00" * 32
        answer_resp = self._make_state_resp({"best_answer": zero_answer})
        bond_resp = self._make_state_resp({"bond": 0})

        call_count = 0

        def mock_contract_api(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return answer_resp
            return bond_resp
            yield  # noqa

        with patch.object(b, "get_contract_api_response", new=mock_contract_api):
            result = _exhaust_gen(b._read_onchain_answer_and_bond(QUESTION_ID))

        assert result == (None, 0)

    def test_success_returns_answer_and_bond(self) -> None:
        """Returns (best_answer, bond) on success."""
        b = self._make_behaviour()
        answer_resp = self._make_state_resp({"best_answer": ANSWER_YES})
        bond_resp = self._make_state_resp({"bond": 10**18})

        call_count = 0

        def mock_contract_api(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return answer_resp
            return bond_resp
            yield  # noqa

        with patch.object(b, "get_contract_api_response", new=mock_contract_api):
            result = _exhaust_gen(b._read_onchain_answer_and_bond(QUESTION_ID))

        assert result == (ANSWER_YES, 10**18)

    def test_none_answer_resp_returns_none(self) -> None:
        """None answer response returns None."""
        b = self._make_behaviour()
        with patch.object(b, "get_contract_api_response", new=_make_gen(None)):
            result = _exhaust_gen(b._read_onchain_answer_and_bond(QUESTION_ID))
        assert result is None


# ---------------------------------------------------------------------------
# _build_submit_answer_tx
# ---------------------------------------------------------------------------


class TestBuildSubmitAnswerTx:
    """Tests for _build_submit_answer_tx."""

    def _make_behaviour(self) -> BuildAnswerTxBehaviour:
        context = _make_context({})
        b = BuildAnswerTxBehaviour(name="build_tx", skill_context=context)
        sd = _make_synced_data()
        patcher = patch.object(
            type(b),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=sd,
        )
        patcher.start()
        b._sd_patcher = patcher
        return b

    def test_invalid_hex_returns_none(self) -> None:
        """Invalid hex for question_id returns None."""
        b = self._make_behaviour()
        result = _exhaust_gen(
            b._build_submit_answer_tx("not-hex", ANSWER_YES, 0, 10**18)
        )
        assert result is None

    def test_realitio_api_failure_returns_none(self) -> None:
        """Realitio submitAnswer API failure returns None."""
        b = self._make_behaviour()
        err_resp = MagicMock()
        err_resp.performative = ContractApiMessage.Performative.ERROR

        with patch.object(b, "get_contract_api_response", new=_make_gen(err_resp)):
            result = _exhaust_gen(
                b._build_submit_answer_tx(QUESTION_ID, ANSWER_YES, 0, 10**18)
            )
        assert result is None

    def test_multisend_api_failure_returns_none(self) -> None:
        """Multisend API failure returns None."""
        b = self._make_behaviour()
        state_resp = MagicMock()
        state_resp.performative = ContractApiMessage.Performative.STATE
        state_resp.state.body = {"data": b"calldata"}

        multisend_err = MagicMock()
        multisend_err.performative = ContractApiMessage.Performative.ERROR

        call_count = 0

        def mock_contract_api(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return state_resp
            return multisend_err
            yield  # noqa

        with patch.object(b, "get_contract_api_response", new=mock_contract_api):
            result = _exhaust_gen(
                b._build_submit_answer_tx(QUESTION_ID, ANSWER_YES, 0, 10**18)
            )
        assert result is None

    def test_safe_tx_hash_failure_returns_none(self) -> None:
        """Safe tx hash failure returns None."""
        b = self._make_behaviour()
        state_resp = MagicMock()
        state_resp.performative = ContractApiMessage.Performative.STATE
        state_resp.state.body = {"data": b"calldata"}

        raw_tx_resp = MagicMock()
        raw_tx_resp.performative = ContractApiMessage.Performative.RAW_TRANSACTION
        raw_tx_resp.raw_transaction.body = {"data": "0xabcd"}

        call_count = 0

        def mock_contract_api(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return state_resp
            return raw_tx_resp
            yield  # noqa

        with (
            patch.object(b, "get_contract_api_response", new=mock_contract_api),
            patch.object(b, "_get_safe_tx_hash", new=_make_gen(None)),
        ):
            result = _exhaust_gen(
                b._build_submit_answer_tx(QUESTION_ID, ANSWER_YES, 0, 10**18)
            )
        assert result is None

    def test_success_returns_payload_hash(self) -> None:
        """Full happy path returns a non-None payload hash string."""
        b = self._make_behaviour()
        state_resp = MagicMock()
        state_resp.performative = ContractApiMessage.Performative.STATE
        state_resp.state.body = {"data": b"calldata"}

        raw_tx_resp = MagicMock()
        raw_tx_resp.performative = ContractApiMessage.Performative.RAW_TRANSACTION
        raw_tx_resp.raw_transaction.body = {"data": "0xdeadbeef"}

        call_count = 0

        def mock_contract_api(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return state_resp
            return raw_tx_resp
            yield  # noqa

        # safe_tx_hash must be 32 bytes (64 hex chars) for payload_tools encoding
        safe_tx_hex = "a" * 64
        with (
            patch.object(b, "get_contract_api_response", new=mock_contract_api),
            patch.object(b, "_get_safe_tx_hash", new=_make_gen(safe_tx_hex)),
        ):
            result = _exhaust_gen(
                b._build_submit_answer_tx(QUESTION_ID, ANSWER_YES, 0, 10**18)
            )

        assert result is not None
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _get_safe_tx_hash
# ---------------------------------------------------------------------------


class TestGetSafeTxHash:
    """Tests for _get_safe_tx_hash."""

    def _make_behaviour(self) -> BuildAnswerTxBehaviour:
        context = _make_context({})
        b = BuildAnswerTxBehaviour(name="build_tx", skill_context=context)
        sd = _make_synced_data()
        patcher = patch.object(
            type(b),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=sd,
        )
        patcher.start()
        b._sd_patcher = patcher
        return b

    def test_success_strips_0x_prefix(self) -> None:
        """Returned tx_hash has 0x prefix stripped."""
        b = self._make_behaviour()
        resp = MagicMock()
        resp.performative = ContractApiMessage.Performative.STATE
        resp.state.body = {"tx_hash": "0xdeadbeef"}

        with patch.object(b, "get_contract_api_response", new=_make_gen(resp)):
            result = _exhaust_gen(b._get_safe_tx_hash("0xTo", b"data", 0, 1))

        assert result == "deadbeef"

    def test_success_no_0x_prefix(self) -> None:
        """tx_hash without 0x prefix is returned as-is."""
        b = self._make_behaviour()
        resp = MagicMock()
        resp.performative = ContractApiMessage.Performative.STATE
        resp.state.body = {"tx_hash": "deadbeef"}

        with patch.object(b, "get_contract_api_response", new=_make_gen(resp)):
            result = _exhaust_gen(b._get_safe_tx_hash("0xTo", b"data", 0, 1))

        assert result == "deadbeef"

    def test_error_response_returns_none(self) -> None:
        """Error performative returns None."""
        b = self._make_behaviour()
        resp = MagicMock()
        resp.performative = ContractApiMessage.Performative.ERROR

        with patch.object(b, "get_contract_api_response", new=_make_gen(resp)):
            result = _exhaust_gen(b._get_safe_tx_hash("0xTo", b"data", 0, 1))

        assert result is None

    def test_none_response_returns_none(self) -> None:
        """None response returns None."""
        b = self._make_behaviour()
        with patch.object(b, "get_contract_api_response", new=_make_gen(None)):
            result = _exhaust_gen(b._get_safe_tx_hash("0xTo", b"data", 0, 1))
        assert result is None


# ---------------------------------------------------------------------------
# _send_payload tx_submitter assignment
# ---------------------------------------------------------------------------


class TestSendPayload:
    """Tests for _send_payload correctly setting tx_submitter."""

    def test_tx_hash_set_sets_tx_submitter(self) -> None:
        """When tx_hash is set, tx_submitter = matching_round.auto_round_id()."""
        b = _make_behaviour()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _exhaust_gen(b._send_payload(VALID_TX_HASH))

        assert payload_sent[0].tx_submitter is not None
        assert payload_sent[0].tx_hash == VALID_TX_HASH

    def test_tx_hash_none_sets_tx_submitter_none(self) -> None:
        """When tx_hash is None, tx_submitter is None."""
        b = _make_behaviour()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _exhaust_gen(b._send_payload(None))

        assert payload_sent[0].tx_submitter is None
        assert payload_sent[0].tx_hash is None
