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

"""This module contains the payloads for the market resolution manager."""

from dataclasses import dataclass
from typing import Optional

from packages.valory.skills.abstract_round_abci.base import BaseTxPayload


@dataclass(frozen=True)
class ScanPendingMarketsPayload(BaseTxPayload):
    """Payload for the ScanPendingMarketsRound."""

    n_markets: Optional[int] = None
    selected_market_id: Optional[str] = None
    selected_market_action: Optional[str] = None


@dataclass(frozen=True)
class EvaluateAnswersPayload(BaseTxPayload):
    """Payload for the EvaluateAnswersRound.

    Two mutually exclusive fields:
    - mech_requests: JSON-serialized list of MechMetadata dicts (needs Mech)
    - evaluation_result: status string (has existing data, skip Mech)
    """

    mech_requests: Optional[str] = None
    evaluation_result: Optional[str] = None


@dataclass(frozen=True)
class BuildChallengesTxPayload(BaseTxPayload):
    """Payload for the BuildChallengesTxRound."""

    challenge_data: Optional[str] = None


@dataclass(frozen=True)
class CleanupTrackedMarketsPayload(BaseTxPayload):
    """Payload for the CleanupTrackedMarketsRound."""

    n_cleaned: Optional[int] = None
