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
import packages.valory.skills.market_resolution_manager_abci.rounds as MarketResolutionManagerAbci
import packages.valory.skills.mech_interact_abci.rounds as MechInteractAbci
import packages.valory.skills.mech_interact_abci.states.final_states as MechFinalStates
import packages.valory.skills.mech_interact_abci.states.mech_version as MechVersionStates
import packages.valory.skills.mech_interact_abci.states.request as MechRequestStates
import packages.valory.skills.mech_interact_abci.states.response as MechResponseStates
import packages.valory.skills.omen_ct_redeem_tokens_abci.rounds as OmenCtRedeemTokensAbci
import packages.valory.skills.omen_fpmm_remove_liquidity_abci.rounds as OmenFpmmRemoveLiquidityAbci
import packages.valory.skills.omen_realitio_withdraw_bonds_abci.rounds as OmenRealitioWithdrawBondAbci
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
    # Registration -> IdentifyServiceOwner
    FinishedRegistrationRound: IdentifyServiceOwnerAbci.IdentifyServiceOwnerRound,
    # IdentifyServiceOwner -> FundsForwarder (ok) / FpmmRemoveLiquidity (error -- skip FundsForwarder)
    IdentifyServiceOwnerAbci.FinishedIdentifyServiceOwnerRound: FundsForwarderAbci.FundsForwarderRound,
    IdentifyServiceOwnerAbci.FinishedIdentifyServiceOwnerErrorRound: OmenFpmmRemoveLiquidityAbci.FpmmRemoveLiquidityRound,
    # FundsForwarder: tx -> TxSettlement (returns via PostTx -> FpmmRemoveLiquidity).
    # No tx -> FpmmRemoveLiquidity directly.
    FundsForwarderAbci.FinishedFundsForwarderNoTxRound: OmenFpmmRemoveLiquidityAbci.FpmmRemoveLiquidityRound,
    FundsForwarderAbci.FinishedFundsForwarderWithTxRound: TransactionSettlementAbci.RandomnessTransactionSubmissionRound,
    # Linear recovery chain: each skill either builds a multisend (-> TxSettlement ->
    # PostTx -> next skill) or produces no tx (-> next skill directly). Every cycle walks
    # through all three skills in order before reaching the core resolution flow.
    #
    # Step 1: FpmmRemoveLiquidity
    OmenFpmmRemoveLiquidityAbci.FinishedWithFpmmRemoveLiquidityTxRound: TransactionSettlementAbci.RandomnessTransactionSubmissionRound,
    OmenFpmmRemoveLiquidityAbci.FinishedWithoutFpmmRemoveLiquidityTxRound: OmenCtRedeemTokensAbci.CtRedeemTokensRound,
    # Step 2: CtRedeemTokens
    OmenCtRedeemTokensAbci.FinishedWithCtRedeemTokensTxRound: TransactionSettlementAbci.RandomnessTransactionSubmissionRound,
    OmenCtRedeemTokensAbci.FinishedWithoutCtRedeemTokensTxRound: OmenRealitioWithdrawBondAbci.RealitioWithdrawBondsRound,
    # Step 3: RealitioWithdrawBond
    OmenRealitioWithdrawBondAbci.FinishedWithRealitioWithdrawBondsTxRound: TransactionSettlementAbci.RandomnessTransactionSubmissionRound,
    OmenRealitioWithdrawBondAbci.FinishedWithoutRealitioWithdrawBondsTxRound: MarketResolutionManagerAbci.ScanMarketsRound,
    # PostTx fan-out: each recovery tx returns to the NEXT step of the chain.
    MarketResolutionManagerAbci.FinishedWithFundsForwarderPostTxRound: OmenFpmmRemoveLiquidityAbci.FpmmRemoveLiquidityRound,
    MarketResolutionManagerAbci.FinishedWithFpmmRemoveLiquidityPostTxRound: OmenCtRedeemTokensAbci.CtRedeemTokensRound,
    MarketResolutionManagerAbci.FinishedWithCtRedeemTokensPostTxRound: OmenRealitioWithdrawBondAbci.RealitioWithdrawBondsRound,
    MarketResolutionManagerAbci.FinishedWithRealitioWithdrawBondsPostTxRound: MarketResolutionManagerAbci.ScanMarketsRound,
    # Core resolution flow (unchanged) ------------------------------------------
    MarketResolutionManagerAbci.FinishedWithMechRequestRound: MechVersionStates.MechVersionDetectionRound,
    MechFinalStates.FinishedMarketplaceLegacyDetectedRound: MechRequestStates.MechRequestRound,
    MechFinalStates.FinishedMechLegacyDetectedRound: MechRequestStates.MechRequestRound,
    MechFinalStates.FinishedMechInformationRound: MechRequestStates.MechRequestRound,
    MechFinalStates.FailedMechInformationRound: MechVersionStates.MechVersionDetectionRound,
    MechFinalStates.FinishedMechRequestRound: TransactionSettlementAbci.RandomnessTransactionSubmissionRound,
    MechFinalStates.FinishedMechPurchaseSubscriptionRound: TransactionSettlementAbci.RandomnessTransactionSubmissionRound,
    MechFinalStates.FinishedMechResponseRound: MarketResolutionManagerAbci.BuildAnswerTxRound,
    MechFinalStates.FinishedMechRequestSkipRound: ResetAndPauseRound,
    MechFinalStates.FinishedMechResponseTimeoutRound: ResetAndPauseRound,
    MarketResolutionManagerAbci.FinishedWithAnswerTxRound: TransactionSettlementAbci.RandomnessTransactionSubmissionRound,
    # TxSettlement -> PostTransactionRound (multiplexes by tx_submitter)
    TransactionSettlementAbci.FinishedTransactionSubmissionRound: MarketResolutionManagerAbci.PostTransactionRound,
    TransactionSettlementAbci.FailedRound: ResetAndPauseRound,
    MarketResolutionManagerAbci.FinishedWithMechPollRound: MechResponseStates.MechResponseRound,
    MarketResolutionManagerAbci.FinishedResolutionRound: ResetAndPauseRound,
    # Reset -> next period
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
        OmenFpmmRemoveLiquidityAbci.OmenFpmmRemoveLiquidityAbciApp,
        OmenCtRedeemTokensAbci.OmenCtRedeemTokensAbciApp,
        OmenRealitioWithdrawBondAbci.OmenRealitioWithdrawBondsAbciApp,
        MarketResolutionManagerAbci.MarketResolutionManagerAbciApp,
        TransactionSettlementAbci.TransactionSubmissionAbciApp,
        MechInteractAbci.MechInteractAbciApp,
        ResetPauseAbciApp,
    ),
    abci_app_transition_mapping,
).add_background_app(termination_config)
