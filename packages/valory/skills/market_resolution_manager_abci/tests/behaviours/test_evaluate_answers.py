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

"""Tests for behaviours/evaluate_answers.py."""

# pylint: disable=protected-access

import json
from typing import Any, Dict, Optional
from unittest.mock import PropertyMock, patch

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    ANSWER_YES,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.evaluate_answers import (
    EvaluateAnswersBehaviour,
)

from .conftest import _make_context, _make_gen, _make_synced_data

NOW = 1_700_000_000


def _make_behaviour(
    questions_db: Optional[Dict] = None,
    selected_market_id: Optional[str] = None,
    selected_market_action: Optional[str] = None,
) -> EvaluateAnswersBehaviour:
    """Instantiate EvaluateAnswersBehaviour with mocked context + synchronized_data patched."""
    context = _make_context(questions_db or {})
    behaviour = EvaluateAnswersBehaviour(name="eval", skill_context=context)
    sd = _make_synced_data(
        selected_market_id=selected_market_id,
        selected_market_action=selected_market_action,
    )
    patcher = patch.object(
        type(behaviour),
        "synchronized_data",
        new_callable=PropertyMock,
        return_value=sd,
    )
    patcher.start()
    behaviour._sd_patcher = patcher

    # The kv_store fire-write is best-effort from the FSM's perspective;
    # these tests cover retry-counter + payload logic, so stub it out.
    # Handler dispatch and wait/timeout coverage live in
    # tests/test_handlers.py::TestKvStoreHandlerDispatch and
    # tests/behaviours/test_base.py::TestWaitForKvReplyTimeout.
    def _noop_gen(*_a: Any, **_k: Any):  # type: ignore[no-untyped-def]
        if False:  # pragma: no cover -- keep this a generator
            yield None

    behaviour._buffer_mech_request_fired = _noop_gen  # type: ignore[assignment]
    return behaviour


def _run_async_act(behaviour: EvaluateAnswersBehaviour) -> None:
    """Drive async_act to completion."""
    gen = behaviour.async_act()
    try:
        while True:
            next(gen)
    except StopIteration:
        pass


def _base_entry(
    evaluation: Optional[Dict] = None,
    mech_retries: int = 0,
    title: str = "Will X?",
) -> Dict[str, Any]:
    """Return a minimal questions_db entry."""
    return {
        "status": None,
        "market_id": "0xM",
        "title": title,
        "question_id": "0xQ",
        "detected_at": NOW,
        "on_chain_answer": None,
        "on_chain_bond": None,
        "last_answerer": "",
        "last_answer_timestamp": None,
        "market_closing_timestamp": 1_699_000_000,
        "realitio_timeout": 86400,
        "mech_request": None,
        "mech_response": None,
        "evaluation": evaluation,
        "pending_tx": None,
        "mech_retries": mech_retries,
    }


class TestEvaluateAnswersBehaviour:
    """Tests for EvaluateAnswersBehaviour.async_act."""

    def test_no_selected_market_sends_none_payload(self) -> None:
        """No selected_market_id -> payload with both fields None."""
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
            _run_async_act(b)

        assert len(payload_sent) == 1
        assert payload_sent[0].mech_requests is None
        assert payload_sent[0].evaluation_result is None

    def test_market_not_in_db_sends_none_payload(self) -> None:
        """Market ID present but not in DB -> payload with both fields None."""
        b = _make_behaviour(
            questions_db={"0xOther": _base_entry()},
            selected_market_id="0xMissing",
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

        assert payload_sent[0].mech_requests is None
        assert payload_sent[0].evaluation_result is None

    def test_existing_evaluation_sends_evaluation_result(self) -> None:
        """Entry with evaluation already set -> sends evaluation_result (skips Mech)."""
        entry = _base_entry(evaluation={"answer": ANSWER_YES, "is_valid": True})
        entry["status"] = "TRANSACTION_PENDING"
        b = _make_behaviour(
            questions_db={"0xM": entry},
            selected_market_id="0xM",
            selected_market_action="TRANSACTION_PENDING",
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

        assert payload_sent[0].mech_requests is None
        assert payload_sent[0].evaluation_result == "TRANSACTION_PENDING"

    def test_max_retries_sends_none_payload(self) -> None:
        """Entry at max_mech_retries -> payload with both fields None."""
        b = _make_behaviour(
            questions_db={"0xM": _base_entry(mech_retries=10)},
            selected_market_id="0xM",
        )
        b.context.params.max_mech_retries = 10

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

        assert payload_sent[0].mech_requests is None
        assert payload_sent[0].evaluation_result is None

    def test_retry_cap_gate_survives_none_mech_retries(self) -> None:
        """Retry-cap gate treats ``None`` mech_retries as ``0`` and fires a fresh Mech request."""
        entry = _base_entry()
        entry["mech_retries"] = None  # type: ignore[assignment]
        b = _make_behaviour(
            questions_db={"0xM": entry},
            selected_market_id="0xM",
        )
        b.context.params.max_mech_retries = 10

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

        # Behaviour completed without TypeError; a fresh Mech request
        # was prepared (mech_requests populated, retries bumped to 1
        # from the coerced base value of 0).
        assert len(payload_sent) == 1
        assert payload_sent[0].mech_requests is not None
        assert b.questions_db["0xM"]["mech_retries"] == 1

    def test_no_title_sends_none_payload(self) -> None:
        """Entry with empty title -> payload with both fields None."""
        entry = _base_entry(title="")
        b = _make_behaviour(
            questions_db={"0xM": entry},
            selected_market_id="0xM",
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

        assert payload_sent[0].mech_requests is None
        assert payload_sent[0].evaluation_result is None

    def test_fresh_mech_request_sends_mech_requests(self) -> None:
        """Entry needing Mech -> sends mech_requests JSON."""
        entry = _base_entry()
        b = _make_behaviour(
            questions_db={"0xM": entry},
            selected_market_id="0xM",
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

        assert payload_sent[0].mech_requests is not None
        assert payload_sent[0].evaluation_result is None

        # Check the JSON is valid MechMetadata
        requests = json.loads(payload_sent[0].mech_requests)
        assert len(requests) == 1
        assert requests[0]["tool"] == "resolve-market-jury-v1"
        assert requests[0]["prompt"] == "Will X?"
        assert "nonce" in requests[0]

    def test_mech_request_increments_retry_counter(self) -> None:
        """Sending a Mech request increments mech_retries in DB."""
        entry = _base_entry(mech_retries=2)
        b = _make_behaviour(
            questions_db={"0xM": entry},
            selected_market_id="0xM",
        )

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert b.questions_db["0xM"]["mech_retries"] == 3

    def test_mech_request_stores_pending_nonce_in_db(self) -> None:
        """The fired Mech request's nonce is stored on the DB entry.

        Refactor: ``evaluate_answers`` no longer persists the full
        ``mech_request`` dict (tool / prompt / nonce) -- only the nonce
        via ``entry['pending_nonce']``. The canonical source for tool +
        prompt is now the subgraph re-fetch in ``scan_markets`` (see
        ``fetch_mech_requests_for_market`` -> ``mech_requests``).
        """
        entry = _base_entry()
        b = _make_behaviour(
            questions_db={"0xM": entry},
            selected_market_id="0xM",
        )

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert b.questions_db["0xM"]["pending_nonce"] is not None
        # The nonce is a UUID4 string at production (see
        # evaluate_answers.py:96-118). We only assert it's a non-empty
        # string -- the value itself is irrelevant to this test.
        assert isinstance(b.questions_db["0xM"]["pending_nonce"], str)
        assert b.questions_db["0xM"]["pending_nonce"] != ""
