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
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import PropertyMock, patch

from packages.valory.skills.market_resolution_manager_abci import mech_cache
from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    ANSWER_YES,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.evaluate_answers import (
    EvaluateAnswersBehaviour,
    MAX_KV_WRITE_ATTEMPTS,
)

from .conftest import (
    SAFE_ADDRESS,
    _exhaust_gen,
)
from .conftest import _make_behaviour as _make_shared_behaviour
from .conftest import (
    _make_context,
    _make_gen,
    _make_synced_data,
)

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


class _FlakyKvWrite:
    """kv-write double: scripted per-call outcomes, records each (key, value).

    Stands in for ``_send_kv_write`` at the process boundary; the retry
    loop, key derivation and row serialization under test all run for
    real.
    """

    def __init__(self, outcomes: List[bool]) -> None:
        self.outcomes = list(outcomes)
        self.calls: List[Tuple[str, str]] = []

    def __call__(self, key: str, value: str) -> Any:
        """Record the write and return the next scripted outcome."""
        self.calls.append((key, value))
        return self.outcomes.pop(0)
        yield  # noqa: unreachable -- makes this a generator function


class TestBufferMechRequestFiredRetry:
    """Tests for the retried fire-time kv write (spec 5.3)."""

    _MARKET_ID = "0xM"
    _NONCE = "n1"
    _PROMPT = "Will X?"
    _FIRED_AT = NOW

    def _run(self, kv: _FlakyKvWrite) -> EvaluateAnswersBehaviour:
        """Run ``_buffer_mech_request_fired`` against the kv double."""
        # The shared factory does NOT stub _buffer_mech_request_fired
        # (unlike this module's local one) -- the retry loop is the
        # subject under test here.
        b = _make_shared_behaviour(EvaluateAnswersBehaviour)
        with (
            patch.object(b, "_send_kv_write", new=kv),
            patch.object(b, "sleep", new=_make_gen(None)),
        ):
            _exhaust_gen(
                b._buffer_mech_request_fired(
                    market_id=self._MARKET_ID,
                    nonce=self._NONCE,
                    prompt=self._PROMPT,
                    fired_at=self._FIRED_AT,
                )
            )
        return b

    def _warning_messages(self, b: EvaluateAnswersBehaviour) -> List[str]:
        return [c.args[0] for c in b.context.logger.warning.call_args_list]

    def test_transient_failure_retries_until_success(self) -> None:
        """Two failures then a success -> 3 identical writes, no give-up warning."""
        kv = _FlakyKvWrite([False, False, True])

        b = self._run(kv)

        assert len(kv.calls) == MAX_KV_WRITE_ATTEMPTS
        # Every attempt must retry the SAME row -- a drifting key or
        # value would fragment the "have I asked this market?" record.
        assert len(set(kv.calls)) == 1
        key, value = kv.calls[0]
        assert key == mech_cache.cache_key(
            prefix="market_resolver/",
            safe_address=SAFE_ADDRESS,
            market_id=self._MARKET_ID,
            nonce=self._NONCE,
        )
        row = json.loads(value)
        assert row["nonce"] == self._NONCE
        assert row["prompt"] == self._PROMPT
        assert row["fired_at"] == self._FIRED_AT
        assert row["delivered_at"] is None
        warnings = self._warning_messages(b)
        assert sum("retrying" in w for w in warnings) == 2
        assert not any("failed after" in w for w in warnings)

    def test_first_attempt_success_writes_once(self) -> None:
        """A healthy store gets exactly one write and zero warnings."""
        kv = _FlakyKvWrite([True])

        b = self._run(kv)

        assert len(kv.calls) == 1
        assert self._warning_messages(b) == []

    def test_all_attempts_fail_swallows_with_final_warning(self) -> None:
        """All attempts fail -> bounded at MAX_KV_WRITE_ATTEMPTS, final warning, no raise.

        The FSM must still transition into the mech request round; the
        miss is bounded by max_mech_retries (see the production
        docstring), so the loop swallows the failure instead of raising.
        """
        kv = _FlakyKvWrite([False] * MAX_KV_WRITE_ATTEMPTS)

        b = self._run(kv)

        assert len(kv.calls) == MAX_KV_WRITE_ATTEMPTS
        warnings = self._warning_messages(b)
        assert any("failed after" in w for w in warnings)
