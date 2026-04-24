# Mech response subgraph cache

## Goal

Before firing a fresh Mech request, check the Mech Marketplace Gnosis subgraph for a prior response from our own Safe on the same question. If one exists, store it in the local DB the same way a fresh Mech response would be stored, and let downstream logic decide whether it's sufficient.

## Model (locked)

1. **Fetch is separate from decide.** "Do I have a cached Mech evaluation?" and "Is that evaluation good enough to act on?" are two independent checks.
2. **Cached response persists regardless of status.** Undeterminable results are stored too — they trip the existing `retry_after` cooldown in `build_answer_tx.py`, same as fresh undeterminable results.
3. **"Valid cached evaluation" (blocks fresh Mech):** `is_valid=true AND is_determinable=true AND has_occurred is not None`. An undeterminable entry is stored but does NOT block a fresh Mech call after cooldown.
4. **Subgraph lookup trigger:** only when `entry["evaluation"]` is missing. Not re-queried on every scan.
5. **When to fire fresh Mech:** need to verify, no valid cached answer (neither local DB nor subgraph), and retry cooldown allows.

## Behavior flow (new)

In `EvaluateAnswersBehaviour.async_act`:

```
if action == CHALLENGE_PENDING and entry["evaluation"] is not None:
    reuse local evaluation → skip Mech (existing behavior, unchanged)

elif entry["evaluation"] is None:
    # NEW: try subgraph before firing Mech
    cached = yield from _fetch_mech_response_from_subgraph(market_id, title, closing_ts)
    if cached is not None:
        entry["evaluation"] = cached.evaluation
        entry["mech_response"] = {
            "source": "subgraph",
            "block_timestamp": cached.block_timestamp,
            "subgraph_request_id": cached.request_id,
            "result_raw": cached.result_raw,
        }
        persist entry
        if cached is valid (determinable):
            → skip Mech, emit evaluation_result branch (go to BuildAnswerTxRound)
        else:
            → cached is undeterminable, store it anyway
            → fall through to "fire fresh Mech" (below)
            → BUT respect mech_retries / retry cooldown

if still need fresh Mech AND retries < max AND cooldown allows:
    fire fresh Mech (existing code path)
```

**Precedence:** local DB hit > subgraph hit > fresh Mech.

## Data fields (new on entry)

`entry["mech_response"]` is already written by `build_answer_tx.py`; extend its shape:

```json
{
  "source": "mech_interact" | "subgraph",
  "nonce": "<uuid>" | null,
  "block_timestamp": <int>,
  "subgraph_request_id": "<str>" | null,
  "result_raw": "<str>"
}
```

Existing code writes `source="mech_interact"` implicitly (field is new). No migration — old entries without `source` default to `"mech_interact"` when read.

## Subgraph query

Endpoint: `https://api.subgraph.autonolas.tech/api/proxy/marketplace-gnosis`

```graphql
query GetMechResponseForMarket($sender: String!, $questionTitle: String!, $blockTimestamp_gt: BigInt!) {
  sender(id: $sender) {
    requests(
      where: {
        parsedRequest_: { questionTitle: $questionTitle }
        blockTimestamp_gt: $blockTimestamp_gt
      }
      orderBy: blockTimestamp
      orderDirection: desc
      first: 5
    ) {
      id
      blockTimestamp
      parsedRequest {
        questionTitle
        tool
      }
      deliveries(first: 1) {
        toolResponse
      }
    }
  }
}
```

**Variables:**
- `sender` = `self.synchronized_data.safe_contract_address` (lowercase hex)
- `questionTitle` = `entry["title"]`
- `blockTimestamp_gt` = `entry["market_closing_timestamp"]` (the FPMM `openingTimestamp`)

**Client-side filters (iterate newest first, return first match):**
- `parsedRequest.tool == self.params.mech_tool_resolve_market`
- `deliveries[0].toolResponse` present and parseable via the existing Mech result parser

## Files to touch

### New
- `packages/valory/skills/market_resolution_manager_abci/behaviours/_mech_parse.py` — extract `_parse_mech_response` from `build_answer_tx.py` into a shared helper (used by both `build_answer_tx.py` and `evaluate_answers.py`).

### Modified

**`packages/valory/skills/market_resolution_manager_abci/`**
- `models.py` — add `MechGnosisSubgraph(ApiSpecs)` class.
- `skill.yaml` — register `mech_gnosis_subgraph` model with URL, `response_key: data:sender`, `response_type: dict`. Document param.
- `behaviours/base.py` — add `get_mech_gnosis_subgraph_result(query, variables)` mirroring `get_omen_subgraph_result`.
- `behaviours/build_answer_tx.py` — move `_parse_mech_response` into `_mech_parse.py`, import from there.
- `behaviours/evaluate_answers.py` — add `_fetch_mech_response_from_subgraph(market_id, title, closing_ts)`. Wire into `async_act` per the flow above.

**`packages/valory/skills/market_resolver_abci/`** (composed)
- `models.py` — re-export `MechGnosisSubgraph`.
- `skill.yaml` — add `mech_gnosis_subgraph` model entry.

**`packages/valory/agents/market_resolver/aea-config.yaml`**
- Add `mech_gnosis_subgraph` override with default URL.

**`packages/valory/services/market_resolver/service.yaml`**
- Add `mech_gnosis_subgraph` override with env-var pattern for per-deployment URL.

### Tests
- `tests/behaviours/test_evaluate_answers.py` — new cases:
  1. Local evaluation present → no subgraph, no Mech (existing).
  2. No local, subgraph returns valid determinable → stored + skip Mech.
  3. No local, subgraph returns undeterminable → stored + fall through to fresh Mech.
  4. No local, subgraph returns response with wrong tool → filtered out, fresh Mech.
  5. No local, subgraph returns response with `blockTimestamp < closing_ts` → query filter excludes it, fresh Mech.
  6. No local, subgraph returns response with unparseable `toolResponse` → skipped, try next, then fresh Mech.
  7. No local, subgraph returns empty → fresh Mech.
  8. Subgraph call errors out → log, fall through to fresh Mech (don't crash).
- `tests/behaviours/test_base.py` — new case for `get_mech_gnosis_subgraph_result`.
- `tests/behaviours/test_build_answer_tx.py` — update imports for moved `_parse_mech_response` (if tested directly).
- `tests/test_models.py` — existing import smoke test covers the new class.

## Open Autonomy discipline

After code changes:
- `autonomy packages lock` as last auto-fix step
- Full `oa-linters` pipeline
- 100% coverage on new code
- Update skill.yaml dependencies if any new imports

## Non-goals

- Not adding a `mech_cache_max_age_seconds` cap. YAGNI — the existing `retry_after` and `blockTimestamp_gt closing_ts` guards already bound staleness.
- Not querying the subgraph on every scan. Only on first evaluation where `entry["evaluation"]` is missing.
- Not changing the re-challenge flow.
- Not backfilling `source` on existing DB entries.
