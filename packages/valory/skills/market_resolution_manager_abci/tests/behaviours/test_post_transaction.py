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

"""Tests for behaviours/post_transaction.py."""

# pylint: disable=protected-access

from typing import Any, Optional
from unittest.mock import PropertyMock, patch

import packages.valory.skills.mech_interact_abci.states.request as MechRequestStates
from packages.valory.skills.funds_forwarder_abci.rounds import FundsForwarderRound
from packages.valory.skills.market_resolution_manager_abci.behaviours.post_transaction import (
    PostTransactionBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    BuildAnswerTxRound,
    PostTransactionRound,
)
from packages.valory.skills.omen_ct_redeem_tokens_abci.rounds import CtRedeemTokensRound
from packages.valory.skills.omen_fpmm_remove_liquidity_abci.rounds import (
    FpmmRemoveLiquidityRound,
)
from packages.valory.skills.omen_realitio_withdraw_bonds_abci.rounds import (
    RealitioWithdrawBondsRound,
)

from .conftest import _make_context, _make_gen, _make_synced_data


def _make_behaviour(tx_submitter: Optional[str] = None) -> PostTransactionBehaviour:
    """Instantiate PostTransactionBehaviour with mocked context + synchronized_data patched."""
    context = _make_context()
    behaviour = PostTransactionBehaviour(name="post_tx", skill_context=context)
    sd = _make_synced_data(tx_submitter=tx_submitter)
    patcher = patch.object(
        type(behaviour),
        "synchronized_data",
        new_callable=PropertyMock,
        return_value=sd,
    )
    patcher.start()
    behaviour._sd_patcher = patcher
    return behaviour


def _run_async_act(behaviour: PostTransactionBehaviour) -> str:
    """Drive async_act to completion and return the payload content sent."""
    payload_sent = []

    def capture_send(payload: Any) -> Any:
        payload_sent.append(payload)
        return None
        yield  # noqa

    with (
        patch.object(behaviour, "send_a2a_transaction", new=capture_send),
        patch.object(behaviour, "wait_until_round_end", new=_make_gen(None)),
        patch.object(behaviour, "set_done"),
    ):
        gen = behaviour.async_act()
        try:
            while True:
                next(gen)
        except StopIteration:
            pass

    return payload_sent[0].content


class TestPostTransactionBehaviour:
    """Tests for PostTransactionBehaviour.async_act routing."""

    def test_mech_request_submitter(self) -> None:
        """Mech request round submitter routes to MECH_REQUEST_DONE_PAYLOAD."""
        content = _run_async_act(
            _make_behaviour(
                tx_submitter=MechRequestStates.MechRequestRound.auto_round_id()
            )
        )
        assert content == PostTransactionRound.MECH_REQUEST_DONE_PAYLOAD

    def test_build_answer_tx_submitter(self) -> None:
        """Build-answer-tx submitter routes to ANSWER_TX_DONE_PAYLOAD."""
        content = _run_async_act(
            _make_behaviour(tx_submitter=BuildAnswerTxRound.auto_round_id())
        )
        assert content == PostTransactionRound.ANSWER_TX_DONE_PAYLOAD

    def test_funds_forwarder_submitter(self) -> None:
        """Funds-forwarder submitter routes to FUNDS_FORWARDER_TX_DONE_PAYLOAD."""
        content = _run_async_act(
            _make_behaviour(tx_submitter=FundsForwarderRound.auto_round_id())
        )
        assert content == PostTransactionRound.FUNDS_FORWARDER_TX_DONE_PAYLOAD

    def test_fpmm_remove_liquidity_submitter(self) -> None:
        """FPMM remove-liquidity submitter routes to FPMM_REMOVE_LIQUIDITY_TX_DONE_PAYLOAD."""
        content = _run_async_act(
            _make_behaviour(tx_submitter=FpmmRemoveLiquidityRound.auto_round_id())
        )
        assert content == PostTransactionRound.FPMM_REMOVE_LIQUIDITY_TX_DONE_PAYLOAD

    def test_ct_redeem_tokens_submitter(self) -> None:
        """CT redeem-tokens submitter routes to CT_REDEEM_TOKENS_TX_DONE_PAYLOAD."""
        content = _run_async_act(
            _make_behaviour(tx_submitter=CtRedeemTokensRound.auto_round_id())
        )
        assert content == PostTransactionRound.CT_REDEEM_TOKENS_TX_DONE_PAYLOAD

    def test_realitio_withdraw_bonds_submitter(self) -> None:
        """Realitio withdraw-bonds submitter routes to REALITIO_WITHDRAW_BONDS_TX_DONE_PAYLOAD."""
        content = _run_async_act(
            _make_behaviour(tx_submitter=RealitioWithdrawBondsRound.auto_round_id())
        )
        assert content == PostTransactionRound.REALITIO_WITHDRAW_BONDS_TX_DONE_PAYLOAD

    def test_unknown_submitter_falls_back_to_answer_tx_done(self) -> None:
        """Unknown tx_submitter falls back to ANSWER_TX_DONE_PAYLOAD."""
        content = _run_async_act(_make_behaviour(tx_submitter="unknown_round_xyz"))
        assert content == PostTransactionRound.ANSWER_TX_DONE_PAYLOAD

    def test_none_submitter_falls_back_to_answer_tx_done(self) -> None:
        """None tx_submitter falls back to ANSWER_TX_DONE_PAYLOAD."""
        content = _run_async_act(_make_behaviour(tx_submitter=None))
        assert content == PostTransactionRound.ANSWER_TX_DONE_PAYLOAD
