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

"""This module contains the market resolver composed ABCI application."""

import packages.valory.skills.funds_forwarder_abci.rounds as FundsForwarderAbci
import packages.valory.skills.identify_service_owner_abci.rounds as IdentifyServiceOwnerAbci
import packages.valory.skills.omen_funds_recoverer_abci.rounds as OmenFundsRecovererAbci
import packages.valory.skills.transaction_settlement_abci.rounds as TransactionSettlementAbci
from packages.valory.skills.abstract_round_abci.abci_app_chain import (
    AbciAppTransitionMapping,
    chain,
)
from packages.valory.skills.abstract_round_abci.base import BackgroundAppConfig
from packages.valory.skills.registration_abci.rounds import (
    AgentRegistrationAbciApp,
    FinishedRegistrationRound,
    RegistrationRound,
)
from packages.valory.skills.reset_pause_abci.rounds import (
    FinishedResetAndPauseErrorRound,
    FinishedResetAndPauseRound,
    ResetAndPauseRound,
    ResetPauseAbciApp,
)
from packages.valory.skills.termination_abci.rounds import (
    BackgroundRound,
    Event,
    TerminationAbciApp,
)

abci_app_transition_mapping: AbciAppTransitionMapping = {
    FinishedRegistrationRound: IdentifyServiceOwnerAbci.IdentifyServiceOwnerRound,
    IdentifyServiceOwnerAbci.FinishedIdentifyServiceOwnerRound: FundsForwarderAbci.FundsForwarderRound,
    IdentifyServiceOwnerAbci.FinishedIdentifyServiceOwnerErrorRound: OmenFundsRecovererAbci.RemoveLiquidityRound,
    FundsForwarderAbci.FinishedFundsForwarderNoTxRound: OmenFundsRecovererAbci.RemoveLiquidityRound,
    FundsForwarderAbci.FinishedFundsForwarderWithTxRound: TransactionSettlementAbci.RandomnessTransactionSubmissionRound,
    OmenFundsRecovererAbci.FinishedWithRecoveryTxRound: TransactionSettlementAbci.RandomnessTransactionSubmissionRound,
    OmenFundsRecovererAbci.FinishedWithoutRecoveryTxRound: ResetAndPauseRound,
    TransactionSettlementAbci.FinishedTransactionSubmissionRound: ResetAndPauseRound,
    TransactionSettlementAbci.FailedRound: ResetAndPauseRound,
    FinishedResetAndPauseRound: IdentifyServiceOwnerAbci.IdentifyServiceOwnerRound,
    FinishedResetAndPauseErrorRound: RegistrationRound,
}

termination_config = BackgroundAppConfig(
    round_cls=BackgroundRound,
    start_event=Event.TERMINATE,
    abci_app=TerminationAbciApp,
)

MarketResolverAbciApp = chain(
    (
        AgentRegistrationAbciApp,
        IdentifyServiceOwnerAbci.IdentifyServiceOwnerAbciApp,
        FundsForwarderAbci.FundsForwarderAbciApp,
        OmenFundsRecovererAbci.OmenFundsRecovererAbciApp,
        TransactionSettlementAbci.TransactionSubmissionAbciApp,
        ResetPauseAbciApp,
    ),
    abci_app_transition_mapping,
).add_background_app(termination_config)
