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
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    ANSWER_INVALID,
    ANSWER_NO,
    ANSWER_YES,
    is_cached_evaluation_valid,
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


class TestGetMechGnosisSubgraphResult:
    """Tests for get_mech_gnosis_subgraph_result."""

    def test_success(self) -> None:
        """Returns parsed JSON on 200 response."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = json.dumps({"data": {"sender": {}}}).encode()

        with patch.object(b, "get_http_response", new=_make_gen(mock_resp)):
            result = _exhaust_gen(b.get_mech_gnosis_subgraph_result("{ q }"))

        assert result is not None

    def test_none_response_returns_none(self) -> None:
        """Returns None when HTTP response is None."""
        b = _make_behaviour()
        with patch.object(b, "get_http_response", new=_make_gen(None)):
            result = _exhaust_gen(b.get_mech_gnosis_subgraph_result("{ q }"))
        assert result is None

    def test_non_200_returns_none(self) -> None:
        """Returns None on non-200 status."""
        b = _make_behaviour()
        mock_resp = MagicMock()
        mock_resp.status_code = 503

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


class TestFindCachedValidMechDelivery:
    """Tests for find_cached_valid_mech_delivery."""

    def _make_entry(
        self, title: str = "Will X happen?", closing_ts: int = 1_700_000_000
    ) -> dict:
        return {"title": title, "market_closing_timestamp": closing_ts}

    def test_missing_title_returns_empty_dict(self) -> None:
        """Returns {} when entry has no title."""
        b = _make_behaviour()
        result = _exhaust_gen(
            b.find_cached_valid_mech_delivery("0xM", {"market_closing_timestamp": 1234})
        )
        assert result == {}

    def test_missing_closing_ts_returns_empty_dict(self) -> None:
        """Returns {} when entry has no market_closing_timestamp."""
        b = _make_behaviour()
        result = _exhaust_gen(b.find_cached_valid_mech_delivery("0xM", {"title": "Q?"}))
        assert result == {}

    def test_no_safe_address_returns_none(self) -> None:
        """Returns None when safe_contract_address is falsy."""
        b = _make_behaviour(synced_data=_make_synced_data(safe_address=""))
        result = _exhaust_gen(
            b.find_cached_valid_mech_delivery("0xM", self._make_entry())
        )
        assert result is None

    def test_subgraph_error_returns_none(self) -> None:
        """Returns None when subgraph call fails."""
        b = _make_behaviour()
        with patch.object(b, "get_mech_gnosis_subgraph_result", new=_make_gen(None)):
            result = _exhaust_gen(
                b.find_cached_valid_mech_delivery("0xM", self._make_entry())
            )
        assert result is None

    def test_no_sender_data_returns_empty_dict(self) -> None:
        """Returns {} when subgraph has no sender data."""
        b = _make_behaviour()
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen({"data": {}})
        ):
            result = _exhaust_gen(
                b.find_cached_valid_mech_delivery("0xM", self._make_entry())
            )
        assert result == {}

    def test_no_matching_requests_returns_empty_dict(self) -> None:
        """Returns {} when sender has requests but none match tool/title/delivery."""
        b = _make_behaviour()
        subgraph_data = {
            "data": {
                "sender": {
                    "requests": [
                        {
                            "id": "req1",
                            "blockTimestamp": "1700001000",
                            "parsedRequest": {
                                "tool": "wrong-tool",
                                "prompt": "Will X happen?",
                            },
                            "deliveries": [
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
                        }
                    ]
                }
            }
        }
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen(subgraph_data)
        ):
            result = _exhaust_gen(
                b.find_cached_valid_mech_delivery("0xM", self._make_entry())
            )
        assert result == {}

    def test_wrong_prompt_skipped(self) -> None:
        """Request with wrong prompt (different title) is skipped."""
        b = _make_behaviour()
        subgraph_data = {
            "data": {
                "sender": {
                    "requests": [
                        {
                            "id": "req1",
                            "blockTimestamp": "1700001000",
                            "parsedRequest": {
                                "tool": "resolve-market-jury-v1",
                                "prompt": "Different question?",
                            },
                            "deliveries": [
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
                        }
                    ]
                }
            }
        }
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen(subgraph_data)
        ):
            result = _exhaust_gen(
                b.find_cached_valid_mech_delivery("0xM", self._make_entry())
            )
        assert result == {}

    def test_no_delivery_skipped(self) -> None:
        """Request with empty deliveries is skipped."""
        b = _make_behaviour()
        subgraph_data = {
            "data": {
                "sender": {
                    "requests": [
                        {
                            "id": "req1",
                            "blockTimestamp": "1700001000",
                            "parsedRequest": {
                                "tool": "resolve-market-jury-v1",
                                "prompt": "Will X happen?",
                            },
                            "deliveries": [],
                        }
                    ]
                }
            }
        }
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen(subgraph_data)
        ):
            result = _exhaust_gen(
                b.find_cached_valid_mech_delivery("0xM", self._make_entry())
            )
        assert result == {}

    def test_garbage_response_skipped(self) -> None:
        """Request with garbage tool response is skipped (evaluation=None)."""
        b = _make_behaviour()
        subgraph_data = {
            "data": {
                "sender": {
                    "requests": [
                        {
                            "id": "req1",
                            "blockTimestamp": "1700001000",
                            "parsedRequest": {
                                "tool": "resolve-market-jury-v1",
                                "prompt": "Will X happen?",
                            },
                            "deliveries": [{"toolResponse": "invalid json {{"}],
                        }
                    ]
                }
            }
        }
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen(subgraph_data)
        ):
            result = _exhaust_gen(
                b.find_cached_valid_mech_delivery("0xM", self._make_entry())
            )
        assert result == {}

    def test_undeterminable_response_skipped(self) -> None:
        """Undeterminable evaluation (answer=None) fails is_cached_evaluation_valid -> skipped."""
        b = _make_behaviour()
        subgraph_data = {
            "data": {
                "sender": {
                    "requests": [
                        {
                            "id": "req1",
                            "blockTimestamp": "1700001000",
                            "parsedRequest": {
                                "tool": "resolve-market-jury-v1",
                                "prompt": "Will X happen?",
                            },
                            "deliveries": [
                                {
                                    "toolResponse": json.dumps(
                                        {"is_valid": True, "is_determinable": False}
                                    )
                                }
                            ],
                        }
                    ]
                }
            }
        }
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen(subgraph_data)
        ):
            result = _exhaust_gen(
                b.find_cached_valid_mech_delivery("0xM", self._make_entry())
            )
        assert result == {}

    def test_valid_cache_hit_returns_dict(self) -> None:
        """Returns cache-hit dict with evaluation and mech_response on success."""
        b = _make_behaviour()
        tool_response = json.dumps(
            {"is_valid": True, "is_determinable": True, "has_occurred": True}
        )
        subgraph_data = {
            "data": {
                "sender": {
                    "requests": [
                        {
                            "id": "req-abc",
                            "blockTimestamp": "1700001000",
                            "parsedRequest": {
                                "tool": "resolve-market-jury-v1",
                                "prompt": "Will X happen?",
                            },
                            "deliveries": [{"toolResponse": tool_response}],
                        }
                    ]
                }
            }
        }
        with patch.object(
            b, "get_mech_gnosis_subgraph_result", new=_make_gen(subgraph_data)
        ):
            result = _exhaust_gen(
                b.find_cached_valid_mech_delivery("0xM", self._make_entry())
            )

        assert isinstance(result, dict)
        assert "evaluation" in result
        assert "mech_response" in result
        assert result["evaluation"]["answer"] == ANSWER_YES
        assert result["mech_response"]["source"] == "subgraph"
        assert result["mech_response"]["subgraph_request_id"] == "req-abc"
