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

The row schema mirrors the fields the downstream code
(``_earliest_valid_evaluation`` and the retry counter) already expects
from the old subgraph return shape, so callers don't need to change:
``rehydrate_to_subgraph_shape`` turns a batch of kv rows into a list of
dicts identical in shape to what the removed subgraph query returned.

Scope: storage-bound, not privacy-bound. The kv_store file is per-agent
local on a Propel PVC; a fresh redeploy starts empty and market-resolver
will re-ask a bounded number of markets before catching up. Documented
in ``docs/market_resolver_offchain_scope.md`` in autonolas-marketplace.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


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
