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

"""Tests for KvStoreHandler dispatch."""

# pylint: disable=too-few-public-methods,protected-access

from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import packages.valory.skills.market_resolution_manager_abci.handlers  # noqa: F401
from packages.valory.protocols.kv_store.message import KvStoreMessage
from packages.valory.skills.market_resolution_manager_abci.handlers import (
    KvStoreHandler,
)


def test_import() -> None:
    """Sanity: the handlers module imports cleanly."""


class _State:
    """Minimal SharedState stub carrying just the fields the handler touches."""

    def __init__(self) -> None:
        """Initialise the two fields KvStoreHandler mutates."""
        self.in_flight_req: bool = False
        self.req_to_callback: Dict[str, Tuple[Any, Dict[str, Any]]] = {}


def _make_handler() -> Tuple[KvStoreHandler, _State, MagicMock]:
    """Build a KvStoreHandler wired to a stub context."""
    # Returns (handler, state, kv_store_dialogues_mock). The dialogues
    # mock's ``update`` return value is the ``dialogue`` argument the
    # registered callback will receive; tests can inspect ``.update``
    # to verify it was invoked, or replace it before firing.
    context = MagicMock()
    context.state = _State()
    context.kv_store_dialogues = MagicMock()
    handler = KvStoreHandler(name="kv_store", skill_context=context)
    return handler, context.state, context.kv_store_dialogues


def _make_msg(
    performative: Any,
    nonce: str = "nonce-abc",
) -> SimpleNamespace:
    """Build a minimal KvStoreMessage-shaped reply."""
    # ``performative`` typed as Any because ``KvStoreMessage.Performative``
    # is a str-Enum and CI mypy on 3.10 sees Enum members as ``str`` at
    # the call site, refusing to narrow them to the enum class. Tests
    # pass real Performative instances.
    return SimpleNamespace(
        performative=performative,
        dialogue_reference=(nonce, "responder"),
        data={},
        next_cursor="",
    )


class TestKvStoreHandlerDispatch:
    """Cover the nonce lookup + callback dispatch protocol.

    Previously these paths were only exercised indirectly via
    ``_send_kv_*`` stubs in the behaviour tests, which meant a regression
    to the handler's dispatch would have gone unnoticed until the FSM
    hit it in production. bennyjo flagged this in PR #36.
    """

    def test_success_reply_invokes_registered_callback(self) -> None:
        """A SUCCESS reply for a registered nonce fires the callback + clears gate."""
        handler, state, dialogues = _make_handler()
        state.in_flight_req = True
        seen: List[Any] = []

        def _cb(reply: Any, dialogue: Any) -> None:
            seen.append((reply, dialogue))

        state.req_to_callback["nonce-abc"] = (_cb, {})

        msg = _make_msg(KvStoreMessage.Performative.SUCCESS, nonce="nonce-abc")
        handler.handle(msg)

        assert len(seen) == 1
        assert seen[0][0] is msg
        assert state.in_flight_req is False
        # Callback popped so a second reply for the same nonce is inert.
        assert "nonce-abc" not in state.req_to_callback

    def test_list_response_reply_invokes_callback(self) -> None:
        """LIST_RESPONSE is one of the reply performatives the handler dispatches."""
        # Pinned separately because the earlier optimus KvStoreHandler
        # (the pattern this one is modeled on) did NOT have LIST_RESPONSE
        # in its allowed set -- LIST landed in kv-store v0.7.0-rc1 and our
        # handler had to add it. Regression pin.
        handler, state, _dialogues = _make_handler()
        state.in_flight_req = True
        fired = {"n": 0}

        def _cb(_reply: Any, _dlg: Any) -> None:
            fired["n"] += 1

        state.req_to_callback["nonce-list"] = (_cb, {})

        msg = _make_msg(KvStoreMessage.Performative.LIST_RESPONSE, nonce="nonce-list")
        handler.handle(msg)

        assert fired["n"] == 1
        assert state.in_flight_req is False

    def test_error_reply_still_invokes_callback(self) -> None:
        """ERROR is a valid dispatched reply -- callers observe outcome via callback."""
        # Pinned so a "fast fail" refactor that filtered ERROR out of the
        # dispatch loop wouldn't silently regress -- the ``_send_kv_*``
        # helpers rely on the ERROR path invoking their callback so they
        # can return ``False`` and let the caller act on it.
        handler, state, _dialogues = _make_handler()
        state.in_flight_req = True
        errors: List[Any] = []

        def _cb(reply: Any, _dlg: Any) -> None:
            errors.append(reply.performative)

        state.req_to_callback["nonce-err"] = (_cb, {})

        msg = _make_msg(KvStoreMessage.Performative.ERROR, nonce="nonce-err")
        handler.handle(msg)

        assert errors == [KvStoreMessage.Performative.ERROR]
        assert state.in_flight_req is False

    def test_late_reply_for_popped_nonce_does_not_clobber_gate(self) -> None:
        """A reply whose nonce is no longer registered leaves state alone.

        This is bennyjo's #1 bug from PR #36. Without the pop-on-timeout
        in ``_wait_for_kv_reply``, a reply arriving after the watchdog
        gave up would (a) invoke a dead callback and (b) flip the gate
        to False while the NEXT kv op was already in flight -- letting
        that op's ``while state.in_flight_req`` loop exit early on a
        partial result. This test pins the handler side: when the
        callback has been popped, a late reply must NOT touch
        ``in_flight_req``.
        """
        handler, state, _dialogues = _make_handler()
        # Simulate a NEW op that started after the previous one timed out.
        state.in_flight_req = True
        # No callback registered for the previous op's nonce (popped on
        # timeout by _wait_for_kv_reply).
        assert "nonce-stale" not in state.req_to_callback

        msg = _make_msg(KvStoreMessage.Performative.SUCCESS, nonce="nonce-stale")
        handler.handle(msg)

        # The current op's gate is untouched -- otherwise the caller's
        # wait loop would exit early.
        assert state.in_flight_req is True

    def test_unrecognized_performative_clears_gate(self) -> None:
        """A reply outside the allowed set clears the gate + logs a warning."""
        # Defensive: if a future kv-store protocol bump adds a new
        # performative and the handler hasn't been updated, we don't
        # want to hold the gate forever -- but we also don't want to
        # invoke a stale callback. The current behaviour (clear the
        # gate, don't touch callbacks) is what's under test.
        handler, state, _dialogues = _make_handler()
        state.in_flight_req = True

        # There's no KvStoreMessage.Performative outside the allowed set
        # today, so simulate one with a sentinel object.
        bad_msg = SimpleNamespace(
            performative="not-a-real-performative",
            dialogue_reference=("nonce-x", "y"),
        )
        handler.handle(bad_msg)

        assert state.in_flight_req is False
