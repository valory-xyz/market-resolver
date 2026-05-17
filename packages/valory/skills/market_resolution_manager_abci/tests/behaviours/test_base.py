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

"""Tests for behaviours/base.py."""

# pylint: disable=protected-access,use-implicit-booleaness-not-comparison
# pylint: disable=unsubscriptable-object,unsupported-membership-test

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock, PropertyMock, patch

from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    ANSWER_INVALID,
    ANSWER_NO,
    ANSWER_YES,
    MECH_CACHE_QUERY_TEMPLATE,
    is_cached_evaluation_valid,
    jury_error_discriminator,
    parse_mech_response,
    to_content,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.scan_markets import (
    ScanMarketsBehaviour,
)

from .conftest import (
    SAFE_ADDRESS,
    _exhaust_gen,
    _make_context,
    _make_gen,
    _make_synced_data,
)

# ---------------------------------------------------------------------------
# parse_mech_response -- pure function tests
# ---------------------------------------------------------------------------


class TestParseMechResponse:
    """Tests for parse_mech_response strict pattern matching."""

    def test_none_input_returns_none(self) -> None:
        """None input returns None."""
        assert parse_mech_response(None) is None

    def test_invalid_json_returns_none(self) -> None:
        """Non-JSON string returns None."""
        assert parse_mech_response("not json") is None

    def test_json_decode_error_on_non_string(self) -> None:
        """Non-string input (raises TypeError internally) returns None."""
        assert parse_mech_response(123) is None  # type: ignore[arg-type]

    def test_case_a_invalid_market(self) -> None:
        """(is_valid=False, is_determinable=None, has_occurred=None) -> INVALID."""
        result = parse_mech_response(
            json.dumps(
                {"is_valid": False, "agreement_ratio": 0.0, "judge_reasoning": "bad q"}
            )
        )
        assert result is not None
        assert result["answer"] == ANSWER_INVALID
        assert result["is_valid"] is False
        assert result["is_determinable"] is None
        assert result["has_occurred"] is None

    def test_case_a_fields_populated(self) -> None:
        """Case A result populates all standard fields."""
        result = parse_mech_response(
            json.dumps(
                {
                    "is_valid": False,
                    "agreement_ratio": 0.9,
                    "judge_reasoning": "reason",
                }
            )
        )
        assert result is not None
        assert result["agreement_ratio"] == 0.9
        assert result["reasoning"] == "reason"
        assert result["agrees_with_on_chain"] is None

    def test_case_b_undeterminable(self) -> None:
        """(is_valid=True, is_determinable=False) -> answer=None (retry)."""
        result = parse_mech_response(
            json.dumps(
                {
                    "is_valid": True,
                    "is_determinable": False,
                    "judge_reasoning": "unclear",
                }
            )
        )
        assert result is not None
        assert result["answer"] is None
        assert result["is_determinable"] is False

    def test_case_c1_yes(self) -> None:
        """(is_valid=True, is_determinable=True, has_occurred=True) -> YES."""
        result = parse_mech_response(
            json.dumps(
                {
                    "is_valid": True,
                    "is_determinable": True,
                    "has_occurred": True,
                    "agreement_ratio": 1.0,
                }
            )
        )
        assert result is not None
        assert result["answer"] == ANSWER_YES

    def test_case_c2_no(self) -> None:
        """(is_valid=True, is_determinable=True, has_occurred=False) -> NO."""
        result = parse_mech_response(
            json.dumps(
                {
                    "is_valid": True,
                    "is_determinable": True,
                    "has_occurred": False,
                    "agreement_ratio": 0.8,
                }
            )
        )
        assert result is not None
        assert result["answer"] == ANSWER_NO

    def test_garbage_is_valid_false_determinable_false(self) -> None:
        """(is_valid=False, is_determinable=False) is garbage -> None."""
        result = parse_mech_response(
            json.dumps({"is_valid": False, "is_determinable": False})
        )
        assert result is None

    def test_garbage_is_valid_true_determinable_true_no_occurred(self) -> None:
        """(is_valid=True, is_determinable=True, has_occurred=None) is garbage -> None."""
        result = parse_mech_response(
            json.dumps({"is_valid": True, "is_determinable": True})
        )
        assert result is None

    def test_garbage_is_valid_none(self) -> None:
        """(is_valid=None) is garbage -> None."""
        result = parse_mech_response(json.dumps({}))
        assert result is None

    def test_garbage_jury_all_voters_failed_payload(self) -> None:
        """``all_voters_failed`` error payload from the jury -> garbage (None).

        Cross-repo contract: when every voter in resolve-market-jury-v1
        errors out (e.g. HTTP 402 quota), the jury emits ``(None, None,
        None) + error="all_voters_failed"``. The parser must NOT treat
        this as INVALID -- ``is_valid`` is ``None``, not ``False``.
        """
        result = parse_mech_response(
            json.dumps(
                {
                    "is_valid": None,
                    "is_determinable": None,
                    "has_occurred": None,
                    "error": "all_voters_failed",
                    "judge_reasoning": "All voters failed.",
                    "n_voters": 4,
                    "n_successful": 0,
                    "n_decided": 0,
                    "agreement_ratio": 0.0,
                }
            )
        )
        assert result is None

    def test_garbage_jury_judge_unparseable_payload(self) -> None:
        """``judge_unparseable`` discriminator -> garbage (None)."""
        result = parse_mech_response(
            json.dumps(
                {
                    "is_valid": None,
                    "is_determinable": None,
                    "has_occurred": None,
                    "error": "judge_unparseable",
                }
            )
        )
        assert result is None

    def test_garbage_jury_malformed_verdict_payload(self) -> None:
        """``malformed_verdict`` discriminator -> garbage (None)."""
        result = parse_mech_response(
            json.dumps(
                {
                    "is_valid": None,
                    "is_determinable": None,
                    "has_occurred": None,
                    "error": "malformed_verdict",
                }
            )
        )
        assert result is None

    def test_agreement_ratio_defaults_to_zero(self) -> None:
        """agreement_ratio defaults to 0.0 when absent."""
        result = parse_mech_response(
            json.dumps(
                {"is_valid": True, "is_determinable": True, "has_occurred": True}
            )
        )
        assert result is not None
        assert result["agreement_ratio"] == 0.0

    def test_reasoning_defaults_to_empty(self) -> None:
        """judge_reasoning defaults to '' when absent."""
        result = parse_mech_response(
            json.dumps(
                {"is_valid": True, "is_determinable": True, "has_occurred": True}
            )
        )
        assert result is not None
        assert result["reasoning"] == ""

    def test_agreement_ratio_explicit_null_coerces_to_zero(self) -> None:
        """`agreement_ratio: null` in the payload coerces to 0.0 without crashing.

        `dict.get(k, 0.0)` returns `None` (not the default) when the key
        is present with value null, which would crash `float(None)`.
        """
        result = parse_mech_response(
            json.dumps(
                {
                    "is_valid": True,
                    "is_determinable": True,
                    "has_occurred": True,
                    "agreement_ratio": None,
                }
            )
        )
        assert result is not None
        assert result["agreement_ratio"] == 0.0

    def test_non_dict_top_level_json_returns_none(self) -> None:
        """Non-dict top-level JSON (string / list / null) returns None.

        Without the isinstance guard, `data.get(...)` would raise
        AttributeError and crash the round.
        """
        assert parse_mech_response(json.dumps("just a string")) is None
        assert parse_mech_response(json.dumps([1, 2, 3])) is None
        assert parse_mech_response(json.dumps(None)) is None

    def test_non_numeric_agreement_ratio_returns_none(self) -> None:
        """A string in `agreement_ratio` raises ValueError inside float(); catch it."""
        result = parse_mech_response(
            json.dumps(
                {
                    "is_valid": True,
                    "is_determinable": True,
                    "has_occurred": True,
                    "agreement_ratio": "not-a-number",
                }
            )
        )
        assert result is None


# ---------------------------------------------------------------------------
# jury_error_discriminator
# ---------------------------------------------------------------------------


class TestJuryErrorDiscriminator:
    """Tests for ``jury_error_discriminator``.

    Operator-facing observability: when the jury emits an off-contract
    payload, the ``error`` field tells the operator API outage from a
    genuine parser failure. The helper extracts it for logging.
    """

    def test_none_input(self) -> None:
        """``None`` payload returns ``None``."""
        assert jury_error_discriminator(None) is None

    def test_non_json(self) -> None:
        """Non-JSON garbage returns ``None`` (handled by upstream warning)."""
        assert jury_error_discriminator("not json") is None

    def test_non_dict_json(self) -> None:
        """Top-level non-dict JSON returns ``None``."""
        assert jury_error_discriminator(json.dumps([1, 2, 3])) is None
        assert jury_error_discriminator(json.dumps("scalar")) is None

    def test_dict_without_error_field(self) -> None:
        """A normal verdict (no ``error``) returns ``None``."""
        result = jury_error_discriminator(
            json.dumps(
                {"is_valid": True, "is_determinable": True, "has_occurred": True}
            )
        )
        assert result is None

    def test_all_voters_failed(self) -> None:
        """The jury's ``all_voters_failed`` discriminator surfaces."""
        result = jury_error_discriminator(
            json.dumps(
                {
                    "is_valid": None,
                    "is_determinable": None,
                    "has_occurred": None,
                    "error": "all_voters_failed",
                }
            )
        )
        assert result == "all_voters_failed"

    def test_judge_unparseable(self) -> None:
        """The jury's ``judge_unparseable`` discriminator surfaces."""
        result = jury_error_discriminator(json.dumps({"error": "judge_unparseable"}))
        assert result == "judge_unparseable"

    def test_malformed_verdict(self) -> None:
        """The jury's ``malformed_verdict`` discriminator surfaces."""
        result = jury_error_discriminator(json.dumps({"error": "malformed_verdict"}))
        assert result == "malformed_verdict"

    def test_empty_string_error_is_ignored(self) -> None:
        """An empty-string ``error`` field is treated as no error."""
        assert jury_error_discriminator(json.dumps({"error": ""})) is None

    def test_non_string_error_is_ignored(self) -> None:
        """A non-string ``error`` field is treated as no error."""
        assert jury_error_discriminator(json.dumps({"error": 42})) is None
        assert jury_error_discriminator(json.dumps({"error": None})) is None


# ---------------------------------------------------------------------------
# is_cached_evaluation_valid
# ---------------------------------------------------------------------------


class TestIsCachedEvaluationValid:
    """Tests for is_cached_evaluation_valid."""

    def test_none_returns_false(self) -> None:
        """None evaluation is not valid."""
        assert is_cached_evaluation_valid(None) is False

    def test_answer_none_returns_false(self) -> None:
        """Evaluation with answer=None (Case B) is not valid."""
        assert is_cached_evaluation_valid({"answer": None}) is False

    def test_answer_yes_returns_true(self) -> None:
        """Evaluation with ANSWER_YES is valid."""
        assert is_cached_evaluation_valid({"answer": ANSWER_YES}) is True

    def test_answer_no_returns_true(self) -> None:
        """Evaluation with ANSWER_NO is valid."""
        assert is_cached_evaluation_valid({"answer": ANSWER_NO}) is True

    def test_answer_invalid_returns_true(self) -> None:
        """Evaluation with ANSWER_INVALID is valid."""
        assert is_cached_evaluation_valid({"answer": ANSWER_INVALID}) is True


# ---------------------------------------------------------------------------
# to_content
# ---------------------------------------------------------------------------


class TestToContent:
    """Tests for to_content helper."""

    def test_returns_bytes(self) -> None:
        """to_content returns bytes."""
        result = to_content("query { }")
        assert isinstance(result, bytes)

    def test_wraps_in_query_key(self) -> None:
        """to_content wraps query in {"query": ...} JSON."""
        result = to_content("my_query")
        data = json.loads(result)
        assert data == {"query": "my_query"}


# ---------------------------------------------------------------------------
# Base behaviour helper methods (via ScanMarketsBehaviour as concrete class)
# ---------------------------------------------------------------------------


def _make_behaviour(
    questions_db: Any = None, synced_data: Any = None
) -> ScanMarketsBehaviour:
    """Instantiate ScanMarketsBehaviour with a mocked context + synced_data patched."""
    context = _make_context(questions_db)
    behaviour = ScanMarketsBehaviour(name="scan", skill_context=context)
    sd = synced_data if synced_data is not None else _make_synced_data()
    patcher = patch.object(
        type(behaviour),
        "synchronized_data",
        new_callable=PropertyMock,
        return_value=sd,
    )
    patcher.start()
    behaviour._sd_patcher = patcher
    return behaviour


class TestBaseBehaviourProperties:
    """Tests for property accessors on MarketResolutionManagerBaseBehaviour."""

    def test_questions_db_get(self) -> None:
        """questions_db getter reads from context.state."""
        db = {"0xM": {"status": "NEEDS_ANSWER"}}
        b = _make_behaviour(questions_db=db)
        assert b.questions_db is db

    def test_questions_db_set(self) -> None:
        """questions_db setter writes to context.state."""
        b = _make_behaviour()
        b.questions_db = {"0xM2": {"status": "VERIFIED"}}
        assert b.context.state.questions_db == {"0xM2": {"status": "VERIFIED"}}

    def test_last_synced_timestamp(self) -> None:
        """last_synced_timestamp returns int from round_sequence."""
        b = _make_behaviour()
        ts = b.last_synced_timestamp
        assert isinstance(ts, int)
        assert ts == 1_700_000_000

    def test_synchronized_data_cast(self) -> None:
        """synchronized_data property casts BaseBehaviour result to SynchronizedData."""
        # Build behaviour without PropertyMock, then exercise the real property.
        context = _make_context()
        behaviour = ScanMarketsBehaviour(name="scan", skill_context=context)
        sd_stub = MagicMock()
        # Patch the PARENT property so super().synchronized_data returns our stub,
        # which forces the cast() line in the subclass property to execute.
        with patch(
            "packages.valory.skills.abstract_round_abci.behaviours.BaseBehaviour.synchronized_data",
            new_callable=PropertyMock,
            return_value=sd_stub,
        ):
            assert behaviour.synchronized_data is sd_stub


class TestGetNativeBalance:
    """Tests for get_native_balance generator method."""

    def test_success(self) -> None:
        """Returns balance when ledger API returns STATE performative."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.performative = LedgerApiMessage.Performative.STATE
        mock_resp.state.body = {"get_balance_result": 5 * 10**18}

        with patch.object(b, "get_ledger_api_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_native_balance(SAFE_ADDRESS))

        assert result == 5 * 10**18

    def test_error_response_returns_none(self) -> None:
        """Returns None when ledger API returns non-STATE performative."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.performative = LedgerApiMessage.Performative.ERROR

        with patch.object(b, "get_ledger_api_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_native_balance(SAFE_ADDRESS))

        assert result is None


class TestGetOmenSubgraphResult:
    """Tests for get_omen_subgraph_result."""

    def test_success(self) -> None:
        """Returns parsed JSON on 200 response."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = json.dumps({"data": {"markets": []}}).encode()

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_omen_subgraph_result("{ query }"))

        assert result == {"data": {"markets": []}}

    def test_none_response_returns_none(self) -> None:
        """Returns None when HTTP response is None."""
        b = _make_behaviour()
        with patch.object(b, "get_http_response", new=_make_gen(None)):
            result = _exhaust_gen(b.get_omen_subgraph_result("{ query }"))
        assert result is None

    def test_non_200_response_returns_none(self) -> None:
        """Returns None on non-200 status code."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_omen_subgraph_result("{ query }"))

        assert result is None

    def test_200_with_non_json_body_returns_none(self) -> None:
        """200 OK with non-JSON body (e.g. CDN HTML page) returns None, no crash.

        The Graph gateway occasionally serves HTML interstitials with a
        200 response under load. The helper must not propagate
        JSONDecodeError -- callers rely on the None contract.
        """
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = b"<html><body>Cloudflare rate-limit</body></html>"

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_omen_subgraph_result("{ query }"))

        assert result is None

    def test_200_with_invalid_utf8_body_returns_none(self) -> None:
        """200 OK with body that cannot be decoded as UTF-8 returns None."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = b"\xff\xfe\xfd\xfc"  # invalid UTF-8

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_omen_subgraph_result("{ query }"))

        assert result is None


class TestGetMechGnosisSubgraphResult:
    """Tests for get_mech_gnosis_subgraph_result.

    The helper now delegates to ``ApiSpecs.process_response`` so the
    configured ``response_key`` (``data:requests``) is walked by the
    framework. These tests stub ``process_response`` per case to assert
    that its return value is propagated and that the wrapping error
    paths still short-circuit.
    """

    def test_success_returns_processed_value(self) -> None:
        """Returns whatever ``process_response`` returns on 200 response."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = json.dumps({"data": {"requests": [{"id": "r1"}]}}).encode()
        b.context.mech_gnosis_subgraph.process_response.return_value = [{"id": "r1"}]

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_mech_gnosis_subgraph_result("{ q }"))

        assert result == [{"id": "r1"}]
        b.context.mech_gnosis_subgraph.process_response.assert_called_once_with(
            mock_resp
        )

    def test_none_response_returns_none(self) -> None:
        """Returns None when HTTP response is None."""
        b = _make_behaviour()
        with patch.object(b, "get_http_response", new=_make_gen(None)):
            result = _exhaust_gen(b.get_mech_gnosis_subgraph_result("{ q }"))
        assert result is None
        b.context.mech_gnosis_subgraph.process_response.assert_not_called()

    def test_non_200_returns_none(self) -> None:
        """Returns None on non-200 status."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 503

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_mech_gnosis_subgraph_result("{ q }"))

        assert result is None
        b.context.mech_gnosis_subgraph.process_response.assert_not_called()

    def test_process_response_returns_none_propagates(self) -> None:
        """If ``process_response`` returns None (e.g. bad path), caller gets None."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = b"<html>rate limited</html>"
        b.context.mech_gnosis_subgraph.process_response.return_value = None

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_mech_gnosis_subgraph_result("{ q }"))

        assert result is None


class TestGetRealitioSubgraphResult:
    """Tests for get_realitio_subgraph_result."""

    def test_success(self) -> None:
        """Returns parsed JSON on 200."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = json.dumps({"data": {}}).encode()

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_realitio_subgraph_result("{ q }"))

        assert result == {"data": {}}

    def test_none_response_returns_none(self) -> None:
        """Returns None when HTTP is None."""
        b = _make_behaviour()
        with patch.object(b, "get_http_response", new=_make_gen(None)):
            result = _exhaust_gen(b.get_realitio_subgraph_result("{ q }"))
        assert result is None

    def test_non_200_returns_none(self) -> None:
        """Returns None on non-200."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_realitio_subgraph_result("{ q }"))
        assert result is None

    def test_200_with_non_json_body_returns_none(self) -> None:
        """200 OK with non-JSON body returns None, no crash."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = b"<html>maintenance</html>"

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_realitio_subgraph_result("{ q }"))

        assert result is None


class TestFetchMechRequestsForMarket:
    """Tests for ``fetch_mech_requests_for_market``.

    Returns the raw filtered list of subgraph request entries (matching
    tool + title for the Safe), or ``None`` on subgraph error. The
    earliest valid evaluation is derived separately by
    ``ScanMarketsBehaviour._earliest_valid_evaluation`` -- not by this
    function.
    """

    def _make_entry(
        self, title: str = "Will X happen?", closing_ts: int = 1_700_000_000
    ) -> Dict[str, Any]:
        return {"title": title, "market_closing_timestamp": closing_ts}

    def _make_request_entry(
        self,
        deliveries: Any = None,
        tool: str = "resolve-market-jury-v1",
        prompt: str = "Will X happen?",
        req_id: str = "req1",
    ) -> Dict[str, Any]:
        return {
            "id": req_id,
            "blockTimestamp": "1700001000",
            "parsedRequest": {"tool": tool, "prompt": prompt},
            "deliveries": deliveries if deliveries is not None else [],
        }

    def test_subgraph_error_returns_none(self) -> None:
        """Returns ``None`` when subgraph call fails."""
        b = _make_behaviour()
        with patch.object(b, "get_mech_gnosis_subgraph_result", new=_make_gen(None)):
            result = _exhaust_gen(b.fetch_mech_requests_for_market(self._make_entry()))
        assert result is None

    def test_empty_list_returns_empty_list(self) -> None:
        """Empty list from subgraph helper -> empty list (no error)."""
        b = _make_behaviour()
        with patch.object(b, "get_mech_gnosis_subgraph_result", new=_make_gen([])):
            result = _exhaust_gen(b.fetch_mech_requests_for_market(self._make_entry()))
        assert result == []

    def test_wrong_tool_filtered_out(self) -> None:
        """Request with a non-matching tool is filtered out of the list."""
        b = _make_behaviour()
        requests = [
            self._make_request_entry(
                deliveries=[
                    {
                        "toolResponse": json.dumps(
                            {
                                "is_valid": True,
                                "is_determinable": True,
                                "has_occurred": True,
                            }
                        )
                    }
                ],
                tool="wrong-tool",
            )
        ]
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen(requests)
        ):
            result = _exhaust_gen(b.fetch_mech_requests_for_market(self._make_entry()))
        assert result == []

    def test_wrong_prompt_filtered_out(self) -> None:
        """Request with a non-matching prompt is filtered out of the list."""
        b = _make_behaviour()
        request_entry = self._make_request_entry(
            deliveries=[
                {
                    "toolResponse": json.dumps(
                        {
                            "is_valid": True,
                            "is_determinable": True,
                            "has_occurred": True,
                        }
                    )
                }
            ],
            prompt="Different question?",
        )
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen([request_entry])
        ):
            result = _exhaust_gen(b.fetch_mech_requests_for_market(self._make_entry()))
        assert result == []

    def test_no_deliveries_kept_in_list(self) -> None:
        """A request with empty ``deliveries`` is still kept in the list.

        Production uses ``len(mech_requests)`` to drive the retry-counter
        gate, so unanswered requests must contribute even if no
        evaluation can be derived from them.
        """
        b = _make_behaviour()
        request_entry = self._make_request_entry(deliveries=[])
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen([request_entry])
        ):
            result = _exhaust_gen(b.fetch_mech_requests_for_market(self._make_entry()))
        assert result == [request_entry]

    def test_garbage_response_kept_in_list(self) -> None:
        """A request with an unparseable ``toolResponse`` is still kept."""
        b = _make_behaviour()
        request_entry = self._make_request_entry(
            deliveries=[{"toolResponse": "invalid json {{"}]
        )
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen([request_entry])
        ):
            result = _exhaust_gen(b.fetch_mech_requests_for_market(self._make_entry()))
        assert result == [request_entry]

    def test_undeterminable_response_kept_in_list(self) -> None:
        """A request whose verdict is undeterminable is still kept.

        ``answer=None`` (Case B) fails ``is_cached_evaluation_valid`` so
        no evaluation is derived, but the entry is retained for
        retry-count purposes.
        """
        b = _make_behaviour()
        request_entry = self._make_request_entry(
            deliveries=[
                {
                    "toolResponse": json.dumps(
                        {"is_valid": True, "is_determinable": False}
                    )
                }
            ]
        )
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen([request_entry])
        ):
            result = _exhaust_gen(b.fetch_mech_requests_for_market(self._make_entry()))
        assert result == [request_entry]

    def test_valid_cache_hit_returns_request_in_list(self) -> None:
        """A request with a valid YES verdict is returned in the list verbatim.

        The verdict-extraction is the caller's responsibility (via
        ``_earliest_valid_evaluation`` in scan_markets); this function
        only filters + returns the raw request entries.
        """
        b = _make_behaviour()
        tool_response = json.dumps(
            {"is_valid": True, "is_determinable": True, "has_occurred": True}
        )
        request_entry = self._make_request_entry(
            deliveries=[{"toolResponse": tool_response}], req_id="req-abc"
        )
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen([request_entry])
        ):
            result = _exhaust_gen(b.fetch_mech_requests_for_market(self._make_entry()))

        assert isinstance(result, list)
        assert result == [request_entry]
        # Verify the caller can derive YES from this list via the helper.
        evaluation = ScanMarketsBehaviour._earliest_valid_evaluation(result)
        assert evaluation is not None
        assert evaluation["answer"] == ANSWER_YES

    def test_helper_returns_list_directly_no_data_walk(self) -> None:
        """Regression: helper returns the list directly (no ``data`` wrapper).

        After switching to ``ApiSpecs.process_response``, the configured
        ``response_key: data:requests`` is walked by the framework, so
        ``get_mech_gnosis_subgraph_result`` hands a list back -- not
        a ``{"data": {"requests": [...]}}`` wrapper. This test pins
        that contract so a regression to manual ``data.sender.requests``
        walking would fail loudly.
        """
        b = _make_behaviour()
        request_entry = self._make_request_entry(req_id="req-top-level")
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen([request_entry])
        ):
            result = _exhaust_gen(b.fetch_mech_requests_for_market(self._make_entry()))
        assert result is not None
        assert len(result) == 1
        assert result[0]["id"] == "req-top-level"

    def test_query_renders_top_level_request_with_tool_filter(self) -> None:
        """The rendered query uses top-level ``requests(where:`` with tool.

        Pins two invariants:

        - ``parsedRequest { nonce }`` is NOT selected. ``nonce`` is not a
          field on ``ParsedRequest`` in the marketplace-gnosis subgraph,
          and selecting it makes the whole query error with ``data:
          null`` -- which previously masqueraded as "zero matching
          requests" and silently disabled the retry-budget gate.
        - The query uses top-level ``requests(where:`` rather than the
          older ``sender(id) { requests(where:) }`` traversal. Both
          shapes return the same rows on this subgraph, but the
          top-level form is the canonical pattern used by watchdog and
          trader and is what the ``response_key: data:requests`` config
          walks.
        """
        rendered = MECH_CACHE_QUERY_TEMPLATE.format(
            sender="0xabc",
            prompt=json.dumps("Will X?"),
            tool="resolve-market-jury-v1",
            block_timestamp_gt=123,
        )
        assert "requests(" in rendered
        assert "sender(id:" not in rendered
        assert "nonce" not in rendered
        assert 'tool: "resolve-market-jury-v1"' in rendered


# ---------------------------------------------------------------------------
# ScanMarketsBehaviour._earliest_valid_evaluation -- helper unit tests
# ---------------------------------------------------------------------------


class TestEarliestValidEvaluation:
    """Tests for ``_earliest_valid_evaluation``.

    Behaviour-independent unit tests for the static helper that derives
    the first usable evaluation from a list of subgraph request entries.
    """

    @staticmethod
    def _valid_yes_response() -> str:
        return json.dumps(
            {"is_valid": True, "is_determinable": True, "has_occurred": True}
        )

    @staticmethod
    def _undeterminable_response() -> str:
        return json.dumps({"is_valid": True, "is_determinable": False})

    def test_empty_list_returns_none(self) -> None:
        """No requests -> None."""
        assert ScanMarketsBehaviour._earliest_valid_evaluation([]) is None

    def test_request_with_no_deliveries_returns_none(self) -> None:
        """Requests present but none have deliveries -> None."""
        reqs: List[Dict[str, Any]] = [{"deliveries": []}, {"deliveries": []}]
        assert ScanMarketsBehaviour._earliest_valid_evaluation(reqs) is None

    def test_garbage_only_delivery_returns_none(self) -> None:
        """A single unparseable delivery -> None (caller retries)."""
        reqs = [{"deliveries": [{"toolResponse": "not json"}]}]
        assert ScanMarketsBehaviour._earliest_valid_evaluation(reqs) is None

    def test_undeterminable_only_delivery_returns_none(self) -> None:
        """A single Case B (undeterminable) delivery -> None."""
        reqs = [{"deliveries": [{"toolResponse": self._undeterminable_response()}]}]
        assert ScanMarketsBehaviour._earliest_valid_evaluation(reqs) is None

    def test_valid_single_delivery_returns_evaluation(self) -> None:
        """A single valid YES delivery is returned."""
        reqs = [{"deliveries": [{"toolResponse": self._valid_yes_response()}]}]
        evaluation = ScanMarketsBehaviour._earliest_valid_evaluation(reqs)
        assert evaluation is not None
        assert evaluation["answer"] == ANSWER_YES

    def test_multi_delivery_skips_garbage_and_returns_later_valid(self) -> None:
        """Regression for #3248049101: a request with [garbage, valid] deliveries.

        Production used to read only ``deliveries[0]`` -- if the first
        delivery is unparseable garbage (e.g. Mech-internal retry), the
        valid second delivery is missed and the market stays stuck. The
        helper now iterates ALL deliveries per request.
        """
        reqs = [
            {
                "deliveries": [
                    {"toolResponse": "not json"},
                    {"toolResponse": self._valid_yes_response()},
                ]
            }
        ]
        evaluation = ScanMarketsBehaviour._earliest_valid_evaluation(reqs)
        assert evaluation is not None
        assert evaluation["answer"] == ANSWER_YES

    def test_first_valid_across_multiple_requests_wins(self) -> None:
        """When multiple requests have valid deliveries, take the earliest."""
        reqs = [
            # Earlier request -- one undeterminable + one valid YES
            {
                "deliveries": [
                    {"toolResponse": self._undeterminable_response()},
                    {"toolResponse": self._valid_yes_response()},
                ]
            },
            # Later request -- valid NO (not reached, earlier wins)
            {
                "deliveries": [
                    {
                        "toolResponse": json.dumps(
                            {
                                "is_valid": True,
                                "is_determinable": True,
                                "has_occurred": False,
                            }
                        )
                    }
                ]
            },
        ]
        evaluation = ScanMarketsBehaviour._earliest_valid_evaluation(reqs)
        assert evaluation is not None
        assert evaluation["answer"] == ANSWER_YES
