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

"""This module contains the base behaviour for the market resolution manager."""

import json
import time
from abc import ABC
from typing import Any, Dict, Generator, List, Optional, Tuple, cast

from packages.valory.connections.kv_store.connection import (
    PUBLIC_ID as KV_STORE_CONNECTION_PUBLIC_ID,
)
from packages.valory.protocols.kv_store.message import KvStoreMessage
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.behaviours import BaseBehaviour
from packages.valory.skills.market_resolution_manager_abci import mech_cache
from packages.valory.skills.market_resolution_manager_abci.models import (
    MarketResolutionManagerParams,
    SharedState,
)
from packages.valory.skills.market_resolution_manager_abci.rounds import (
    SynchronizedData,
)

HTTP_OK = 200

# Realitio answer encoding (outcome index for binary Yes/No markets).
ANSWER_YES = "0x0000000000000000000000000000000000000000000000000000000000000000"
ANSWER_NO = "0x0000000000000000000000000000000000000000000000000000000000000001"
ANSWER_INVALID = "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

# GraphQL query template: prior Mech requests from our Safe for a given
# prompt, scoped to blocks after the market closed (openingTimestamp).
# Variables are inlined because the base helper only posts the `query` field.
#
# Used only for the one-time lazy seeding of the kv_store cache: when a
# (safe, market) pair has no seed marker yet, the historical on-chain
# requests are pulled from the subgraph and copied into the kv cache so
# in-flight markets keep their retry budget across the migration. Live
# reads come from the kv cache, not from this query.
#
# NOTE: the Mech Marketplace Gnosis subgraph stores the market question in
# ``parsedRequest.prompt``, NOT in ``parsedRequest.questionTitle`` (which is
# always empty for our requests). We filter on ``prompt`` accordingly.
#
# ``parsedRequest`` has no ``nonce`` field on this subgraph. Selecting it
# makes the whole query error with ``data: null`` -- a previous version of
# this template did exactly that, which silently disabled the retry-budget
# gate (the extractor walked ``data.sender.requests`` against ``null`` and
# got ``[]``, so ``len(requests) == 0`` every cycle no matter how many
# requests had really been fired). Keep the projection to ``prompt`` and
# ``tool`` only.
#
# Top-level ``requests`` (with ``sender:`` in the ``where`` clause) is
# equivalent to ``sender(id) { requests(...) }`` on this subgraph -- both
# return the same rows -- but the top-level form matches the pattern used
# by watchdog and trader, and is what the framework's ``response_key:
# data:requests`` walks.
MECH_CACHE_QUERY_TEMPLATE = """
{{
  requests(
    where: {{
      sender: "{sender}"
      parsedRequest_: {{ prompt: {prompt} }}
      blockTimestamp_gt: "{block_timestamp_gt}"
    }}
    orderBy: blockTimestamp
    orderDirection: asc
    first: 1000
  ) {{
    id
    blockTimestamp
    parsedRequest {{
      prompt
      tool
    }}
    deliveries(orderBy: blockTimestamp, orderDirection: asc) {{
      id
      blockTimestamp
      toolResponse
    }}
  }}
}}
"""


def parse_mech_response(  # pylint: disable=too-many-return-statements
    result: Optional[str],
) -> Optional[dict]:
    """Parse a resolve-market-jury-v1 Mech result with strict pattern matching.

    Only four exact ``(is_valid, is_determinable, has_occurred)`` patterns
    are recognised:

    - **Case A** ``(False, None, None)`` -> market is invalid, answer=INVALID
    - **Case B** ``(True, False, None)`` -> undeterminable, answer=None (retry)
    - **Case C1** ``(True, True, True)`` -> answer=YES
    - **Case C2** ``(True, True, False)`` -> answer=NO

    Any other combination is garbage (e.g. API errors returning
    ``(False, False, None)``) and returns ``None`` so callers retry.

    :param result: raw Mech response payload (JSON string) or ``None``.
    :return: parsed answer dict on a recognised pattern, else ``None``.
    """
    if result is None:
        return None
    try:
        data = json.loads(result)
        if not isinstance(data, dict):
            return None
        is_valid = data.get("is_valid")
        is_determinable = data.get("is_determinable")
        has_occurred = data.get("has_occurred")
        agreement_ratio = float(data.get("agreement_ratio") or 0.0)
        reasoning = data.get("judge_reasoning", "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return None

    def _make(answer: Optional[str]) -> dict:
        return {
            "answer": answer,
            "has_occurred": has_occurred,
            "is_valid": is_valid,
            "is_determinable": is_determinable,
            "agreement_ratio": agreement_ratio,
            "agrees_with_on_chain": None,
            "reasoning": reasoning,
        }

    # Case A: question is invalid -> submit INVALID answer
    if is_valid is False and is_determinable is None and has_occurred is None:
        return _make(ANSWER_INVALID)

    # Cases B, C1, C2 require is_valid=True
    if is_valid is not True:
        return None  # garbage

    # Case B: undeterminable -> retry later
    if is_determinable is False:
        return _make(None)

    # Case C1 / C2: determined
    if is_determinable is True and has_occurred is True:
        return _make(ANSWER_YES)
    if is_determinable is True and has_occurred is False:
        return _make(ANSWER_NO)

    # Anything else (e.g. is_determinable=True but has_occurred=None)
    return None


def jury_error_discriminator(result: Optional[str]) -> Optional[str]:
    """Return the jury's ``error`` discriminator if the payload reports one.

    The resolve-market-jury-v1 Mech tool emits a top-level ``error`` field
    on its off-contract / failure paths (``all_voters_failed``,
    ``judge_unparseable``, ``malformed_verdict``) alongside the
    ``(None, None, None)`` verdict tuple. ``parse_mech_response`` correctly
    routes these to the garbage path, but the discriminator itself is
    operationally valuable -- it lets the operator distinguish an API
    outage from a genuine parser failure. Returns ``None`` for non-JSON
    payloads, JSON without an ``error`` field, or non-dict JSON.

    :param result: raw Mech response payload (JSON string) or ``None``.
    :return: the ``error`` string if present, else ``None``.
    """
    if result is None:
        return None
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, str) and err:
        return err
    return None


def is_cached_evaluation_valid(evaluation: Optional[dict]) -> bool:
    """Return True if a cached evaluation has a definitive answer.

    Cases A (INVALID), C1 (YES), C2 (NO) have answer set and are cacheable.
    Case B (undeterminable, answer=None) and garbage (None) are not.

    :param evaluation: cached parse result from ``parse_mech_response``.
    :return: True if the evaluation carries a definitive answer.
    """
    if evaluation is None:
        return False
    return evaluation.get("answer") is not None


def to_content(query: str) -> bytes:
    """Convert the given query string to payload content."""
    finalized_query = {"query": query}
    encoded_query = json.dumps(finalized_query, sort_keys=True).encode("utf-8")
    return encoded_query


class MarketResolutionManagerBaseBehaviour(BaseBehaviour, ABC):
    """Base behaviour for the market resolution manager skill."""

    @property
    def synchronized_data(self) -> SynchronizedData:
        """Return the synchronized data."""
        return cast(SynchronizedData, super().synchronized_data)

    @property
    def params(self) -> MarketResolutionManagerParams:
        """Return the params."""
        return cast(MarketResolutionManagerParams, super().params)

    @property
    def questions_db(self) -> Dict[str, Any]:
        """Get the questions database from shared state."""
        return self.context.state.questions_db

    @questions_db.setter
    def questions_db(self, value: Dict[str, Any]) -> None:
        """Set the questions database on shared state."""
        self.context.state.questions_db = value

    @property
    def last_synced_timestamp(self) -> int:
        """Get last synced timestamp."""
        state = cast(SharedState, self.context.state)
        last_timestamp = (
            state.round_sequence.last_round_transition_timestamp.timestamp()
        )
        return int(last_timestamp)

    def get_native_balance(self, address: str) -> Generator[None, None, Optional[int]]:
        """Get the native xDAI balance of the provided address."""
        ledger_api_response = yield from self.get_ledger_api_response(
            performative=LedgerApiMessage.Performative.GET_STATE,  # type: ignore
            ledger_callable="get_balance",
            account=address,
            chain_id=self.params.default_chain_id,
        )
        if ledger_api_response.performative != LedgerApiMessage.Performative.STATE:
            self.context.logger.error(
                f"Could not get balance for {address}. "
                f"Expected STATE, got {ledger_api_response.performative.value}."
            )
            return None
        balance = cast(int, ledger_api_response.state.body.get("get_balance_result"))
        return balance

    def get_omen_subgraph_result(
        self,
        query: str,
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Query the Omen subgraph.

        :param query: the GraphQL query string.
        :yield: None
        :return: the parsed JSON response, or None on error.
        """
        response = yield from self.get_http_response(
            content=to_content(query),
            **self.context.omen_subgraph.get_spec(),
        )

        if response is None:
            self.context.logger.error(
                "Could not retrieve response from Omen subgraph. Response was None."
            )
            return None
        if response.status_code != HTTP_OK:
            self.context.logger.error(
                f"Could not retrieve response from Omen subgraph. "
                f"Received status code {response.status_code}.\n{response}"
            )
            return None

        try:
            return json.loads(response.body.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.context.logger.error(
                f"Omen subgraph returned 200 with non-JSON body: {exc}"
            )
            return None

    def get_realitio_subgraph_result(
        self,
        query: str,
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Query the Realitio subgraph.

        :param query: the GraphQL query string.
        :yield: None
        :return: the parsed JSON response, or None on error.
        """
        response = yield from self.get_http_response(
            content=to_content(query),
            **self.context.realitio_subgraph.get_spec(),
        )

        if response is None:
            self.context.logger.error(
                "Could not retrieve response from Realitio subgraph. "
                "Response was None."
            )
            return None
        if response.status_code != HTTP_OK:
            self.context.logger.error(
                f"Could not retrieve response from Realitio subgraph. "
                f"Received status code {response.status_code}.\n{response}"
            )
            return None

        try:
            return json.loads(response.body.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.context.logger.error(
                f"Realitio subgraph returned 200 with non-JSON body: {exc}"
            )
            return None

    def get_mech_gnosis_subgraph_result(
        self,
        query: str,
    ) -> Generator[None, None, Optional[List[Dict[str, Any]]]]:
        """Query the Mech Marketplace Gnosis subgraph.

        Only used to seed the kv_store cache once per (safe, market) pair;
        see ``fetch_mech_requests_for_market``.

        :param query: the GraphQL query string.
        :yield: None
        :return: the parsed + key-walked response, or None on error.
        """
        subgraph = self.context.mech_gnosis_subgraph
        response = yield from self.get_http_response(
            content=to_content(query),
            **subgraph.get_spec(),
        )

        if response is None:
            self.context.logger.error(
                "Could not retrieve response from Mech Gnosis subgraph. "
                "Response was None."
            )
            return None
        if response.status_code != HTTP_OK:
            self.context.logger.error(
                f"Could not retrieve response from Mech Gnosis subgraph. "
                f"Received status code {response.status_code}.\n{response}"
            )
            return None

        return subgraph.process_response(response)

    def fetch_mech_requests_for_market(
        self, entry: Dict[str, Any]
    ) -> Generator[None, None, Optional[list]]:
        """Fetch prior Mech fires + deliveries for this market from kv_store.

        Under the on-chain path this queried the Mech Marketplace Gnosis
        subgraph directly. Under the off-chain path the subgraph's
        ``parsedRequest.prompt`` and ``deliveries[].toolResponse`` go empty
        (the mech no longer publishes to IPFS), so the subgraph stops
        answering "have I already asked this market?" for new requests.
        The kv_store cache (written by
        ``evaluate_answers._buffer_mech_request_fired`` and updated by
        ``build_answer_tx._buffer_mech_response_delivered``) replaces it.
        Same behaviour, both onchain and offchain: mech-interact internally
        decides which mode to use and hides that from us.

        The subgraph keeps one job: on the first read for a (safe, market)
        pair (no seed marker row yet), its historical on-chain rows are
        copied into the kv cache so an in-flight market keeps its retry
        budget across the migration instead of being re-fired as a fresh
        paid request. The marker is written only after every row upsert
        succeeded, so a crash mid-seed re-seeds on the next cycle (the
        upserts are idempotent: seeded keys are derived from immutable
        subgraph request ids).

        Returns the same list-of-dicts shape the previous subgraph result
        did, so ``_earliest_valid_evaluation`` and the ``mech_retries``
        counter in ``scan_markets`` need zero changes.

        :param entry: the per-market state dict.
        :yield: control to the FSM while kv_store and subgraph requests
            complete.
        :return: rehydrated list of rows for this (safe, market) pair, or
            ``None`` on kv_store or subgraph error.
        """
        market_id = entry.get("market_id") or ""
        safe_address = self.synchronized_data.safe_contract_address or ""

        seeded = yield from self._ensure_market_seeded(
            entry, safe_address, str(market_id)
        )
        if not seeded:
            return None

        prefix = mech_cache.list_prefix(
            prefix=self.params.mech_cache_key_prefix,
            safe_address=safe_address,
            market_id=str(market_id),
        )
        rows_by_key = yield from self._send_kv_list(prefix)
        if rows_by_key is None:
            return None

        expected_tool = self.params.mech_tool_resolve_market
        expected_prompt = entry.get("title") or ""
        rehydrated = mech_cache.rehydrate_to_subgraph_shape(rows_by_key)
        # Filter to rows whose stored (tool, prompt) match the entry we're
        # scanning against. Same filter the subgraph path applied. Under
        # normal operation every row for this (safe, market_id) prefix
        # was written by evaluate_answers with the same tool+prompt, so
        # the filter is a no-op; keeping it lets a future param change
        # (different mech_tool_resolve_market) not mis-count rows from
        # the old tool as "already asked".
        return [
            req
            for req in rehydrated
            if (req.get("parsedRequest") or {}).get("tool") == expected_tool
            and (req.get("parsedRequest") or {}).get("prompt") == expected_prompt
        ]

    def _ensure_market_seeded(
        self,
        entry: Dict[str, Any],
        safe_address: str,
        market_id: str,
    ) -> Generator[None, None, bool]:
        """Seed the kv cache from the subgraph once per (safe, market) pair.

        Exactly-once is keyed on the seed marker row, not on "is the kv
        namespace empty": an empty namespace is also the normal state of
        a brand-new market that has no history anywhere, and treating it
        as "needs seeding" would re-query the subgraph on every scan of
        every fresh market forever.

        Order matters: rows first, marker last. If the process dies
        mid-seed the marker is absent on the next cycle and the whole
        pass re-runs; row upserts are idempotent because seeded keys are
        derived from immutable subgraph request ids.

        :param entry: the per-market state dict.
        :param safe_address: the requester Safe address.
        :param market_id: the Omen market id.
        :yield: control to the FSM while kv_store and subgraph requests
            complete.
        :return: True if the cache is known to be seeded, False on any
            kv_store or subgraph error (callers must treat the cache as
            unreadable this cycle rather than risk a duplicate paid fire).
        """
        marker_key = mech_cache.seed_marker_key(
            prefix=self.params.mech_cache_key_prefix,
            safe_address=safe_address,
            market_id=market_id,
        )
        marker = yield from self._send_kv_read((marker_key,))
        if marker is None:
            return False
        if marker_key in marker:
            return True

        title = entry.get("title") or ""
        closing_ts = entry.get("market_closing_timestamp") or 0
        query = MECH_CACHE_QUERY_TEMPLATE.format(
            sender=safe_address.lower(),
            prompt=json.dumps(title),
            block_timestamp_gt=int(closing_ts),
        )
        historical = yield from self.get_mech_gnosis_subgraph_result(query)
        if historical is None:
            return False

        written = 0
        for row in historical:
            converted = mech_cache.subgraph_row_to_cache_row(row)
            if converted is None:
                self.context.logger.warning(
                    f"Skipping unusable subgraph row while seeding market "
                    f"{market_id}: {row!r}"
                )
                continue
            key = mech_cache.cache_key(
                prefix=self.params.mech_cache_key_prefix,
                safe_address=safe_address,
                market_id=market_id,
                nonce=converted["nonce"],
            )
            value = mech_cache.serialize_row(
                safe_address=safe_address,
                market_id=market_id,
                **converted,
            )
            ok = yield from self._send_kv_write(key, value)
            if not ok:
                self.context.logger.error(
                    f"kv_store write failed while seeding market {market_id}; "
                    "marker not written, seeding will re-run next cycle."
                )
                return False
            written += 1

        marker_value = json.dumps(
            {"seeded_at": self.last_synced_timestamp, "rows": written},
            sort_keys=True,
        )
        ok = yield from self._send_kv_write(marker_key, marker_value)
        if not ok:
            self.context.logger.error(
                f"kv_store write failed for the seed marker of market "
                f"{market_id}; seeding will re-run next cycle."
            )
            return False
        self.context.logger.info(
            f"Seeded {written} historical mech request row(s) for market "
            f"{market_id} from the Mech Gnosis subgraph."
        )
        return True

    def _wait_for_kv_reply(self, nonce: str) -> Generator[None, None, None]:
        """Block until the kv_store handler clears the single in-flight gate.

        The handler flips ``state.in_flight_req`` to ``False`` when the
        reply arrives and the per-nonce callback has run. A watchdog
        bounds the wait so a lost reply (dropped envelope, connection
        crash) doesn't wedge the scan cycle: the gate is force-cleared
        after ``mech_cache_kv_request_timeout`` seconds. Local SQLite
        replies land in sub-millisecond time in the healthy case, so the
        watchdog is only relevant to genuinely-stuck plumbing.

        On timeout the caller's callback is popped from ``req_to_callback``
        so a late reply for this nonce can't fire the (now-abandoned)
        callback. Without that pop, a stale reply arriving during the
        next kv_store request would (a) invoke the previous callback
        against the wrong reply and (b) flip ``in_flight_req`` to False
        while the current op is still waiting, letting the current
        caller's ``while state.in_flight_req`` loop exit early and read
        a partial result. The single-in-flight-op protocol depends on
        this pop.

        :param nonce: dialogue nonce of the in-flight op, popped on
            timeout so a late reply is inert.
        :yield: control to the FSM until the reply lands or the watchdog fires.
        """
        state = cast(SharedState, self.context.state)
        deadline = time.time() + self.params.mech_cache_kv_request_timeout
        while state.in_flight_req and time.time() < deadline:
            yield from self.sleep(0.05)
        if state.in_flight_req:
            self.context.logger.warning(
                "kv_store reply did not arrive within "
                f"{self.params.mech_cache_kv_request_timeout}s; force-clearing "
                "in_flight_req. The scan cycle will proceed with whatever "
                "partial data the callback recorded."
            )
            state.req_to_callback.pop(nonce, None)
            state.in_flight_req = False

    def _send_kv_write(
        self,
        key: str,
        value: str,
    ) -> Generator[None, None, bool]:
        """Upsert one row into the kv_store and block until the reply lands.

        :param key: the fully-namespaced kv_store key.
        :param value: the JSON-serialised row payload.
        :yield: control to the FSM until the reply lands.
        :return: True on SUCCESS, False on ERROR / no-reply.
        """
        outcome: Dict[str, bool] = {"ok": False}

        def _cb(reply: KvStoreMessage, _dlg: Any) -> None:
            outcome["ok"] = reply.performative == KvStoreMessage.Performative.SUCCESS

        state = cast(SharedState, self.context.state)
        msg, dlg = self.context.kv_store_dialogues.create(
            counterparty=str(KV_STORE_CONNECTION_PUBLIC_ID),
            performative=KvStoreMessage.Performative.CREATE_OR_UPDATE_REQUEST,
            data={key: value},
        )
        nonce = dlg.dialogue_label.dialogue_reference[0]
        state.req_to_callback[nonce] = (_cb, {})
        state.in_flight_req = True
        self.context.outbox.put_message(message=msg)
        yield from self._wait_for_kv_reply(nonce)
        return outcome["ok"]

    def _send_kv_read(
        self,
        keys: Tuple[str, ...],
    ) -> Generator[None, None, Optional[Dict[str, str]]]:
        """READ kv_store keys and block until the reply lands.

        The connection only returns rows that exist, so a missing key is
        simply absent from the returned mapping. That keeps "key not
        present" (absent from the dict) distinguishable from "read
        failed" (``None``) -- callers gating on key existence must not
        collapse the two.

        :param keys: the fully-namespaced kv_store keys to read.
        :yield: control to the FSM until the reply lands.
        :return: mapping of existing keys to their values, or ``None``
            on ERROR / no-reply.
        """
        result: Dict[str, Any] = {}

        def _cb(reply: KvStoreMessage, _dlg: Any) -> None:
            if reply.performative == KvStoreMessage.Performative.READ_RESPONSE:
                result["data"] = dict(reply.data)

        state = cast(SharedState, self.context.state)
        msg, dlg = self.context.kv_store_dialogues.create(
            counterparty=str(KV_STORE_CONNECTION_PUBLIC_ID),
            performative=KvStoreMessage.Performative.READ_REQUEST,
            keys=keys,
        )
        nonce = dlg.dialogue_label.dialogue_reference[0]
        state.req_to_callback[nonce] = (_cb, {})
        state.in_flight_req = True
        self.context.outbox.put_message(message=msg)
        yield from self._wait_for_kv_reply(nonce)
        return result.get("data")

    def _send_kv_list(
        self,
        prefix: str,
    ) -> Generator[None, None, Optional[Dict[str, str]]]:
        """LIST kv_store rows under a prefix, paginating until exhausted.

        Returns the merged ``{key -> value}`` map across all pages, or
        ``None`` on ERROR / no-reply. Pagination uses the connection's
        ``next_cursor`` field: an empty cursor means the walk is complete.

        :param prefix: the kv_store key prefix to filter by.
        :yield: control to the FSM until each page reply lands.
        :return: merged rows, or None on error.
        """
        merged: Dict[str, str] = {}
        cursor: str = ""
        # Bound the walk defensively; each iteration issues one LIST.
        # A namespace bigger than page_size * max_pages hasn't been seen
        # in practice (a single safe wouldn't ask about > page_size
        # markets in one retention window), but the guard prevents an
        # infinite loop if the server ever returns a non-empty cursor
        # on an empty page.
        max_pages = 1024

        for _ in range(max_pages):
            page: Dict[str, Any] = {}

            # ``page`` bound as a default arg so the closure captures the
            # current iteration's dict, not a re-used loop-scope name
            # (flake8-bugbear B023).
            def _cb(
                reply: KvStoreMessage,
                _dlg: Any,
                _page: Dict[str, Any] = page,
            ) -> None:
                if reply.performative == KvStoreMessage.Performative.LIST_RESPONSE:
                    _page["data"] = dict(reply.data)
                    _page["next_cursor"] = reply.next_cursor or ""

            state = cast(SharedState, self.context.state)
            msg, dlg = self.context.kv_store_dialogues.create(
                counterparty=str(KV_STORE_CONNECTION_PUBLIC_ID),
                performative=KvStoreMessage.Performative.LIST_REQUEST,
                key_prefix=prefix,
                limit=self.params.mech_cache_list_page_size,
                cursor=cursor,
            )
            nonce = dlg.dialogue_label.dialogue_reference[0]
            state.req_to_callback[nonce] = (_cb, {})
            state.in_flight_req = True
            self.context.outbox.put_message(message=msg)
            yield from self._wait_for_kv_reply(nonce)

            if "data" not in page:
                # ERROR or watchdog timeout; abandon the walk.
                return None
            merged.update(page["data"])
            cursor = page["next_cursor"]
            if not cursor:
                return merged
        self.context.logger.warning(
            "kv_store LIST paginated past %d pages without exhausting the "
            "namespace; returning partial results.",
            max_pages,
        )
        return merged
