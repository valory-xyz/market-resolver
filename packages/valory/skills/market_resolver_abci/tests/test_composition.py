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
    lose fields (e.g. a poll timeout disappears and downstream Python
    picks up whichever default the framework happens to fall through to).
    Pin the offchain fields still inside the dict to identical values
    across all three layers. Missing this coverage was jmoreira's MEDIUM
    finding on PR #37.

    ``use_offchain`` and ``offchain_deposit_target_calls`` moved out of
    the marketplace-config dict in mech-interact v0.32.7 and are now
    top-level ``MechParams`` args; ``TestTopLevelMechParamsParity``
    below covers their cross-layer parity separately.
    """

    _OFFCHAIN_KEYS = (
        "offchain_url",
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


class TestTopLevelMechParamsParity:
    """``use_offchain`` and ``offchain_deposit_target_calls`` are top-level.

    Upstream ``mech-interact`` v0.32.7 (PR #113) hoisted these two out of
    the ``MechMarketplaceConfig`` dict into ``MechParams`` itself, with a
    fail-loud migration guard: leaving either key nested in the composed
    ``mech_marketplace_config`` mapping raises at ``MechParams.__init__``.
    Assert that every layer declares them at the top level of the
    ``params.args`` block, with the ships-dark ``use_offchain=false``
    default preserved across all three files.
    """

    _TOP_LEVEL_KEYS = ("use_offchain", "offchain_deposit_target_calls")

    def _skill_yaml_args(self) -> Dict[str, Any]:
        data = _load_composed_skill_yaml()
        return data["models"]["params"]["args"]

    def _multi_doc_args(self, yaml_path: Path) -> Dict[str, Any]:
        """Extract the ``params.args`` block from a multi-doc yaml file.

        Both ``aea-config.yaml`` and ``service.yaml`` are multi-doc; the
        skill block lives under ``public_id: valory/market_resolver_abci``.
        The top-level env-var wrappers (``${USE_OFFCHAIN:bool:false}`` etc)
        parse to strings under ``yaml.safe_load``, which is the shape we
        want to introspect here -- see ``_parse_env_default`` below.
        """
        docs = list(yaml.safe_load_all(yaml_path.read_text()))
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if str(doc.get("public_id", "")).startswith("valory/market_resolver_abci"):
                return doc["models"]["params"]["args"]  # type: ignore[no-any-return]
        raise AssertionError(
            f"market_resolver_abci block missing from {yaml_path.name}"
        )

    @staticmethod
    def _parse_env_default(value: Any) -> Any:
        """Peel a ``${VAR:type:default}`` env-var wrapper down to its default.

        Plain scalars pass through untouched so this helper works uniformly
        against ``skill.yaml`` (plain values) and ``aea-config.yaml`` /
        ``service.yaml`` (env-var-wrapped values). The parser is intentionally
        small; it only handles ``bool`` and ``int`` because those are the
        two hoisted keys.
        """
        if not isinstance(value, str) or not value.startswith("${"):
            return value
        # ``${NAME:type:default}`` -- default is everything after the second colon
        inner = value.removeprefix("${").rstrip("}")
        parts = inner.split(":", 2)
        assert len(parts) == 3, f"malformed env-var wrapper: {value!r}"
        _, kind, default = parts
        if kind == "bool":
            return {"true": True, "false": False}[default.lower()]
        if kind == "int":
            return int(default)
        raise AssertionError(f"unsupported env-var type in this test: {kind!r}")

    def test_top_level_keys_present_on_all_three_layers(self) -> None:
        """The two hoisted keys must exist at ``params.args`` on every layer."""
        skill = self._skill_yaml_args()
        agent = self._multi_doc_args(_AGENT_CONFIG_YAML)
        service = self._multi_doc_args(_SERVICE_YAML)

        for key in self._TOP_LEVEL_KEYS:
            assert key in skill, f"skill.yaml missing top-level {key!r}"
            assert key in agent, f"aea-config.yaml missing top-level {key!r}"
            assert key in service, f"service.yaml missing top-level {key!r}"

    def test_top_level_defaults_agree_across_layers(self) -> None:
        """The three yaml layers must agree on the default values."""
        skill = self._skill_yaml_args()
        agent = self._multi_doc_args(_AGENT_CONFIG_YAML)
        service = self._multi_doc_args(_SERVICE_YAML)

        for key in self._TOP_LEVEL_KEYS:
            s = self._parse_env_default(skill[key])
            a = self._parse_env_default(agent[key])
            v = self._parse_env_default(service[key])
            assert s == a == v, (
                f"top-level {key} drift: " f"skill={s!r} agent={a!r} service={v!r}"
            )

    def test_use_offchain_defaults_false(self) -> None:
        """Ships-dark invariant: default must be ``false`` on all 3 layers."""
        skill = self._skill_yaml_args()
        agent = self._multi_doc_args(_AGENT_CONFIG_YAML)
        service = self._multi_doc_args(_SERVICE_YAML)
        for name, args in (("skill", skill), ("agent", agent), ("service", service)):
            assert self._parse_env_default(args["use_offchain"]) is False, (
                f"{name}.yaml default use_offchain must be false -- flipping "
                "it on requires operator opt-in per PR #37's description."
            )

    def test_marketplace_dict_no_longer_carries_hoisted_keys(self) -> None:
        """After v0.32.7, leaving either key in the dict trips the guard.

        Guard against operators (or future refactors) re-nesting these
        keys inside the ``mech_marketplace_config`` dict, which would boot
        the agent into the fail-loud ``MechParams.__init__`` migration
        error introduced by mech-interact PR #113.
        """
        parity = TestMechMarketplaceConfigParity()
        for name, dumped in (
            ("skill", parity._skill_yaml_mmc()),
            ("agent", parity._config_yaml_mmc()),
            ("service", parity._service_yaml_mmc()),
        ):
            for key in self._TOP_LEVEL_KEYS:
                assert key not in dumped, (
                    f"{name}.yaml still has {key!r} inside mech_marketplace_config; "
                    "mech-interact v0.32.7 hoisted this to a top-level MechParams "
                    "arg and MechParams.__init__ raises ValueError if it stays nested."
                )
