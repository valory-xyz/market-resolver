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

"""Shared fixtures for behaviour tests."""

# pylint: disable=protected-access,unused-argument

from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

SAFE_ADDRESS = "0x" + "5a" * 20
AGENT_ADDRESS = "0x" + "a9" * 20


def _make_context(questions_db: Any = None) -> MagicMock:
    """Create a minimal mocked skill context."""
    context = MagicMock()
    context.agent_address = AGENT_ADDRESS
    context.logger = MagicMock()
    context.state = MagicMock()
    context.state.questions_db = questions_db if questions_db is not None else {}
    context.state.round_sequence = MagicMock()
    context.state.round_sequence.last_round_transition_timestamp.timestamp.return_value = (
        1_700_000_000.0
    )
    context.params = MagicMock()
    context.params.default_chain_id = "gnosis"
    context.params.mech_tool_resolve_market = "resolve-market-jury-v1"
    context.params.initial_answer_bond = 10**18
    context.params.max_challenge_bond = 16 * 10**18
    context.params.challenge_cooldown_fraction = 0.25
    context.params.challenge_urgency_buffer = 3600
    context.params.max_mech_retries = 10
    context.params.mech_retry_cooldown = 3600
    context.params.omen_subgraph_max_market_age_seconds = 365 * 86400
    context.params.trusted_addresses = []
    context.params.watched_creator_addresses = ["0xCreator"]
    context.params.realitio_contract = "0x" + "11" * 20
    context.params.multisend_address = "0x" + "22" * 20
    context.omen_subgraph = MagicMock()
    context.omen_subgraph.get_spec.return_value = {
        "method": "POST",
        "url": "https://api.thegraph.com/omen",
        "headers": {},
    }
    context.mech_gnosis_subgraph = MagicMock()
    context.mech_gnosis_subgraph.get_spec.return_value = {
        "method": "POST",
        "url": "https://api.thegraph.com/mech-gnosis",
        "headers": {},
    }
    context.realitio_subgraph = MagicMock()
    context.realitio_subgraph.get_spec.return_value = {
        "method": "POST",
        "url": "https://api.thegraph.com/realitio",
        "headers": {},
    }
    context.benchmark_tool = MagicMock()
    return context


def _make_synced_data(
    safe_address: str = SAFE_ADDRESS,
    selected_market_id: Any = None,
    selected_market_action: Any = None,
    tx_submitter: Any = None,
    mech_responses: Any = None,
) -> MagicMock:
    """Return a mocked SynchronizedData."""
    sd = MagicMock()
    sd.safe_contract_address = safe_address
    sd.selected_market_id = selected_market_id
    sd.selected_market_action = selected_market_action
    sd.tx_submitter = tx_submitter
    sd.final_tx_hash = "0xFinalisedHash"
    sd.mech_responses = mech_responses or []
    sd.mech_requests = []
    return sd


def _exhaust_gen(gen: Any) -> Any:
    """Drive a generator to completion, returning the final value."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def _make_gen(return_value: Any) -> Any:
    """Return a generator factory that yields nothing and returns ``return_value``."""

    def gen(*args: Any, **kwargs: Any) -> Any:
        return return_value
        yield  # noqa: unreachable -- makes this a generator function

    return gen


def _make_behaviour(
    behaviour_cls: Any,
    questions_db: Any = None,
    synced_data: Any = None,
) -> Any:
    """Instantiate ``behaviour_cls`` with mocked context + synchronized_data patched."""
    context = _make_context(questions_db)
    behaviour = behaviour_cls(name=behaviour_cls.__name__, skill_context=context)
    sd = synced_data if synced_data is not None else _make_synced_data()
    patcher = patch.object(
        type(behaviour),
        "synchronized_data",
        new_callable=PropertyMock,
        return_value=sd,
    )
    patcher.start()
    behaviour._sd_patcher = patcher  # keep reference; stop in teardown
    return behaviour


@contextmanager
def behaviour_ctx(
    behaviour_cls: Any,
    questions_db: Any = None,
    synced_data: Any = None,
) -> Iterator[Any]:
    """Context manager variant of ``_make_behaviour`` that cleans up the patch."""
    behaviour = _make_behaviour(behaviour_cls, questions_db, synced_data)
    try:
        yield behaviour
    finally:
        behaviour._sd_patcher.stop()


@pytest.fixture
def mock_context() -> MagicMock:
    """Fixture: mocked skill context with an empty questions_db."""
    return _make_context()


@pytest.fixture
def mock_synced_data() -> MagicMock:
    """Fixture: default mocked SynchronizedData."""
    return _make_synced_data()


@pytest.fixture(autouse=True)
def _stop_leaked_patchers() -> Iterator[None]:
    """Stop any patch.object() patchers left running by ``_make_behaviour``.

    ``_make_behaviour`` starts a ``PropertyMock`` patch on the behaviour class
    and never stops it, so the patched class attribute leaks across tests. This
    autouse fixture stops every active patch after each test to keep tests
    isolated.

    :yield: control back to the test before tearing down patches.
    """
    yield
    patch.stopall()
