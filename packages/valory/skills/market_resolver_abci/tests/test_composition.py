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

"""Tests for the composition module of the composed skill."""

# pylint: disable=unused-import

from pathlib import Path
from typing import Any

import yaml

import packages.valory.skills.market_resolver_abci
import packages.valory.skills.market_resolver_abci.composition  # noqa: F401


def test_import() -> None:
    """Test that the composition module can be imported."""


_COMPOSED_SKILL_YAML = (
    Path(packages.valory.skills.market_resolver_abci.__file__).parent / "skill.yaml"
)


def _load_composed_skill_yaml() -> Any:
    """Read the composed skill's yaml so we can assert on its declarations."""
    with open(_COMPOSED_SKILL_YAML) as fp:
        return yaml.safe_load(fp)


class TestKvStoreComposedWiring:
    """Regression guards for the composition of ``market_resolver_abci``.

    Behaviours defined in ``market_resolution_manager_abci`` execute bound
    to the composed skill's ``SkillContext`` at runtime. Anything they read
    off ``self.context`` (dialogues, handlers, connections) must exist on
    the composed skill, otherwise the first live cycle AttributeErrors.
    The mocked-context unit tests do not catch this because ``conftest``
    uses ``MagicMock()`` -- every ``self.context.*`` returns a mock instead
    of raising -- so a composition wiring omission slips through CI.
    """

    def test_kv_store_connection_declared(self) -> None:
        """The kv_store connection must be reachable from the composed skill."""
        data = _load_composed_skill_yaml()
        connections = data.get("connections", []) or []
        assert any(
            "valory/kv_store" in entry for entry in connections
        ), "kv_store connection missing from composed skill.yaml"

    def test_kv_store_protocol_declared(self) -> None:
        """The kv_store protocol must be reachable from the composed skill."""
        data = _load_composed_skill_yaml()
        protocols = data.get("protocols", []) or []
        assert any(
            "valory/kv_store" in entry for entry in protocols
        ), "kv_store protocol missing from composed skill.yaml"

    def test_kv_store_handler_declared(self) -> None:
        """The kv_store response handler must be registered."""
        data = _load_composed_skill_yaml()
        handlers = data.get("handlers", {}) or {}
        assert "kv_store" in handlers, (
            "kv_store handler missing from composed skill.yaml "
            "-- the KvStore SUCCESS/ERROR callbacks won't dispatch."
        )
        assert handlers["kv_store"].get("class_name") == "KvStoreHandler"

    def test_kv_store_dialogues_declared(self) -> None:
        """The kv_store dialogues model must be registered.

        This is the exact failure mode that surfaced in the local e2e
        run: ``self.context.kv_store_dialogues`` AttributeError on the
        first ``ScanMarketsBehaviour`` tick, because the composed skill
        never declared this model.
        """
        data = _load_composed_skill_yaml()
        models = data.get("models", {}) or {}
        assert "kv_store_dialogues" in models, (
            "kv_store_dialogues missing from composed skill.yaml "
            "-- self.context.kv_store_dialogues will AttributeError."
        )
        assert models["kv_store_dialogues"].get("class_name") == "KvStoreDialogues"

    def test_composed_handlers_module_reexports_kv_store(self) -> None:
        """``handlers.py`` must expose ``KvStoreHandler`` at module scope."""
        from packages.valory.skills.market_resolver_abci import handlers as _handlers

        assert hasattr(_handlers, "KvStoreHandler"), (
            "market_resolver_abci.handlers must re-export KvStoreHandler "
            "so the skill.yaml handler entry can resolve the class."
        )

    def test_composed_dialogues_module_reexports_kv_store(self) -> None:
        """``dialogues.py`` must expose ``KvStoreDialogues`` at module scope."""
        from packages.valory.skills.market_resolver_abci import dialogues as _dialogues

        assert hasattr(_dialogues, "KvStoreDialogues"), (
            "market_resolver_abci.dialogues must re-export KvStoreDialogues "
            "so the skill.yaml model entry can resolve the class."
        )
