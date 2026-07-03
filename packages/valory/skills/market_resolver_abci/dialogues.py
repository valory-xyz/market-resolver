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

"""This module contains the classes required for dialogue management."""

from packages.valory.skills.abstract_round_abci.dialogues import (
    AbciDialogue as BaseAbciDialogue,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    AbciDialogues as BaseAbciDialogues,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    ContractApiDialogue as BaseContractApiDialogue,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    ContractApiDialogues as BaseContractApiDialogues,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    HttpDialogue as BaseHttpDialogue,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    HttpDialogues as BaseHttpDialogues,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    IpfsDialogue as BaseIpfsDialogue,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    IpfsDialogues as BaseIpfsDialogues,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    LedgerApiDialogue as BaseLedgerApiDialogue,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    LedgerApiDialogues as BaseLedgerApiDialogues,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    SigningDialogue as BaseSigningDialogue,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    SigningDialogues as BaseSigningDialogues,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    TendermintDialogue as BaseTendermintDialogue,
)
from packages.valory.skills.abstract_round_abci.dialogues import (
    TendermintDialogues as BaseTendermintDialogues,
)
from packages.valory.skills.market_resolution_manager_abci.dialogues import (
    KvStoreDialogues as _InnerKvStoreDialogues,
)

AbciDialogue = BaseAbciDialogue
AbciDialogues = BaseAbciDialogues

HttpDialogue = BaseHttpDialogue
HttpDialogues = BaseHttpDialogues

SigningDialogue = BaseSigningDialogue
SigningDialogues = BaseSigningDialogues

LedgerApiDialogue = BaseLedgerApiDialogue
LedgerApiDialogues = BaseLedgerApiDialogues

ContractApiDialogue = BaseContractApiDialogue
ContractApiDialogues = BaseContractApiDialogues

TendermintDialogue = BaseTendermintDialogue
TendermintDialogues = BaseTendermintDialogues

IpfsDialogue = BaseIpfsDialogue
IpfsDialogues = BaseIpfsDialogues

# Composed skill re-export: the inner sub-skill's KvStoreDialogues class must
# be reachable on the composed context so behaviours (which run bound to the
# composed skill at runtime) can register kv_store messages. Without this,
# ``self.context.kv_store_dialogues`` AttributeErrors on the first cache
# hit -- and the mocked-context unit tests do not catch it, since
# ``conftest.py`` uses ``MagicMock()`` which silently returns a mock.
KvStoreDialogues = _InnerKvStoreDialogues
