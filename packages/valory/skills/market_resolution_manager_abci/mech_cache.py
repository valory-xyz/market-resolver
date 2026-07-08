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

"""Durable "have I asked the mech about this market?" cache.

Under the on-chain path, market-resolver used the Mech Marketplace Gnosis
subgraph to answer that question by filtering ``sender = safe`` and
``parsedRequest.prompt = title``. Under the off-chain path the mech no
longer uploads request/response content to IPFS, so the subgraph's
``parsedRequest.prompt`` and ``deliveries[].toolResponse`` fields go empty
and the query stops returning useful rows.

This module owns the replacement: one row per (safe, market_id, nonce)
persisted via the ``valory/kv_store`` connection (SQLite + WAL on a
Propel-provisioned PVC). Writes happen at fire time
(``evaluate_answers``) and again at delivery time (``build_answer_tx``).
Reads happen once per scan cycle (``scan_markets``) via a prefixed
``LIST_REQUEST``.

**Migration seeding.** The kv store starts empty on the first deploy of
the off-chain path, but in-flight markets may already have on-chain
request history that only the subgraph knows about. To avoid re-firing
paid requests for those markets, ``fetch_mech_requests_for_market``
lazily seeds the cache once per (safe, market) pair: if no seed marker
row exists (``seed_marker_key``), the historical rows are pulled from
the subgraph, converted with ``subgraph_row_to_cache_row``, upserted,
and the marker written last. The marker key lives outside the row LIST
prefix so it never shows up in row listings.

The row schema mirrors the fields the downstream code
(``_earliest_valid_evaluation`` and the retry counter) already expects
from the old subgraph return shape, so callers don't need to change:
``rehydrate_to_subgraph_shape`` turns a batch of kv rows into a list of
dicts identical in shape to what the removed subgraph query returned.

**Rows are never pruned.** Nothing sends ``DELETE_REQUEST``; the store
grows on every fired mech request and only shrinks on a fresh redeploy
(which starts from an empty PVC). Reads stay cheap
because ``LIST_REQUEST`` is scoped to a per-market prefix, but the raw
size grows with the fleet's lifetime request volume. Pruning is a
planned follow-up:

- Simplest: age-based DELETE keyed on ``fired_at`` past a configurable
  retention window (mirrors the mech-side preimage buffer pattern
  established in valory-xyz/mech PR #455).
- Alternative: LIST + filter + DELETE by market_id once a market
  finalises on-chain, since the "have I asked?" question is moot for
  finalised markets.

Neither is in scope here. Tracking issue TBD; see
``docs/market_resolver_offchain_scope.md`` in autonolas-marketplace.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

# Deliveries as they come from the subgraph are untrusted (see the
# ``row: Any`` convention on :func:`subgraph_row_to_cache_row`); every
# implementation guards with ``isinstance(delivery, dict)`` at the
# entry to each frame. Type as ``List[Any]`` to match. Invariant a
# selector must honour: the returned delivery, if any, must carry a
# numeric ``blockTimestamp``, else ``subgraph_row_to_cache_row`` seeds
# the row without delivery info (both ``result`` and ``delivered_at``
# get dropped as one atomic unit).
DeliverySelector = Callable[[List[Any]], Optional[Dict[str, Any]]]


def cache_key(prefix: str, safe_address: str, market_id: str, nonce: str) -> str:
    """Return the kv_store key for one fired mech request.

    The prefix + safe + market_id triple lets a LIST_REQUEST at
    ``f"{prefix}{safe}/{market_id}/"`` fetch exactly the rows for one
    market from one safe. Lowercase the safe here so callers don't have
    to remember to normalise everywhere.

    :param prefix: operator-configured key namespace (e.g. ``market_resolver/``).
    :param safe_address: the requester Safe address; normalised to lowercase.
    :param market_id: the Omen market id.
    :param nonce: the client-generated uuid4 from ``evaluate_answers``.
    :return: the fully-namespaced kv_store key.
    """
    return f"{prefix}{safe_address.lower()}/{market_id}/{nonce}"


def list_prefix(prefix: str, safe_address: str, market_id: str) -> str:
    """Return the LIST_REQUEST prefix scoped to one (safe, market) pair.

    :param prefix: operator-configured key namespace.
    :param safe_address: the requester Safe address; normalised to lowercase.
    :param market_id: the Omen market id.
    :return: the prefix suitable for LIST_REQUEST.key_prefix.
    """
    return f"{prefix}{safe_address.lower()}/{market_id}/"


def seed_marker_key(prefix: str, safe_address: str, market_id: str) -> str:
    """Return the kv_store key of the one-time seed marker for a market.

    Presence of this key means "the subgraph history for this
    (safe, market) pair has already been copied into the kv cache";
    its value is informational only. The key is deliberately outside
    the ``list_prefix`` namespace (``{prefix}{safe}/{market_id}/``) so
    a row LIST never returns it: ``seeded`` can't collide with a safe
    address segment because addresses are ``0x``-prefixed hex.

    :param prefix: operator-configured key namespace.
    :param safe_address: the requester Safe address; normalised to lowercase.
    :param market_id: the Omen market id.
    :return: the fully-namespaced marker key.
    """
    return f"{prefix}seeded/{safe_address.lower()}/{market_id}"


def default_delivery_selector(
    deliveries: List[Any],
) -> Optional[Dict[str, Any]]:
    """Return the earliest delivery whose ``blockTimestamp`` parses as int.

    Default policy for :func:`subgraph_row_to_cache_row` when the caller
    does not supply an evaluation-aware selector. Callers on the seeding
    path should pass a selector built from
    ``parse_mech_response`` + ``is_cached_evaluation_valid`` to preserve
    the "earliest usable across all deliveries" contract that
    ``_earliest_valid_evaluation`` documents at scan time; without it,
    a mech-internal retry whose first delivery is garbage but a later
    one is valid would be silently collapsed to the garbage row.

    Public so evaluation-aware selectors (see
    ``base.pick_earliest_usable_seed_delivery``) can delegate their
    "fall back to earliest numeric timestamp" phase here and keep the
    fallback semantics in one place.

    :param deliveries: subgraph ``deliveries`` list (ascending order).
    :return: earliest delivery with a numeric ``blockTimestamp``, or
        ``None`` if none of them parse.
    """
    for delivery in deliveries:
        if not isinstance(delivery, dict):
            continue
        try:
            int(delivery.get("blockTimestamp"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        return delivery
    return None


def subgraph_row_to_cache_row(
    row: Any,
    delivery_selector: Optional[DeliverySelector] = None,
) -> Optional[Dict[str, Any]]:
    """Convert one verbatim subgraph request entry to cache-row fields.

    Maps the subgraph request ``id`` to the row ``nonce`` (so seeded
    rows and kv-native rows share a keyspace without collisions --
    subgraph ids are tx-hash-derived, live nonces are uuid4), and one
    delivery to the ``result`` / ``delivered_at`` pair. Which delivery
    is chosen depends on ``delivery_selector``: seeding passes an
    evaluation-aware picker so a mech-internal retry whose first
    delivery is garbage but a later one is valid still resolves;
    callers that don't care fall through to
    :func:`default_delivery_selector` (earliest with numeric
    timestamp). ``error`` is always ``None``: the subgraph has no error
    field, and a garbage payload is handled downstream by
    ``parse_mech_response`` exactly as it was on the on-chain path.

    Never raises. A malformed entry (missing id, non-numeric
    timestamps) returns ``None`` so the seeding pass skips it instead
    of crashing the scan cycle.

    :param row: one entry from the subgraph ``requests`` list.
    :param delivery_selector: optional callable that picks which
        delivery to persist. Defaults to the earliest-numeric-ts one.
    :return: kwargs for :func:`serialize_row` (minus ``safe_address``
        and ``market_id``), or ``None`` if the entry is unusable.
    """
    if not isinstance(row, dict):
        return None
    nonce = row.get("id")
    if not isinstance(nonce, str) or not nonce:
        return None
    parsed_request = row.get("parsedRequest") or {}
    if not isinstance(parsed_request, dict):
        return None
    try:
        fired_at = int(row.get("blockTimestamp"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

    result: Optional[str] = None
    delivered_at: Optional[int] = None
    deliveries = row.get("deliveries") or []
    if isinstance(deliveries, list) and deliveries:
        selector = delivery_selector or default_delivery_selector
        delivery = selector(deliveries)
        if isinstance(delivery, dict):
            try:
                delivered_at = int(delivery.get("blockTimestamp"))  # type: ignore[arg-type]
                result = delivery.get("toolResponse")
            except (TypeError, ValueError):
                delivered_at = None

    return {
        "nonce": nonce,
        "tool": parsed_request.get("tool") or "",
        "prompt": parsed_request.get("prompt") or "",
        "fired_at": fired_at,
        "result": result,
        "error": None,
        "delivered_at": delivered_at,
    }


def serialize_row(
    safe_address: str,
    market_id: str,
    nonce: str,
    tool: str,
    prompt: str,
    fired_at: int,
    result: Optional[str] = None,
    error: Optional[str] = None,
    delivered_at: Optional[int] = None,
) -> str:
    """Serialize one cache row to its kv_store JSON string value.

    :param safe_address: the requester Safe address; not lowercased here
        so a caller can spot a bookkeeping mismatch between what was
        stored and what the key encodes.
    :param market_id: the Omen market id.
    :param nonce: the client-generated request nonce.
    :param tool: the mech tool the request targets.
    :param prompt: the market title / question sent to the mech.
    :param fired_at: epoch seconds when the request was fired.
    :param result: the raw mech response JSON string when delivered,
        else ``None``.
    :param error: the mech error string when delivered with error,
        else ``None``.
    :param delivered_at: epoch seconds of the delivery, else ``None``.
    :return: JSON string suitable for CREATE_OR_UPDATE_REQUEST.value.
    """
    return json.dumps(
        {
            "safe": safe_address,
            "market_id": market_id,
            "nonce": nonce,
            "tool": tool,
            "prompt": prompt,
            "fired_at": fired_at,
            "result": result,
            "error": error,
            "delivered_at": delivered_at,
        },
        sort_keys=True,
    )


def parse_row(raw: str) -> Optional[Dict[str, Any]]:
    """Parse one kv_store row value string; return None if unparseable.

    Never raise. A malformed / schema-drifted row is skipped rather
    than crashing the scan cycle.

    :param raw: the JSON string stored under a kv_store key.
    :return: the parsed row dict, or ``None`` on any parse failure.
    """
    try:
        row = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(row, dict):
        return None
    return row


def rehydrate_to_subgraph_shape(
    rows_by_key: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Turn kv_store LIST rows into the shape the old subgraph returned.

    ``_earliest_valid_evaluation`` and the ``mech_retries`` counter
    consume the pre-refactor return shape verbatim, so rehydrating
    here keeps the callers untouched:

    .. code-block:: python

        [{
          "id":             "<nonce>",
          "blockTimestamp": "<fired_at>",
          "parsedRequest":  {"prompt": ..., "tool": ...},
          "deliveries": [{
            "id":             "<nonce>",
            "blockTimestamp": "<delivered_at>",
            "toolResponse":   "<raw result string>"
          }] if delivered else []
        }, ...]

    A row without ``delivered_at`` maps to ``deliveries: []`` (the mech
    hasn't replied yet), which is what the previous subgraph return
    would have looked like too.

    Rows that fail to parse are skipped with no side effect; the
    caller-visible list simply omits them.

    :param rows_by_key: kv_store LIST_RESPONSE.data mapping (fully-
        namespaced key -> JSON string value).
    :return: list of dicts shaped like the old subgraph rows.
    """
    out: List[Dict[str, Any]] = []
    for _key, raw in rows_by_key.items():
        row = parse_row(raw)
        if row is None:
            continue
        deliveries: List[Dict[str, Any]] = []
        if row.get("delivered_at") is not None:
            deliveries.append(
                {
                    "id": row.get("nonce"),
                    "blockTimestamp": str(row.get("delivered_at")),
                    "toolResponse": row.get("result"),
                }
            )
        out.append(
            {
                "id": row.get("nonce"),
                "blockTimestamp": str(row.get("fired_at")),
                "parsedRequest": {
                    "prompt": row.get("prompt"),
                    "tool": row.get("tool"),
                },
                "deliveries": deliveries,
            }
        )
    return out
