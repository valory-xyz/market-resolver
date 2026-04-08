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

"""This module contains the base state definitions for the market resolution manager."""

from enum import Enum


class Event(Enum):
    """MarketResolutionManagerAbciApp Events"""

    NO_MAJORITY = "no_majority"
    DONE = "done"
    NONE = "none"
    ROUND_TIMEOUT = "round_timeout"
    MECH_REQUEST_DONE = "mech_request_done"
    ANSWER_TX_DONE = "answer_tx_done"
    FUNDS_FORWARDER_TX_DONE = "funds_forwarder_tx_done"
    FPMM_REMOVE_LIQUIDITY_TX_DONE = "fpmm_remove_liquidity_tx_done"
    CT_REDEEM_TOKENS_TX_DONE = "ct_redeem_tokens_tx_done"
    REALITIO_WITHDRAW_BONDS_TX_DONE = "realitio_withdraw_bonds_tx_done"


class AnswerStatus(str, Enum):
    """Status of a tracked market answer in the questions database."""

    NEEDS_ANSWER = "NEEDS_ANSWER"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    TRUSTED_ANSWER = "TRUSTED_ANSWER"
    VERIFIED = "VERIFIED"
    CHALLENGE_PENDING = "CHALLENGE_PENDING"
