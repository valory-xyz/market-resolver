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

"""Tests for behaviours/scan_markets.py."""

# pylint: disable=protected-access,unused-argument,unused-variable
# pylint: disable=too-many-arguments,too-many-positional-arguments
# pylint: disable=too-few-public-methods,too-many-public-methods
# pylint: disable=use-implicit-booleaness-not-comparison,not-an-iterable

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.skills.market_resolution_manager_abci.behaviours.base import (
    ANSWER_NO,
    ANSWER_YES,
)
from packages.valory.skills.market_resolution_manager_abci.behaviours.scan_markets import (
    ScanMarketsBehaviour,
)
from packages.valory.skills.market_resolution_manager_abci.states.base import (
    AnswerStatus,
)

from .conftest import (
    SAFE_ADDRESS,
    _exhaust_gen,
    _make_context,
    _make_gen,
    _make_synced_data,
)

NOW = 1_700_000_000


def _make_market(
    market_id: str = "0xMarket1",
    title: str = "Will X happen?",
    current_answer: Optional[str] = None,
    current_answer_bond: Optional[str] = None,
    current_answer_timestamp: Optional[str] = None,
    opening_timestamp: str = "1699000000",
    timeout: str = "86400",
    question_id: str = "0xQuestion1",
) -> Dict[str, Any]:
    """Build a minimal market dict from the Omen subgraph."""
    return {
        "id": market_id,
        "creator": "0xCreator",
        "currentAnswer": current_answer,
        "currentAnswerBond": current_answer_bond,
        "currentAnswerTimestamp": current_answer_timestamp,
        "openingTimestamp": opening_timestamp,
        "timeout": timeout,
        "question": {
            "id": question_id,
            "data": "",
            "currentAnswerBond": current_answer_bond,
        },
        "title": title,
    }


def _make_behaviour(
    questions_db: Optional[Dict] = None, synced_data: Optional[MagicMock] = None
) -> ScanMarketsBehaviour:
    """Instantiate ScanMarketsBehaviour with mocked context + synchronized_data patched."""
    context = _make_context(questions_db or {})
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


def _run_async_act(behaviour: ScanMarketsBehaviour) -> None:
    """Drive async_act to completion."""
    gen = behaviour.async_act()
    try:
        while True:
            next(gen)
    except StopIteration:
        pass


class TestScanMarketsBehaviourNoWatchedAddresses:
    """Test that no watched addresses triggers NONE payload."""

    def test_sends_none_payload_when_no_watched_addresses(self) -> None:
        """No watched addresses -> _send_none_payload."""
        b = _make_behaviour()
        b.context.params.watched_creator_addresses = []

        mock_send = MagicMock(side_effect=_make_gen(None))
        with (
            patch.object(b, "send_a2a_transaction", mock_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert mock_send.called


class TestFetchPendingAndFinalizingMarkets:
    """Tests for _fetch_pending_and_finalizing_markets."""

    def test_success_returns_deduplicated_markets(self) -> None:
        """Returns merged deduplicated list from two subgraph queries."""
        b = _make_behaviour()
        m1 = _make_market("0xM1")
        m2 = _make_market("0xM2")

        call_count = 0

        def mock_subgraph(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"data": {"fixedProductMarketMakers": [m1]}}
            return {"data": {"fixedProductMarketMakers": [m2]}}
            yield  # noqa

        with patch.object(b, "get_omen_subgraph_result", new=mock_subgraph):
            result = _exhaust_gen(
                b._fetch_pending_and_finalizing_markets(["0xCreator"])
            )

        assert result is not None
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert "0xM1" in ids
        assert "0xM2" in ids

    def test_deduplication(self) -> None:
        """Market appearing in both queries is deduplicated."""
        b = _make_behaviour()
        m = _make_market("0xDup")

        def mock_subgraph(*args: Any, **kwargs: Any) -> Any:
            return {"data": {"fixedProductMarketMakers": [m]}}
            yield  # noqa

        with patch.object(b, "get_omen_subgraph_result", new=mock_subgraph):
            result = _exhaust_gen(
                b._fetch_pending_and_finalizing_markets(["0xCreator"])
            )

        assert result is not None
        assert len(result) == 1

    def test_none_result_treated_as_empty(self) -> None:
        """None subgraph response results in empty list for that query."""
        b = _make_behaviour()

        def mock_subgraph(*args: Any, **kwargs: Any) -> Any:
            return None
            yield  # noqa

        with patch.object(b, "get_omen_subgraph_result", new=mock_subgraph):
            result = _exhaust_gen(
                b._fetch_pending_and_finalizing_markets(["0xCreator"])
            )

        assert result == []


class TestFetchCurrentAnswerers:
    """Tests for _fetch_current_answerers."""

    def test_empty_question_ids_returns_empty(self) -> None:
        """Empty input returns empty dict without querying."""
        b = _make_behaviour()
        result = _exhaust_gen(b._fetch_current_answerers([]))
        assert result == {}

    def test_success(self) -> None:
        """Returns answerer mapping from subgraph response."""
        b = _make_behaviour()
        subgraph_data = {
            "data": {
                "questions": [
                    {
                        "questionId": "0xQ1",
                        "responses": [{"user": "0xAnswerer", "timestamp": "100"}],
                    }
                ]
            }
        }

        with patch.object(
            b, "get_realitio_subgraph_result", new=_make_gen(subgraph_data)
        ):
            result = _exhaust_gen(b._fetch_current_answerers(["0xQ1"]))

        assert result == {"0xQ1": "0xanswerer"}

    def test_none_response_skipped(self) -> None:
        """None subgraph response is skipped gracefully."""
        b = _make_behaviour()
        with patch.object(b, "get_realitio_subgraph_result", new=_make_gen(None)):
            result = _exhaust_gen(b._fetch_current_answerers(["0xQ1"]))
        assert result == {}

    def test_no_responses_skipped(self) -> None:
        """Question with empty responses list is skipped."""
        b = _make_behaviour()
        subgraph_data = {
            "data": {"questions": [{"questionId": "0xQ1", "responses": []}]}
        }
        with patch.object(
            b, "get_realitio_subgraph_result", new=_make_gen(subgraph_data)
        ):
            result = _exhaust_gen(b._fetch_current_answerers(["0xQ1"]))
        assert result == {}


class TestNewEntry:
    """Tests for ScanMarketsBehaviour._new_entry static method."""

    def test_creates_entry_with_expected_fields(self) -> None:
        """_new_entry creates a well-formed DB entry."""
        market = _make_market(
            market_id="0xM",
            title="Will Y?",
            opening_timestamp="1699000000",
            timeout="172800",
        )
        entry = ScanMarketsBehaviour._new_entry(market, "0xAnswerer", NOW)
        assert entry["market_id"] == "0xM"
        assert entry["title"] == "Will Y?"
        assert entry["detected_at"] == NOW
        assert entry["last_answerer"] == "0xAnswerer"
        assert entry["realitio_timeout"] == 172800
        assert entry["evaluation"] is None
        assert entry["mech_requests"] == []
        assert entry["pending_nonce"] is None
        assert entry["pending_tx"] is None
        assert entry["mech_retries"] == 0
        assert entry["status"] is None
        # Refactor: ``mech_request`` / ``mech_response`` were dropped --
        # the per-market Mech state is now sourced from the subgraph
        # via ``mech_requests`` (list of subgraph request entries).
        assert "mech_request" not in entry
        assert "mech_response" not in entry

    def test_default_timeout_when_absent(self) -> None:
        """_new_entry uses 86400 as default timeout."""
        market = _make_market()
        market.pop("timeout", None)
        entry = ScanMarketsBehaviour._new_entry(market, "", NOW)
        assert entry["realitio_timeout"] == 86400


class TestActionableSortKey:
    """Tests for ScanMarketsBehaviour._actionable_sort_key static method."""

    def test_unanswered_sorts_last(self) -> None:
        """Unanswered markets (on_chain_answer=None) sort after challenged ones."""
        questions_db = {
            "0xUnanswered": {
                "on_chain_answer": None,
                "last_answer_timestamp": None,
                "realitio_timeout": 86400,
            },
            "0xChallenged": {
                "on_chain_answer": ANSWER_NO,
                "last_answer_timestamp": str(NOW - 3600),
                "realitio_timeout": 86400,
            },
        }
        key_unanswered = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xUnanswered"}
        )
        key_challenged = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xChallenged"}
        )
        # Unanswered has is_unanswered=True -> sort key (1, inf), challenged has (0, x)
        assert key_unanswered > key_challenged

    def test_closer_finalization_sorts_first(self) -> None:
        """Among challenged markets, soonest finalization sorts first."""
        questions_db = {
            "0xUrgent": {
                "on_chain_answer": ANSWER_NO,
                "last_answer_timestamp": str(NOW - 80000),
                "realitio_timeout": 86400,
            },
            "0xLater": {
                "on_chain_answer": ANSWER_NO,
                "last_answer_timestamp": str(NOW - 10000),
                "realitio_timeout": 86400,
            },
        }
        key_urgent = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xUrgent"}
        )
        key_later = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xLater"}
        )
        assert key_urgent < key_later

    def test_zero_last_ts_gives_inf_finalization(self) -> None:
        """When last_answer_timestamp is falsy, finalization = inf."""
        questions_db = {
            "0xM": {
                "on_chain_answer": ANSWER_YES,
                "last_answer_timestamp": None,
                "realitio_timeout": 86400,
            }
        }
        key = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xM"}
        )
        assert key[1] == float("inf")

    def test_fewer_mech_retries_breaks_tie_for_unanswered(self) -> None:
        """Within identical priority+urgency, fewer retries wins.

        Without this tiebreaker the unanswered population all ties at
        ``(1, inf)``, so Python's stable sort hands the slot to whichever
        market was inserted first in ``questions_db`` -- concentrating
        retries on the oldest-by-opening market every cycle.
        """
        questions_db = {
            "0xRetriedTwice": {
                "on_chain_answer": None,
                "last_answer_timestamp": None,
                "realitio_timeout": 86400,
                "mech_retries": 2,
            },
            "0xFresh": {
                "on_chain_answer": None,
                "last_answer_timestamp": None,
                "realitio_timeout": 86400,
                "mech_retries": 0,
            },
        }
        key_retried = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xRetriedTwice"}
        )
        key_fresh = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xFresh"}
        )
        assert key_fresh < key_retried

    def test_urgency_beats_retry_count(self) -> None:
        """Soonest finalization wins even if the urgent market has more retries.

        Priority and urgency must take precedence over fair rotation: a
        challenge with a near-finalization deadline must not lose its slot to
        a fresher challenge with a later deadline.
        """
        questions_db = {
            "0xUrgentRetried": {
                "on_chain_answer": ANSWER_NO,
                "last_answer_timestamp": str(NOW - 80000),
                "realitio_timeout": 86400,
                "mech_retries": 5,
            },
            "0xLaterFresh": {
                "on_chain_answer": ANSWER_NO,
                "last_answer_timestamp": str(NOW - 10000),
                "realitio_timeout": 86400,
                "mech_retries": 0,
            },
        }
        key_urgent = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xUrgentRetried"}
        )
        key_later = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xLaterFresh"}
        )
        assert key_urgent < key_later

    def test_challenge_beats_unanswered_regardless_of_retries(self) -> None:
        """A challenge always sorts before any unanswered market.

        Priority is the first dimension of the sort key, so even an
        unanswered market with zero retries cannot jump ahead of a
        challenge that has already been retried multiple times.
        """
        questions_db = {
            "0xChallengeRetried": {
                "on_chain_answer": ANSWER_NO,
                "last_answer_timestamp": str(NOW - 10000),
                "realitio_timeout": 86400,
                "mech_retries": 5,
            },
            "0xUnansweredFresh": {
                "on_chain_answer": None,
                "last_answer_timestamp": None,
                "realitio_timeout": 86400,
                "mech_retries": 0,
            },
        }
        key_challenge = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xChallengeRetried"}
        )
        key_unanswered = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xUnansweredFresh"}
        )
        assert key_challenge < key_unanswered

    def test_none_mech_retries_treated_as_zero(self) -> None:
        """A ``None`` value for ``mech_retries`` must not raise.

        The sort key uses ``int(... or 0)`` so an explicitly-null counter
        (e.g. from a future JSON snapshot or a stale entry) is coerced to
        ``0`` rather than crashing ``actionable.sort()`` with a
        ``TypeError`` and stalling the FSM round.
        """
        questions_db = {
            "0xNull": {
                "on_chain_answer": None,
                "last_answer_timestamp": None,
                "realitio_timeout": 86400,
                "mech_retries": None,
            },
        }
        key = ScanMarketsBehaviour._actionable_sort_key(
            questions_db, {"market_id": "0xNull"}
        )
        assert key == (1, float("inf"), 0)


class TestLogScanSummary:
    """Tests for _log_scan_summary."""

    def test_logs_counts(self) -> None:
        """_log_scan_summary calls logger.info at least once."""
        b = _make_behaviour()
        questions_db = {
            "0xM1": {
                "status": AnswerStatus.TRUSTED_ANSWER,
                "on_chain_answer": ANSWER_YES,
                "on_chain_bond": "1",
                "last_answerer": "0x1",
            },
            "0xM2": {
                "status": AnswerStatus.NEEDS_ANSWER,
                "on_chain_answer": None,
                "on_chain_bond": "0",
                "last_answerer": "",
            },
        }
        b._log_scan_summary(questions_db)
        assert b.context.logger.info.called

    def test_non_trusted_markets_logged(self) -> None:
        """Non-trusted markets appear in per-market log lines."""
        b = _make_behaviour()
        questions_db = {
            "0xVerified": {
                "status": AnswerStatus.VERIFIED,
                "on_chain_answer": ANSWER_YES,
                "on_chain_bond": "1",
                "last_answerer": "0xSafe",
            },
            "0xNeedsAnswer": {
                "status": AnswerStatus.NEEDS_ANSWER,
                "on_chain_answer": None,
                "on_chain_bond": None,
                "last_answerer": "",
            },
        }
        b._log_scan_summary(questions_db)
        logged_calls = [str(c) for c in b.context.logger.info.call_args_list]
        assert any("0xNeedsAnswer" in s for s in logged_calls)


class TestAsyncActIntegration:
    """Integration-style tests for ScanMarketsBehaviour.async_act."""

    def _stub_fetch(
        self,
        behaviour: ScanMarketsBehaviour,
        markets: Optional[List] = None,
        fail_fetch: bool = False,
    ) -> None:
        """Stub _fetch_pending_and_finalizing_markets and _fetch_current_answerers."""
        if fail_fetch:
            patch.object(
                behaviour,
                "_fetch_pending_and_finalizing_markets",
                new=_make_gen(None),
            ).start()
        else:
            patch.object(
                behaviour,
                "_fetch_pending_and_finalizing_markets",
                new=_make_gen(markets or []),
            ).start()
        patch.object(behaviour, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(
            behaviour, "get_native_balance", new=_make_gen(10 * 10**18)
        ).start()
        patch.object(
            behaviour, "fetch_mech_requests_for_market", new=_make_gen([])
        ).start()

    def test_subgraph_error_sends_none_payload(self) -> None:
        """Subgraph error -> _send_none_payload."""
        b = _make_behaviour()
        self._stub_fetch(b, fail_fetch=True)

        mock_send = MagicMock(side_effect=_make_gen(None))
        with (
            patch.object(b, "send_a2a_transaction", mock_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert mock_send.called

    def test_no_markets_sends_payload_with_none_id(self) -> None:
        """No actionable markets -> _send_payload with None market ID."""
        b = _make_behaviour()
        self._stub_fetch(b, markets=[])

        mock_send = MagicMock(side_effect=_make_gen(None))
        with (
            patch.object(b, "send_a2a_transaction", mock_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert mock_send.called

    def test_market_with_no_answer_classified_needs_answer(self) -> None:
        """Unanswered market gets NEEDS_ANSWER status and is selected."""
        market = _make_market("0xM1", current_answer=None)
        b = _make_behaviour()
        self._stub_fetch(b, markets=[market])

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # The DB should have the market classified
        assert "0xM1" in b.questions_db
        assert b.questions_db["0xM1"]["status"] == AnswerStatus.NEEDS_ANSWER

    def test_trusted_answerer_classified_trusted(self) -> None:
        """Market answered by trusted address -> TRUSTED_ANSWER status."""
        trusted = "0xTrusted0000000000000000000000000000000001"
        market = _make_market(
            "0xM2",
            current_answer=ANSWER_YES,
            current_answer_bond="1000000000000000000",
        )
        b = _make_behaviour(synced_data=_make_synced_data(safe_address=SAFE_ADDRESS))
        b.context.params.trusted_addresses = [trusted]
        # _fetch_current_answerers will return trusted as answerer
        patch.object(
            b,
            "_fetch_pending_and_finalizing_markets",
            new=_make_gen([market]),
        ).start()
        patch.object(
            b,
            "_fetch_current_answerers",
            new=_make_gen({"0xQuestion1": trusted.lower()}),
        ).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert b.questions_db["0xM2"]["status"] == AnswerStatus.TRUSTED_ANSWER

    def test_safe_answerer_classified_trusted(self) -> None:
        """Market answered by safe address -> TRUSTED_ANSWER status."""
        market = _make_market(
            "0xM3",
            current_answer=ANSWER_YES,
            current_answer_bond="1000000000000000000",
        )
        b = _make_behaviour()
        patch.object(
            b,
            "_fetch_pending_and_finalizing_markets",
            new=_make_gen([market]),
        ).start()
        patch.object(
            b,
            "_fetch_current_answerers",
            new=_make_gen({"0xQuestion1": SAFE_ADDRESS.lower()}),
        ).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert b.questions_db["0xM3"]["status"] == AnswerStatus.TRUSTED_ANSWER

    def test_mech_agrees_with_on_chain_classified_verified(self) -> None:
        """Market with matching Mech + on-chain answer -> VERIFIED."""
        market = _make_market(
            "0xM4",
            current_answer=ANSWER_YES,
            current_answer_bond="1000000000000000000",
        )
        existing_db = {
            "0xM4": {
                "status": None,
                "market_id": "0xM4",
                "title": "Will X?",
                "question_id": "0xQuestion1",
                "detected_at": NOW,
                "on_chain_answer": ANSWER_YES,
                "on_chain_bond": "1000000000000000000",
                "last_answerer": "0xOther",
                "last_answer_timestamp": str(NOW - 100),
                "market_closing_timestamp": 1_699_000_000,
                "realitio_timeout": 86400,
                "mech_request": None,
                "mech_response": None,
                "evaluation": {"answer": ANSWER_YES, "is_valid": True},
                "pending_tx": None,
                "mech_retries": 0,
            }
        }
        b = _make_behaviour(questions_db=existing_db)
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(
            b,
            "_fetch_current_answerers",
            new=_make_gen({"0xQuestion1": "0xother"}),
        ).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert b.questions_db["0xM4"]["status"] == AnswerStatus.VERIFIED

    def test_mech_disagrees_with_on_chain_classified_tx_pending(self) -> None:
        """Mech disagrees with on-chain answer -> TRANSACTION_PENDING."""
        market = _make_market(
            "0xM5",
            current_answer=ANSWER_NO,
            current_answer_bond="1000000000000000000",
            current_answer_timestamp=str(NOW - 1000),
        )
        existing_db = {
            "0xM5": {
                "status": None,
                "market_id": "0xM5",
                "title": "Will X?",
                "question_id": "0xQuestion1",
                "detected_at": NOW,
                "on_chain_answer": ANSWER_NO,
                "on_chain_bond": "1000000000000000000",
                "last_answerer": "0xOther",
                "last_answer_timestamp": str(NOW - 1000),
                "market_closing_timestamp": 1_699_000_000,
                "realitio_timeout": 86400,
                "mech_request": None,
                "mech_response": None,
                "evaluation": {"answer": ANSWER_YES, "is_valid": True},  # disagrees
                "pending_tx": None,
                "mech_retries": 0,
            }
        }
        b = _make_behaviour(questions_db=existing_db)
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert b.questions_db["0xM5"]["status"] == AnswerStatus.TRANSACTION_PENDING

    def test_no_evaluation_classified_needs_verification(self) -> None:
        """Market with on-chain answer but no Mech evaluation -> NEEDS_VERIFICATION."""
        market = _make_market(
            "0xM6",
            current_answer=ANSWER_NO,
            current_answer_bond="1000000000000000000",
        )
        b = _make_behaviour()
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert b.questions_db["0xM6"]["status"] == AnswerStatus.NEEDS_VERIFICATION

    def test_stale_undeterminable_evaluation_cleared_on_needs_answer(self) -> None:
        """Regression for stale undeterminable evaluation persisting.

        Stale undeterminable evaluations must be cleared even when the
        status would be NEEDS_ANSWER (no on-chain answer).

        Production bug: when a Mech response came back undeterminable
        (``answer=None``), ``build_answer_tx`` stored the evaluation on the
        entry and set ``retry_after = now + 24h``. The classification block
        in scan_markets only cleared stale undeterminable evaluations in
        the ``NEEDS_VERIFICATION`` (else) branch -- not in the
        ``NEEDS_ANSWER`` branch (taken when nobody has answered on-chain).

        Result: after the 24h cooldown, the market would be selected
        again, but ``evaluate_answers`` would see the cached undeterminable
        evaluation, log "reusing existing Mech evaluation, skipping Mech
        request", and route to ``build_answer_tx``, which just re-set
        ``retry_after`` for another 24h. The on-chain Mech request counter
        never advanced past 1; ``max_mech_retries=10`` was unreachable;
        markets piled up in zombie state forever.

        This test pins the fix: a market with no on-chain answer but a
        stale undeterminable cached evaluation must have that evaluation
        cleared during classification so the next selection cycle fires
        a fresh Mech request.
        """
        # Pre-existing entry: no on-chain answer, but a stale undeterminable
        # evaluation cached from a previous cycle.
        market_id = "0xStuck"
        existing_db = {
            market_id: {
                "status": AnswerStatus.NEEDS_ANSWER,
                "market_id": market_id,
                "title": "Will X happen?",
                "question_id": "0xQStuck",
                "detected_at": NOW - 100000,
                "on_chain_answer": None,
                "on_chain_bond": None,
                "last_answerer": "",
                "last_answer_timestamp": None,
                "market_closing_timestamp": NOW + 100000,
                "realitio_timeout": 86400,
                "mech_requests": [],
                "pending_nonce": None,
                "evaluation": {
                    "answer": None,  # <-- stale undeterminable
                    "is_valid": True,
                    "is_determinable": False,
                    "has_occurred": None,
                    "agreement_ratio": 0.5,
                    "agrees_with_on_chain": None,
                    "reasoning": "stale undeterminable",
                },
                "pending_tx": None,
                "mech_retries": 1,
                "retry_after": NOW - 10,  # cooldown already expired
            }
        }
        # Subgraph returns the same market still pending
        market = _make_market(market_id)
        b = _make_behaviour(questions_db=existing_db)
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        # fetch_mech_requests_for_market returns [] (not None), so the
        # ``if requests is None: continue`` skip path is bypassed and the
        # code reaches ``max(local, len(subgraph)) = max(1, 0) = 1`` --
        # the monotonic guard preserves the prior ``mech_retries`` value.
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # Status is NEEDS_ANSWER (no on-chain answer) AND evaluation has
        # been cleared so evaluate_answers will fire a fresh Mech request
        # rather than reusing the stale undeterminable.
        assert b.questions_db[market_id]["status"] == AnswerStatus.NEEDS_ANSWER
        assert b.questions_db[market_id]["evaluation"] is None

    def test_stale_market_purged_from_db(self) -> None:
        """Market no longer in subgraph results is removed from DB."""
        # Pre-existing stale entry
        existing_db = {
            "0xStale": {
                "status": AnswerStatus.NEEDS_ANSWER,
                "market_id": "0xStale",
                "title": "Stale?",
                "question_id": "0xQStale",
                "detected_at": NOW - 100000,
                "on_chain_answer": None,
                "on_chain_bond": None,
                "last_answerer": "",
                "last_answer_timestamp": None,
                "market_closing_timestamp": 1_698_000_000,
                "realitio_timeout": 86400,
                "mech_request": None,
                "mech_response": None,
                "evaluation": None,
                "pending_tx": None,
                "mech_retries": 0,
            }
        }
        # Subgraph returns a different market (not the stale one)
        market = _make_market("0xFresh")
        b = _make_behaviour(questions_db=existing_db)
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert "0xStale" not in b.questions_db
        assert "0xFresh" in b.questions_db

    def test_cache_rehydration_on_hit(self) -> None:
        """Valid Mech delivery from the subgraph populates ``entry['evaluation']``."""
        market = _make_market("0xM7", current_answer=ANSWER_YES)
        # The subgraph returns one request with a valid YES delivery.
        # scan_markets._earliest_valid_evaluation derives the evaluation
        # from the raw request list.
        request_entry = {
            "id": "req-cache-hit",
            "blockTimestamp": "1700001000",
            "parsedRequest": {
                "tool": "resolve-market-jury-v1",
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
        b = _make_behaviour()
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(
            b, "fetch_mech_requests_for_market", new=_make_gen([request_entry])
        ).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        entry = b.questions_db.get("0xM7")
        assert entry is not None
        assert entry["evaluation"] is not None

    def test_late_mech_delivery_picked_up_on_rescan(self) -> None:
        """A subgraph miss on one scan must not block a hit on the next.

        Regression for the bug where ``cache_checked`` was set on first
        miss and never cleared, so Mech responses that arrive after our
        in-process ``mech_response_round`` timed out were never
        rehydrated from the subgraph.
        """
        market = _make_market("0xM9")
        b = _make_behaviour()
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()

        # First scan: subgraph miss (empty list). Second scan: subgraph hit
        # with a valid YES delivery.
        late_request = {
            "id": "req-late",
            "blockTimestamp": "1700001000",
            "parsedRequest": {
                "tool": "resolve-market-jury-v1",
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
        call_results: List[Any] = [[], [late_request]]

        def cache_lookup(*args: Any, **kwargs: Any) -> Any:
            return call_results.pop(0)
            yield  # noqa

        patch.object(b, "fetch_mech_requests_for_market", new=cache_lookup).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)  # first scan -- miss
            _run_async_act(b)  # second scan -- must re-query and hit

        entry = b.questions_db.get("0xM9")
        assert entry is not None
        assert entry["evaluation"] is not None
        assert entry["evaluation"]["answer"] == ANSWER_YES
        # Both scans consulted the subgraph -- the second one is what
        # would have been skipped under the old cache_checked logic.
        assert call_results == []

    def test_cache_lookup_returns_none_skipped(self) -> None:
        """Subgraph error (cached=None) leaves entry untouched, no crash."""
        market = _make_market("0xMNone")
        b = _make_behaviour()
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen(None)).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        entry = b.questions_db.get("0xMNone")
        assert entry is not None
        assert entry["evaluation"] is None
        assert entry["mech_retries"] == 0

    def test_max_mech_retries_skipped(self) -> None:
        """Market with mech_retries >= max_mech_retries is not selected."""
        market = _make_market("0xMaxed")
        b = _make_behaviour()
        b.context.params.max_mech_retries = 5
        # Subgraph returns 5 prior Mech requests -> seeds mech_retries=5
        # via ``max(mech_retries, len(mech_requests))`` in scan_markets.
        # Deliveries are empty so no evaluation is derived; the retry-gate
        # is what we exercise here.
        prior_requests = [
            {
                "id": f"req-prior-{i}",
                "blockTimestamp": str(1700000000 + i),
                "parsedRequest": {
                    "tool": "resolve-market-jury-v1",
                    "prompt": "Will X happen?",
                },
                "deliveries": [],
            }
            for i in range(5)
        ]
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(
            b, "fetch_mech_requests_for_market", new=_make_gen(prior_requests)
        ).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # Market exists in DB but is not selected (max retries gate).
        assert b.questions_db.get("0xMaxed") is not None
        assert b.questions_db["0xMaxed"]["mech_retries"] == 5
        assert payload_sent[0].selected_market_id is None

    @pytest.mark.parametrize("subgraph_returns", [None, []])
    def test_mech_retries_monotonic_when_subgraph_empty(
        self, subgraph_returns: Any
    ) -> None:
        """Regression on retry-counter monotonicity.

        ``mech_retries`` must NOT collapse to 0 when the subgraph helper
        returns ``None`` or ``[]``. The original retry-budget bug shipped
        because ``mech_retries = max(prior, len(requests))`` silently
        degraded to 0 when the broken cache query returned an empty list
        (the ``nonce`` field error masquerading as zero results). The fix
        preserves the prior counter -- this test pins that contract.

        Seeds the DB with ``mech_retries=3``, makes the subgraph helper
        return ``None`` / ``[]``, then asserts the counter is unchanged
        after a full scan.

        :param subgraph_returns: the value ``fetch_mech_requests_for_market``
            is mocked to return (``None`` or empty list).
        """
        market_id = "0xMonotonic"
        market = _make_market(market_id)
        seeded_db: Dict[str, Any] = {
            market_id: {
                "status": None,
                "market_id": market_id,
                "title": "Will X happen?",
                "question_id": "0xQ",
                "detected_at": NOW - 1000,
                "on_chain_answer": None,
                "on_chain_bond": None,
                "last_answerer": "",
                "last_answer_timestamp": None,
                "market_closing_timestamp": NOW - 500,
                "realitio_timeout": 86400,
                "mech_requests": [],
                "pending_nonce": None,
                "evaluation": None,
                "pending_tx": None,
                "mech_retries": 3,
            }
        }
        b = _make_behaviour(questions_db=seeded_db)
        b.context.params.max_mech_retries = 10
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(
            b,
            "fetch_mech_requests_for_market",
            new=_make_gen(subgraph_returns),
        ).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # Counter must be preserved (max of prior=3 and the empty/None
        # subgraph result). A regression to the buggy behaviour would
        # collapse this to 0 and let the market be re-selected indefinitely.
        assert b.questions_db[market_id]["mech_retries"] == 3

    def test_none_mech_retries_normalizes_to_zero_on_empty_subgraph(self) -> None:
        """``None`` mech_retries normalizes to ``0`` via the line-204 max() expression."""
        market_id = "0xNoneInit"
        market = _make_market(market_id)
        seeded_db: Dict[str, Any] = {
            market_id: {
                "status": None,
                "market_id": market_id,
                "title": "Will X happen?",
                "question_id": "0xQ",
                "detected_at": NOW - 1000,
                "on_chain_answer": None,
                "on_chain_bond": None,
                "last_answerer": "",
                "last_answer_timestamp": None,
                "market_closing_timestamp": NOW - 500,
                "realitio_timeout": 86400,
                "mech_requests": [],
                "pending_nonce": None,
                "evaluation": None,
                "pending_tx": None,
                "mech_retries": None,
            }
        }
        b = _make_behaviour(questions_db=seeded_db)
        b.context.params.max_mech_retries = 10
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        # Returning ``[]`` (not ``None``) reaches the line-204 ``max()``
        # expression -- exercising the sibling path to the None-skip case.
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # ``max(int(None or 0), len([])) == max(0, 0) == 0``. A regression
        # to ``int(entry.get("mech_retries", 0))`` would raise TypeError on
        # ``int(None)`` before reaching the assertion.
        assert b.questions_db[market_id]["mech_retries"] == 0

    def test_retry_cap_gate_survives_none_mech_retries(self) -> None:
        """Retry-cap gate treats ``None`` mech_retries as ``0`` and selects the market."""
        market_id = "0xNoneRetries"
        market = _make_market(market_id)
        seeded_db: Dict[str, Any] = {
            market_id: {
                "status": None,
                "market_id": market_id,
                "title": "Will X happen?",
                "question_id": "0xQ",
                "detected_at": NOW - 1000,
                "on_chain_answer": None,
                "on_chain_bond": None,
                "last_answerer": "",
                "last_answer_timestamp": None,
                "market_closing_timestamp": NOW - 500,
                "realitio_timeout": 86400,
                "mech_requests": [],
                "pending_nonce": None,
                "evaluation": None,
                "pending_tx": None,
                "mech_retries": None,
            }
        }
        b = _make_behaviour(questions_db=seeded_db)
        b.context.params.max_mech_retries = 10
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        # Returning ``None`` here triggers the ``continue`` skip on
        # line 196, leaving ``mech_retries`` un-normalized so the
        # downstream gate sees the seeded ``None``.
        patch.object(
            b,
            "fetch_mech_requests_for_market",
            new=_make_gen(None),
        ).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # The scan completed without a TypeError; the market is treated
        # as having ``mech_retries=0`` and is therefore selectable
        # (below the cap).
        assert len(payload_sent) == 1
        assert payload_sent[0].selected_market_id == market_id

    def test_bond_too_high_market_skipped(self) -> None:
        """Market with bond exceeding max_challenge_bond is skipped when prefetch=False."""
        # Bond is 32 xDAI, max is 16 xDAI
        huge_bond = str(32 * 10**18)
        market = _make_market(
            "0xMBig",
            current_answer=ANSWER_NO,
            current_answer_bond=huge_bond,
            current_answer_timestamp=str(NOW - 1000),
        )
        b = _make_behaviour()
        b.context.params.max_challenge_bond = 16 * 10**18
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        # The sent payload should have no selected_market_id
        assert len(payload_sent) == 1
        assert payload_sent[0].selected_market_id is None

    def test_safe_balance_too_low_market_skipped(self) -> None:
        """Market skipped when safe balance < required bond."""
        market = _make_market("0xMLowBal", current_answer=None)
        b = _make_behaviour()
        b.context.params.initial_answer_bond = 10**18
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        # Balance is 0 -- can't afford the initial bond
        patch.object(b, "get_native_balance", new=_make_gen(0)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert len(payload_sent) == 1
        assert payload_sent[0].selected_market_id is None

    def test_safe_balance_none_treated_as_zero(self) -> None:
        """When get_native_balance returns None, balance treated as 0."""
        market = _make_market("0xMNoBal", current_answer=None)
        b = _make_behaviour()
        b.context.params.initial_answer_bond = 10**18
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(None)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].selected_market_id is None

    def test_retry_after_in_future_skips_market(self) -> None:
        """Market with retry_after > now is skipped."""
        market = _make_market("0xMRetry", current_answer=None)
        existing_db = {
            "0xMRetry": {
                "status": None,
                "market_id": "0xMRetry",
                "title": "?",
                "question_id": "0xQ",
                "detected_at": NOW,
                "on_chain_answer": None,
                "on_chain_bond": None,
                "last_answerer": "",
                "last_answer_timestamp": None,
                "market_closing_timestamp": 1_699_000_000,
                "realitio_timeout": 86400,
                "mech_request": None,
                "mech_response": None,
                "evaluation": None,
                "pending_tx": None,
                "mech_retries": 1,
                "retry_after": NOW + 3600,  # in the future
            }
        }
        b = _make_behaviour(questions_db=existing_db)
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].selected_market_id is None

    def test_already_finalized_tx_pending_market_skipped(self) -> None:
        """TRANSACTION_PENDING market past finalization deadline is skipped."""
        # finalization = last_answer_ts + timeout = (NOW - 90000) + 86400 = NOW - 3600 (past)
        market = _make_market(
            "0xMPast",
            current_answer=ANSWER_NO,
            current_answer_bond="1000000000000000000",
            current_answer_timestamp=str(NOW - 90000),
        )
        existing_db = {
            "0xMPast": {
                "status": None,
                "market_id": "0xMPast",
                "title": "?",
                "question_id": "0xQ",
                "detected_at": NOW,
                "on_chain_answer": ANSWER_NO,
                "on_chain_bond": "1000000000000000000",
                "last_answerer": "0xOther",
                "last_answer_timestamp": str(NOW - 90000),
                "market_closing_timestamp": 1_699_000_000,
                "realitio_timeout": 86400,
                "mech_request": None,
                "mech_response": None,
                "evaluation": {"answer": ANSWER_YES},  # disagrees -> TX_PENDING
                "pending_tx": None,
                "mech_retries": 0,
            }
        }
        b = _make_behaviour(questions_db=existing_db)
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].selected_market_id is None

    def test_tx_pending_challenge_cooldown_skips_market(self) -> None:
        """TRANSACTION_PENDING market under cooldown and no urgency is skipped."""
        # escalation_count > 0 triggers cooldown check
        last_ts = NOW - 1000  # recent challenge
        market = _make_market(
            "0xMCool",
            current_answer=ANSWER_NO,
            current_answer_bond="1000000000000000000",
            current_answer_timestamp=str(last_ts),
        )
        existing_db = {
            "0xMCool": {
                "status": None,
                "market_id": "0xMCool",
                "title": "?",
                "question_id": "0xQ",
                "detected_at": NOW,
                "on_chain_answer": ANSWER_NO,
                "on_chain_bond": "1000000000000000000",
                "last_answerer": "0xOther",
                "last_answer_timestamp": str(last_ts),
                "market_closing_timestamp": 1_699_000_000,
                "realitio_timeout": 86400,
                "mech_request": None,
                "mech_response": None,
                "evaluation": {"answer": ANSWER_YES},  # disagrees
                "pending_tx": {
                    "escalation_count": 1,
                    "timestamp": NOW - 1000,  # just challenged
                },
                "mech_retries": 0,
            }
        }
        b = _make_behaviour(questions_db=existing_db)
        b.context.params.challenge_cooldown_fraction = 0.25
        b.context.params.challenge_urgency_buffer = 3600
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].selected_market_id is None

    def test_tx_pending_urgency_overrides_cooldown(self) -> None:
        """TRANSACTION_PENDING market within urgency window is selected even under cooldown."""
        # finalization is 1h away (< urgency_buffer=3600) -> urgent
        timeout = 86400
        last_ts = NOW - timeout + 3599  # finalization ~ NOW + 1 second
        market = _make_market(
            "0xMUrgent",
            current_answer=ANSWER_NO,
            current_answer_bond="1000000000000000000",
            current_answer_timestamp=str(last_ts),
        )
        existing_db = {
            "0xMUrgent": {
                "status": None,
                "market_id": "0xMUrgent",
                "title": "?",
                "question_id": "0xQ",
                "detected_at": NOW,
                "on_chain_answer": ANSWER_NO,
                "on_chain_bond": "1000000000000000000",
                "last_answerer": "0xOther",
                "last_answer_timestamp": str(last_ts),
                "market_closing_timestamp": 1_699_000_000,
                "realitio_timeout": timeout,
                "mech_request": None,
                "mech_response": None,
                "evaluation": {"answer": ANSWER_YES},
                "pending_tx": {
                    "escalation_count": 1,
                    "timestamp": NOW - 100,  # very recent
                },
                "mech_retries": 0,
            }
        }
        b = _make_behaviour(questions_db=existing_db)
        b.context.params.challenge_cooldown_fraction = 0.25
        b.context.params.challenge_urgency_buffer = 3600
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert payload_sent[0].selected_market_id == "0xMUrgent"

    def test_existing_market_upserted(self) -> None:
        """Pre-existing DB entry has on-chain fields updated in upsert step."""
        market = _make_market(
            "0xExisting",
            current_answer=ANSWER_YES,
            current_answer_bond="2000000000000000000",
            current_answer_timestamp=str(NOW - 200),
        )
        existing_db = {
            "0xExisting": {
                "status": None,
                "market_id": "0xExisting",
                "title": "?",
                "question_id": "0xQuestion1",
                "detected_at": NOW - 10000,
                "on_chain_answer": None,  # old stale value
                "on_chain_bond": None,
                "last_answerer": "",
                "last_answer_timestamp": None,
                "market_closing_timestamp": 1_699_000_000,
                "realitio_timeout": 86400,
                "mech_request": None,
                "mech_response": None,
                "evaluation": None,
                "pending_tx": None,
                "mech_retries": 0,
            }
        }
        b = _make_behaviour(questions_db=existing_db)
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        with (
            patch.object(b, "send_a2a_transaction", new=_make_gen(None)),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        entry = b.questions_db["0xExisting"]
        assert entry["on_chain_answer"] == ANSWER_YES
        assert entry["on_chain_bond"] == "2000000000000000000"

    def test_selected_market_sent_in_payload(self) -> None:
        """The highest-priority actionable market ID ends up in the payload."""
        market = _make_market("0xSelected", current_answer=None)
        b = _make_behaviour()
        patch.object(
            b, "_fetch_pending_and_finalizing_markets", new=_make_gen([market])
        ).start()
        patch.object(b, "_fetch_current_answerers", new=_make_gen({})).start()
        patch.object(b, "get_native_balance", new=_make_gen(10 * 10**18)).start()
        patch.object(b, "fetch_mech_requests_for_market", new=_make_gen([])).start()

        payload_sent = []

        def capture_send(payload: Any) -> Any:
            payload_sent.append(payload)
            return None
            yield  # noqa

        with (
            patch.object(b, "send_a2a_transaction", new=capture_send),
            patch.object(b, "wait_until_round_end", new=_make_gen(None)),
            patch.object(b, "set_done"),
        ):
            _run_async_act(b)

        assert len(payload_sent) == 1
        assert payload_sent[0].selected_market_id == "0xSelected"
