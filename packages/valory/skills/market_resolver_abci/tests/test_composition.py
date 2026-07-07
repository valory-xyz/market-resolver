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

import json
from pathlib import Path
from typing import Any, Dict

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


_AGENT_CONFIG_YAML = (
    Path(packages.valory.skills.market_resolver_abci.__file__).parent.parent.parent
    / "agents"
    / "market_resolver"
    / "aea-config.yaml"
)
_SERVICE_YAML = (
    Path(packages.valory.skills.market_resolver_abci.__file__).parent.parent.parent
    / "services"
    / "market_resolver"
    / "service.yaml"
)


def _extract_marketplace_dict(dumped: str) -> Dict[str, Any]:
    """Extract the mech_marketplace_config dict from an env-var default string.

    Both ``aea-config.yaml`` and ``service.yaml`` render this field as a
    ``${MECH_MARKETPLACE_CONFIG:dict:{...}}`` env-var default. Peel the
    outer wrapping and parse the inner JSON to get the same shape as the
    ``mech_marketplace_config`` mapping the skill.yaml declares natively.
    """
    marker = ":dict:"
    idx = dumped.find(marker)
    assert idx != -1, f"mech_marketplace_config default missing dict marker: {dumped}"
    inner = dumped[idx + len(marker) :].rstrip("}").rstrip()
    if not inner.endswith("}"):
        inner = inner + "}"
    return json.loads(inner)


class TestMechMarketplaceConfigParity:
    """The offchain config dict lives in 3 yaml layers; assert they agree.

    ``MECH_MARKETPLACE_CONFIG`` is a dict-typed env var, which the AEA
    framework replaces atomically -- setting one field in ops replaces the
    whole dict. If the defaults across skill.yaml, aea-config.yaml and
    service.yaml drift, operators who don't override every key silently
    lose fields (e.g. ``use_offchain`` disappears and downstream Python
    picks up whichever default the framework happens to fall through to).
    Pin the seven offchain fields to identical values across all three
    layers. Missing this coverage was jmoreira's MEDIUM finding on PR #37.
    """

    _OFFCHAIN_KEYS = (
        "use_offchain",
        "offchain_url",
        "offchain_deposit_target_calls",
        "auto_deposit_cap_per_cycle",
        "offchain_poll_interval_seconds",
        "offchain_poll_timeout_seconds",
        "offchain_failover_max_retries",
    )

    def _skill_yaml_mmc(self) -> Dict[str, Any]:
        data = _load_composed_skill_yaml()
        return data["models"]["params"]["args"]["mech_marketplace_config"]

    def _config_yaml_mmc(self) -> Dict[str, Any]:
        """The agent yaml is multi-doc; find the skill-scoped override."""
        docs = list(yaml.safe_load_all(_AGENT_CONFIG_YAML.read_text()))
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if str(doc.get("public_id", "")).startswith("valory/market_resolver_abci"):
                return _extract_marketplace_dict(
                    doc["models"]["params"]["args"]["mech_marketplace_config"]
                )
        raise AssertionError("market_resolver_abci block missing from aea-config.yaml")

    def _service_yaml_mmc(self) -> Dict[str, Any]:
        """The service yaml is multi-doc; find the skill-scoped override."""
        docs = list(yaml.safe_load_all(_SERVICE_YAML.read_text()))
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if str(doc.get("public_id", "")).startswith("valory/market_resolver_abci"):
                return _extract_marketplace_dict(
                    doc["models"]["params"]["args"]["mech_marketplace_config"]
                )
        raise AssertionError("market_resolver_abci block missing from service.yaml")

    def test_offchain_defaults_agree_across_three_yaml_layers(self) -> None:
        """All 7 offchain keys must be identical in the 3 yaml layers."""
        skill = self._skill_yaml_mmc()
        agent = self._config_yaml_mmc()
        service = self._service_yaml_mmc()

        for key in self._OFFCHAIN_KEYS:
            assert key in skill, f"skill.yaml missing offchain key {key!r}"
            assert key in agent, f"aea-config.yaml missing offchain key {key!r}"
            assert key in service, f"service.yaml missing offchain key {key!r}"
            assert skill[key] == agent[key] == service[key], (
                f"mech_marketplace_config.{key} drift: "
                f"skill={skill[key]!r} agent={agent[key]!r} "
                f"service={service[key]!r}"
            )

    def test_use_offchain_defaults_false(self) -> None:
        """Ships-dark invariant: default must be ``false`` on all 3 layers."""
        skill = self._skill_yaml_mmc()
        agent = self._config_yaml_mmc()
        service = self._service_yaml_mmc()
        for name, dumped in (("skill", skill), ("agent", agent), ("service", service)):
            assert dumped["use_offchain"] is False, (
                f"{name}.yaml default use_offchain must be false -- flipping "
                "it on requires operator opt-in per PR #37's description."
            )
