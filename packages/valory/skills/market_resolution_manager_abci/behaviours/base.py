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

# Both fire-time and delivery-time kv writes guard against duplicate paid
# mech requests, so a transient failure is retried a few times before
# being swallowed by ``_send_kv_write_with_retries``.
MAX_KV_WRITE_ATTEMPTS = 3
KV_WRITE_RETRY_SLEEP_SECONDS = 0.2

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


def pick_earliest_usable_seed_delivery(
    deliveries: List[Any],
) -> Optional[Dict[str, Any]]:
    """Delivery selector for :func:`mech_cache.subgraph_row_to_cache_row`.

    Preserves the "iterate all deliveries" contract that
    ``_earliest_valid_evaluation`` documents at scan time (see the
    comment on that helper): a mech-internal retry whose first delivery
    is garbage but a later one is valid must still resolve on the
    seeded cache row, even though the row schema only has slots for
    one ``result`` / ``delivered_at`` pair.

    Intent, in order of preference:

    1. Return the earliest delivery with both a cache-valid evaluation
       (``parse_mech_response`` + ``is_cached_evaluation_valid``) AND
       a numeric ``blockTimestamp`` -- the numeric-ts requirement is
       load-bearing here because the row schema stores ``result`` and
       ``delivered_at`` as one atomic unit
       (see :data:`mech_cache.DeliverySelector`), so a valid response
       without a persistable timestamp can't be kept.
    2. Failing that, fall back to the earliest delivery with a numeric
       ``blockTimestamp`` (regardless of ``toolResponse`` validity).
       Delegated to :func:`mech_cache.default_delivery_selector` so
       the "earliest numeric-ts" policy stays in one place. This
       degraded row rehydrates into a request whose one delivery
       won't validate downstream, so the market classifies unanswered
       and a fresh mech request will fire on the next scan.
    3. If nothing matches either phase, return ``None``. The seeded
       row still gets written (``fired_at`` comes from the request
       body, not the deliveries) so the fire itself stays visible on
       ``mech_requests``; the retry budget is preserved by that
       row-level accounting regardless of which phase produces the
       delivery.

    :param deliveries: subgraph ``deliveries`` list (ascending order).
    :return: the chosen delivery, or ``None`` if none can be used.
    """
    for delivery in deliveries:
        if not isinstance(delivery, dict):
            continue
        evaluation = parse_mech_response(delivery.get("toolResponse"))
        if evaluation is None:
            continue
        if not is_cached_evaluation_valid(evaluation):
            continue
        try:
            int(delivery.get("blockTimestamp"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        return delivery
    # Phase 2 -- "earliest with numeric ts" is the mech_cache module's
    # default policy already; delegate so the fallback semantics stay
    # in one place and can't drift between the two callers.
    return mech_cache.default_delivery_selector(deliveries)


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
    ) -> Generator[None, None, Optional[List[Dict[str, Any]]]]:
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

    def _audit_row_deliveries_shape(self, row: Any, market_id: str) -> List[Any]:
        """Return the row's ``deliveries`` as a safe list + warn on drift.

        The top-level ``historical`` list is guarded before this loop;
        this covers peer drift inside ``row["deliveries"]``:

        - Row itself is not a dict -> returns ``[]``. No warning here;
          ``subgraph_row_to_cache_row`` will return ``None`` on the
          same input and the outer loop already logs "unusable
          subgraph row".
        - ``deliveries`` is a truthy non-list envelope (dict, string,
          etc.) -> warns and returns ``[]``. Without this branch the
          row seeded delivery-less with zero diagnostics and looked
          identical to "genuinely undelivered" in the store.
        - ``deliveries`` is a list containing non-dict entries ->
          warns naming the row id and returns the list verbatim (the
          picker's own ``isinstance(delivery, dict)`` guard skips the
          drifted entries).

        :param row: one entry from the ``historical`` list.
        :param market_id: id of the market being seeded, for the log.
        :return: a list of deliveries safe to iterate on downstream
            (empty list on non-dict row or non-list envelope).
        """
        if not isinstance(row, dict):
            return []
        raw_deliveries = row.get("deliveries")
        if raw_deliveries is None:
            return []
        if not isinstance(raw_deliveries, list):
            self.context.logger.warning(
                "Non-list ``deliveries`` envelope in subgraph row for "
                "market %s (type=%s); treating as no deliveries. "
                "row_id=%r",
                market_id,
                type(raw_deliveries).__name__,
                row.get("id"),
            )
            return []
        if any(not isinstance(d, dict) for d in raw_deliveries):
            self.context.logger.warning(
                "Non-dict entries in subgraph deliveries for market %s; "
                "ignoring the drifted entries. row_id=%r",
                market_id,
                row.get("id"),
            )
        return raw_deliveries

    def _log_seed_phase(
        self,
        converted: Dict[str, Any],
        raw_deliveries: List[Any],
        market_id: str,
    ) -> None:
        """Emit the per-row seed-phase trace for one converted seed row.

        See :func:`pick_earliest_usable_seed_delivery` for the phase
        model. Same operational shape across the branches: any log
        emitted here corresponds to a seeded row that scan will
        classify as unanswered (so a fresh mech request will fire),
        with the branch distinguishing the *cause*:

        - Phase 3: picker returned ``None``. Gated on "the row
          actually had deliveries" so a healthy in-flight market
          with ``deliveries: []`` stays silent.
        - Null-``toolResponse`` sub-case: picker kept a delivery
          whose ``toolResponse`` was null in source. This is the
          expected shape of every delivered request under the
          off-chain regime (the mech stopped uploading response
          content), so a fresh Safe with post-migration-only
          history hits it once per delivered row. Logged at ``INFO``
          so it doesn't dilute the WARNING channel the other
          degraded branches use for genuinely anomalous cases.
        - Phase 2: picker kept a delivery whose ``toolResponse``
          doesn't validate. Message calls out that an earlier valid
          delivery may have been skipped for a non-numeric
          timestamp (phase 1 requires both a validating response
          AND a numeric ts).
        - Phase 1 (silent): valid evaluation preserved; no log.

        :param converted: cache-row fields returned by
            :func:`mech_cache.subgraph_row_to_cache_row`.
        :param raw_deliveries: original ``deliveries`` list from the
            subgraph row (see :meth:`_audit_row_deliveries_shape`);
            used to gate the phase-3 log.
        :param market_id: id of the market being seeded, for the log.
        """
        nonce = converted["nonce"]
        if converted.get("delivered_at") is None:
            if raw_deliveries:
                self.context.logger.warning(
                    "Seeded row %s for market %s carries no delivery "
                    "(phase 3): the row had %d deliveries but none "
                    "carried a numeric blockTimestamp; scan will "
                    "classify unanswered and may re-fire when "
                    "retry_after expires.",
                    nonce,
                    market_id,
                    len(raw_deliveries),
                )
            return
        if converted.get("result") is None:
            self.context.logger.info(
                "Seeded row %s for market %s carries a delivery with "
                "null ``toolResponse`` (post-offchain-migration "
                "subgraph shape): scan will classify unanswered and "
                "may re-fire on the next scan.",
                nonce,
                market_id,
            )
            return
        if not is_cached_evaluation_valid(parse_mech_response(converted.get("result"))):
            self.context.logger.warning(
                "Seeded row %s for market %s carries a degraded "
                "delivery (phase 2): the kept delivery's toolResponse "
                "does not validate (an earlier valid delivery may "
                "have been skipped for a non-numeric timestamp); scan "
                "will classify unanswered and may re-fire next cycle.",
                nonce,
                market_id,
            )

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
        # Shape-drift guard: ``ApiSpecs.process_response`` returns Any, so a
        # ``response_key`` misconfiguration (or a subgraph replica shipping a
        # dict envelope where we expect the list) would fall through to the
        # for-loop below, iterate string keys, and every "row" would fail
        # the isinstance check inside subgraph_row_to_cache_row -- writing
        # a marker with ``rows: 0`` and permanently masking real history.
        if not isinstance(historical, list):
            self.context.logger.error(
                "Subgraph result for market %s has unexpected shape "
                "%s; expected list. Refusing to seed to avoid a bogus "
                "zero-row marker.",
                market_id,
                type(historical).__name__,
            )
            return False

        written = 0
        for row in historical:
            raw_deliveries = self._audit_row_deliveries_shape(row, market_id)
            converted = mech_cache.subgraph_row_to_cache_row(
                row,
                delivery_selector=pick_earliest_usable_seed_delivery,
            )
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
            self._log_seed_phase(converted, raw_deliveries, market_id)

        # If we saw non-empty historical rows but none of them were usable,
        # writing the marker with ``rows: 0`` would permanently mask the
        # market's real pre-migration history: every future scan would
        # short-circuit at the marker check and never re-attempt seeding.
        # That defeats the whole point of this feature (protecting an
        # in-flight market's retry budget across the migration), so treat
        # a total-loss row set as a seeding failure and skip the marker.
        # The genuinely-fresh case (empty historical + rows==0) is still
        # marked as seeded so we don't re-query the subgraph forever.
        if written == 0 and len(historical) > 0:
            self.context.logger.error(
                "Subgraph returned %d historical rows for market %s but "
                "none were usable (schema drift or garbage). Skipping the "
                "seed marker so the next scan cycle retries; otherwise the "
                "market's real fire history would be permanently invisible "
                "and it could be re-requested (paid) up to "
                "``max_mech_retries`` times.",
                len(historical),
                market_id,
            )
            return False

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

    def _send_kv_write_with_retries(
        self,
        key: str,
        value: str,
        max_attempts: int,
        sleep_seconds: float,
        retry_label: str,
    ) -> Generator[None, None, bool]:
        """Upsert with a bounded retry loop over transient KV failures.

        Both the fire-time and delivery-time cache writes protect against
        the same failure class: a lost write can leave the "have I already
        asked/answered this market?" cache stale, which risks a duplicate
        paid mech request on the next scan. Sharing one helper keeps that
        contract in one place -- per-attempt failures log at ``WARNING``,
        the final give-up logs at ``ERROR`` so operators watching for
        financial-loss signals see it without filtering retry chatter.

        :param key: the fully-namespaced kv_store key.
        :param value: the JSON-serialised row payload.
        :param max_attempts: total attempts including the first one.
        :param sleep_seconds: pause between attempts.
        :param retry_label: short prefix ("fire-time" / "delivery-time")
            so operators can tell the two failure classes apart in prod
            logs. Keep it short: it is stitched into every per-attempt
            WARNING line, not just the final ERROR.
        :yield: control to the FSM while attempts are in flight.
        :return: True if any attempt succeeded, False after all failed.
        """
        for attempt in range(1, max_attempts + 1):
            ok = yield from self._send_kv_write(key=key, value=value)
            if ok:
                return True
            if attempt < max_attempts:
                self.context.logger.warning(
                    "%s kv_store write for key=%s failed (attempt %d/%d); retrying.",
                    retry_label,
                    key,
                    attempt,
                    max_attempts,
                )
                yield from self.sleep(sleep_seconds)
        # ERROR (not WARNING) because the give-up may cause a duplicate
        # paid mech call on the next cycle -- a financial-loss signal
        # that shouldn't sit at the same level as the retry chatter.
        self.context.logger.error(
            "%s kv_store write for key=%s failed after %d attempts.",
            retry_label,
            key,
            max_attempts,
        )
        return False

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
