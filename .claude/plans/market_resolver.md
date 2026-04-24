# Plan: Market Resolver Service

> Architecture context: [market-creator/.claude/plans/service_split.md](/home/jose/git/market-creator/.claude/plans/service_split.md)
> Shared fund recovery skill: [market-creator/.claude/plans/omen_funds_recoverer.md](/home/jose/git/market-creator/.claude/plans/omen_funds_recoverer.md)
> Domain reference: [market-creator/.claude/docs/omen_lifecycle.md](/home/jose/git/market-creator/.claude/docs/omen_lifecycle.md)

## Purpose

Autonomous market resolver — monitors Omen prediction markets for incorrect or missing Realitio answers, provides initial answers, and challenges wrong answers. Runs as an independent service with its own Gnosis Safe.

---

## Service Composition

```
AgentRegistrationAbciApp
  → IdentifyServiceOwnerAbciApp         # resolve real owner (handles staking indirection)
  → FundsForwarderAbciApp               # sweep excess funds to service owner
  → OmenFundsRecovererAbciApp           # recover locked funds (LP, CT, bonds)
  → MarketResolutionManagerAbciApp      # core: scan, evaluate, answer/challenge
  → TransactionSettlementAbciApp
  → MechInteractAbciApp                 # AI evaluation of answers
  → ResetPauseAbciApp
  + TerminationAbciApp (background)
```

### Skills needed (dev packages)

| Skill | Source | Notes |
|-------|--------|-------|
| `identify_service_owner_abci` | From market-creator repo (shared) | Resolves real owner through staking contract |
| `funds_forwarder_abci` | From market-creator repo (shared) | Sweeps excess funds to owner |
| `omen_funds_recoverer_abci` | From market-creator repo (shared) | Recovers LP tokens, CT positions, Realitio bonds |
| `market_resolution_manager_abci` | **New — this repo** | Core watchdog logic |
| `market_resolver_abci` | **New — this repo** | Composed app (wires all skills) |

---

## `market_resolution_manager_abci` — Core Skill

### No keeper selection needed

All operations in this skill are **deterministic**: subgraph queries return the same data for all agents, Mech responses are shared via consensus, and challenge tx construction is deterministic given the same inputs. No randomness or keeper selection is required.

### FSM Specification

```
Rounds:
  0. ScanPendingMarketsRound              # scan pending markets, classify answers
  1. EvaluateAnswersRound          # decide: request Mech or reuse existing data
  2. BuildChallengesTxRound        # build answer/challenge tx or mark as verified
  3. CleanupTrackedMarketsRound          # purge finalized markets from DB
  4. FinishedWithMechRequestRound  (degenerate → MechInteract)
  5. FinishedWithChallengeTxRound  (degenerate → TxSettlement)
  6. FinishedResolutionRound       (degenerate → ResetPause)

Initial state: ScanPendingMarketsRound
Initial states: {ScanPendingMarketsRound, BuildChallengesTxRound}

Transition function:
  ScanPendingMarketsRound:
    DONE          → EvaluateAnswersRound          # found actionable question
    NONE          → CleanupTrackedMarketsRound          # all OK or no pending markets
    NO_MAJORITY   → ScanPendingMarketsRound
    ROUND_TIMEOUT → ScanPendingMarketsRound

  EvaluateAnswersRound:
    DONE          → FinishedWithMechRequestRound  # needs Mech → MechInteract → BuildChallenges
    NONE          → BuildChallengesTxRound        # already has Mech data → skip Mech, build tx
    NO_MAJORITY   → CleanupTrackedMarketsRound
    ROUND_TIMEOUT → CleanupTrackedMarketsRound

  BuildChallengesTxRound:                         # entered after MechInteract OR from Evaluate
    DONE          → FinishedWithChallengeTxRound  # tx built → TxSettlement → Cleanup
    NONE          → CleanupTrackedMarketsRound          # Mech agreed, no tx needed
    NO_MAJORITY   → CleanupTrackedMarketsRound
    ROUND_TIMEOUT → CleanupTrackedMarketsRound

  CleanupTrackedMarketsRound:
    DONE          → FinishedResolutionRound
    NONE          → FinishedResolutionRound
    NO_MAJORITY   → FinishedResolutionRound
    ROUND_TIMEOUT → FinishedResolutionRound
```

### Flow Diagram

```
ScanPendingMarkets ──DONE──► EvaluateAnswers ──DONE──► [MechInteract]
    │                       │                         │
    │                       │──NONE──┐                ▼
    │                                ▼         BuildChallengesTx
    │──NONE──┐           BuildChallengesTx            │
             │                  │              ──DONE──► [TxSettlement]
             │           ──DONE──► [TxSettl]          │
             │                  │──NONE──┐            │
             ▼                           ▼            ▼
         CleanupTrackedMarkets ◄────────────────────────────┘
             │
             ▼
       [FinishedResolution] → ResetPause
```

---

## Round Details

### `ScanPendingMarketsRound`

**Purpose**: Discover pending markets from watched creators, classify by answer status, and pick the first actionable question.

**Query** (Omen subgraph):

```graphql
fixedProductMarketMakers(
  where: {
    creator_in: $watched_creator_addresses
    openingTimestamp_lt: $now
    answerFinalizedTimestamp: null
  }
  orderBy: openingTimestamp
  orderDirection: asc
) {
  id, question { id, currentAnswer, currentAnswerBond, currentAnswerTimestamp, timeout }
}
```

Then for each answered market, fetch the latest answerer from Realitio subgraph.

**Classification logic** (unanswered questions first, then by finalization urgency):

```
for each pending question (unanswered first, then sorted by finalization_deadline ascending):

    # Apply re-scan logic first (update DB entries based on on-chain changes)
    if question_id in questions_db:
        if last_answer_timestamp changed:
            apply re-scan transitions (see "Re-scan logic" above)

    # Classify
    if question_id in questions_db AND status == TRUSTED_ANSWER:
        → skip (no action needed)

    elif question_id in questions_db AND status == VERIFIED_OK:
        → skip (Mech confirmed answer is correct)

    elif question_id in questions_db AND status == CHALLENGE_PENDING:
        → check if cooldown has elapsed
        → if elapsed: actionable = NEEDS_CHALLENGE (has Mech data, go straight to tx)
        → if not elapsed: skip (waiting)

    elif question_id in questions_db AND status == NEEDS_EVALUATION:
        → actionable = NEEDS_EVALUATION (needs Mech request)

    elif question_id NOT in questions_db:
        if latest_answerer is trusted:
            → add to DB as TRUSTED_ANSWER, skip
        else:
            → add to DB as NEEDS_EVALUATION, actionable
```

**Pick first actionable**: Select the first question with `NEEDS_EVALUATION` or `CHALLENGE_PENDING` with cooldown elapsed (in finalization-urgency order, unanswered first). Only one question is processed per cycle.

**Events**: `DONE` (found actionable question → EvaluateAnswers), `NONE` (nothing to do → Cleanup)

### `EvaluateAnswersRound`

**Purpose**: Decide whether to request Mech or reuse existing evaluation data.

**Logic**:
1. Read the selected question and its DB entry from SynchronizedData
2. If the question has no prior Mech evaluation (`evaluation` is null):
   - Build Mech request (question text + current answer context + resolution date)
   - Store as `mech_requests` in SynchronizedData
   - Emit `DONE` → `FinishedWithMechRequestRound` → MechInteract
3. If the question already has Mech evaluation data (re-challenge scenario):
   - Skip Mech request entirely — existing data is still valid
   - Emit `NONE` → `BuildChallengesTxRound` (go straight to tx)

**Events**: `DONE` (needs Mech → MechInteract), `NONE` (has Mech data → BuildChallenges)

### `BuildChallengesTxRound`

**Purpose**: Build answer/challenge transaction. Entered in two ways:
- After MechInteract returns (`FinishedMechResponseRound` → here via composition): fresh Mech data
- From EvaluateAnswers (`NONE` event): existing Mech data, re-challenge scenario

**Logic**:

```
mech_response = get mech result for the selected question

if question was NEEDS_ANSWER:
    → build submitAnswer(question_id, mech_answer, 0) with value = initial_bond
    → update DB: status = ANSWERED_BY_US

elif mech_answer AGREES with on_chain_answer:
    → update DB: status = VERIFIED_OK, on_chain_answer = current_answer
    → emit NONE (no tx needed)

elif mech_answer DISAGREES with on_chain_answer:
    → check economic viability (bond cap, balance)
    → update DB: status = CHALLENGE_PENDING, mech_answer = ..., detected_at = now
    → if challenge cooldown elapsed:
        → build submitAnswer(question_id, mech_answer, current_bond) with value = 2x bond
        → emit DONE
    → else:
        → emit NONE (wait for cooldown)
```

**Events**: `DONE` (challenge/answer tx built), `NONE` (Mech agreed or cooldown not elapsed)

### `CleanupTrackedMarketsRound`

**Purpose**: Purge finalized markets from `questions_db`.

Remove entries where `answerFinalizedTimestamp != null` (question fully resolved, no longer actionable).

**Events**: `DONE` (always)

---

## Questions Database

Persistent cross-period state stored in `SynchronizedData` (`cross_period_persisted_keys`).

### Schema

```python
questions_db: Dict[str, QuestionEntry] = {
    "<question_id_hex>": {
        # Status
        "status": "TRUSTED_ANSWER" | "NEEDS_EVALUATION" | "VERIFIED_OK" | "CHALLENGE_PENDING",
        "detected_at": 1711900000,               # timestamp when we first saw this state

        # On-chain state (from subgraph, refreshed each scan)
        "on_chain_answer": "0x00...00",          # current answer on Realitio
        "on_chain_bond": 10000000000000000000,   # current bond in wei
        "last_answerer": "0xabc...",             # who posted the current answer
        "last_answer_timestamp": 1711900000,     # when the current answer was posted (from Realitio subgraph)
        "market_closing_timestamp": 1712000000,  # openingTimestamp of the market
        "realitio_timeout": 86400,               # Realitio timeout for this question (seconds)
        # Derived (not stored, computed at scan time):
        #   finalization_deadline = last_answer_timestamp + realitio_timeout
        #   cooldown_elapsed = now > detected_at + (realitio_timeout * challenge_cooldown_fraction)
        #   is_challengeable = cooldown_elapsed AND now < finalization_deadline
        #   urgency_sort_key = finalization_deadline (ascending = most urgent first)

        # Mech interaction — stores the framework objects directly
        # Populated by EvaluateAnswersRound (request) and BuildChallengesTxRound (response)
        "mech_request": {                        # MechMetadata dict (null if not yet requested)
            "prompt": "Question: Will...",       #   the prompt sent to Mech
            "tool": "resolve-market-jury-v1",  #   which Mech tool
            "nonce": "abc123",                   #   unique request nonce
        },
        "mech_response": {                       # MechInteractionResponse dict (null if not yet received)
            "nonce": "abc123",
            "data": "Qm...",                     #   IPFS hash of request data
            "requestId": 12345,                  #   on-chain Mech request ID
            "result": "{\"answer\": ...}",       #   raw Mech response (JSON string)
            "error": "Unknown",                  #   error message if failed
        },

        # Parsed evaluation result (extracted from mech_response.result)
        "evaluation": {                          # null if not yet evaluated
            "answer": "0x00...01",               #   Mech's answer (encoded)
            "confidence": 0.91,                  #   Mech confidence score
            "agrees_with_on_chain": false,        #   whether Mech agrees with current on-chain answer
            "reasoning": "Based on...",          #   Mech's reasoning text
        },

        # Challenge tracking
        "challenge": {                           # null if not yet challenged
            "tx_hash": "0xdef...",               #   tx hash of our submitAnswer
            "bond": 20000000000000000000,        #   bond we posted in wei
            "answer": "0x00...01",               #   the answer we submitted
            "escalation_count": 0,               #   how many times we've re-challenged
        },

        # Retry tracking
        "mech_retries": 0,                       # how many times Mech request failed/timed out
    }
}
```

### Entry Lifecycle

The DB tracks all pending questions we've seen. Entries persist until the question finalizes, preserving Mech evaluation data across re-challenges.

```
First seen (trusted answerer)     → TRUSTED_ANSWER (no action, but tracked)
First seen (non-trusted answerer) → NEEDS_EVALUATION (queue for Mech)
  ↓ Mech agrees                  → VERIFIED_OK (no action)
  ↓ Mech disagrees               → CHALLENGE_PENDING (wait for cooldown)
  ↓ Cooldown elapsed, challenged → TRUSTED_ANSWER (we are now the answerer)
  ↓ Someone re-challenges us     → CHALLENGE_PENDING (re-challenge using existing Mech data, no new request)
  ↓ Question finalizes           → removed from DB (CleanupTrackedMarketsRound)
```

### Re-scan logic (each cycle)

On each scan, the behaviour checks every pending market from the subgraph:

**Questions NOT in DB:**

- Trusted answerer → add as `TRUSTED_ANSWER`
- Non-trusted answerer or unanswered → add as `NEEDS_EVALUATION`

**Questions already in DB** (compare `last_answer_timestamp` to detect changes):

- Answer unchanged → keep current status
- Answer changed, new answerer is trusted → status → `TRUSTED_ANSWER`
- Answer changed, new answerer is NOT trusted:
  - If we have prior Mech evaluation (`evaluation` is not null) → `CHALLENGE_PENDING` (reuse existing Mech answer, no new request)
  - Otherwise → `NEEDS_EVALUATION` (first time seeing a non-trusted answer, need Mech)

### State Transitions

| Current Status | Trigger | New Status |
|---|---|---|
| (not in DB) | Trusted answerer | `TRUSTED_ANSWER` |
| (not in DB) | Non-trusted answerer or unanswered | `NEEDS_EVALUATION` |
| `TRUSTED_ANSWER` | Answer unchanged | do nothing |
| `TRUSTED_ANSWER` | Answer changed by trusted | update timestamps, stay `TRUSTED_ANSWER` |
| `TRUSTED_ANSWER` | Answer changed by non-trusted, have evaluation | `CHALLENGE_PENDING` (reuse Mech answer) |
| `TRUSTED_ANSWER` | Answer changed by non-trusted, no evaluation | `NEEDS_EVALUATION` |
| `NEEDS_EVALUATION` | Mech agrees | `VERIFIED_OK` |
| `NEEDS_EVALUATION` | Mech disagrees | `CHALLENGE_PENDING` |
| `CHALLENGE_PENDING` | Cooldown elapsed + tx submitted | `TRUSTED_ANSWER` |
| `CHALLENGE_PENDING` | Answer changed by non-trusted | `NEEDS_EVALUATION` (different answer, need re-eval) |
| `VERIFIED_OK` | Answer unchanged | do nothing |
| `VERIFIED_OK` | Answer changed by trusted | `TRUSTED_ANSWER` |
| `VERIFIED_OK` | Answer changed by non-trusted | `NEEDS_EVALUATION` |
| any | `answerFinalizedTimestamp != null` | removed from DB |

---

## Challenge Timing Strategy

### The Problem

Realitio questions have a `timeout` (typically 24h). After the last answer is submitted, if no new answer arrives within `timeout`, the answer finalizes. Challenging too early gives the attacker time to re-escalate. Challenging too late risks missing the window.

But we also don't want to delay finalization unnecessarily — every challenge resets the timeout clock, extending the market's resolution time.

### The Design

We introduce a `challenge_cooldown_fraction` parameter (default: `0.25`). The cooldown period is:

```
cooldown = realitio_timeout * challenge_cooldown_fraction
challenge_after = last_wrong_answer_timestamp + cooldown
```

With default values (24h timeout, 0.25 fraction):
- Wrong answer posted at T=0
- Cooldown = 6 hours
- We challenge at T+6h
- New timeout starts: finalizes at T+6h+24h = T+30h
- Total resolution time: **30 hours** (1.25x the normal 24h)

This is a reasonable tradeoff — minimal delay to market resolution while still giving time for self-correction:
- `0.25` → challenge at 6h, total 30h (1.25x) — fast response, minimal resolution delay
- `0.5` → challenge at 12h, total 36h (1.5x) — more patience, more delay
- `0.75` → challenge at 18h, total 42h (1.75x) — very patient but doubles resolution time
- `1.0` → challenge at 24h — too late, answer already finalized!

### Why Wait at All?

1. **Reduces unnecessary escalation wars**: If the attacker sees no challenge for 18h, they may believe they succeeded and stop monitoring. Our late challenge catches them off guard.
2. **Gives time for self-correction**: Other market participants may challenge the wrong answer before us, saving us the bond.
3. **Minimizes resolution delay**: Challenging at 75% of timeout adds only 0.75x delay vs challenging immediately (which adds 1.0x).

### Edge Case: Urgent Markets

If a market is very close to its Realitio timeout (less than `challenge_urgency_buffer` remaining), challenge immediately regardless of cooldown. Default `challenge_urgency_buffer`: 3600 seconds (1 hour).

```python
time_until_finalization = (last_answer_timestamp + realitio_timeout) - now

if time_until_finalization < challenge_urgency_buffer:
    # Challenge NOW — about to finalize with wrong answer
    challenge_immediately = True
elif now >= detected_at + (realitio_timeout * challenge_cooldown_fraction):
    # Normal cooldown elapsed
    challenge_immediately = True
else:
    # Still in cooldown, wait
    challenge_immediately = False
```

---

## Walkthrough Example

### Cycle 1

Scan finds 4 pending questions (sorted by finalization urgency):

| Question | Latest Answerer | DB Status | Action |
|---|---|---|---|
| q1 | `0xTrusted` (whitelisted) | — | skip (trusted) |
| q2 | `0xOurSafe` | — | skip (us) |
| q3 | `0xUnknown` | not in DB | **pick** → `NEEDS_EVALUATION` |
| q4 | `0xUnknown` | not in DB | queued for next cycle |

Process q3: Mech evaluates → agrees with on-chain answer.
DB update: `q3 → VERIFIED_OK`

### Cycle 2

| Question | Latest Answerer | DB Status | Action |
|---|---|---|---|
| q1 | `0xTrusted` | — | skip |
| q2 | `0xOurSafe` | — | skip |
| q3 | `0xUnknown` | `VERIFIED_OK` (answer unchanged) | skip |
| q4 | `0xUnknown` | not in DB | **pick** → `NEEDS_EVALUATION` |
| q5 | `0xAttacker` | not in DB | queued for next cycle |

Process q4: Mech evaluates → agrees. DB: `q4 → VERIFIED_OK`

### Cycle 3

| Question | Latest Answerer | DB Status | Action |
|---|---|---|---|
| q1 | `0xTrusted` | — | skip |
| q2 | `0xOurSafe` | — | skip |
| q3 | `0xUnknown` | `VERIFIED_OK` | skip |
| q4 | `0xUnknown` | `VERIFIED_OK` | skip |
| q5 | `0xAttacker` | not in DB | **pick** → `NEEDS_EVALUATION` |

Process q5: Mech evaluates → **disagrees** (confidence 0.91).
DB: `q5 → CHALLENGE_PENDING, detected_at=now, mech_answer=0x00...01`

### Cycle 4

| Question | DB Status | Action |
|---|---|---|
| q5 | `CHALLENGE_PENDING` | cooldown not elapsed (detected 1 cycle ago) → skip |

Everything else OK → `NONE` → CleanupTrackedMarkets

### Cycle 5 (cooldown elapsed)

| Question | DB Status | Action |
|---|---|---|
| q5 | `CHALLENGE_PENDING` | cooldown elapsed → **pick** → `NEEDS_CHALLENGE` |

BuildChallengesTx: `submitAnswer(q5, 0x00...01, current_bond)` with 2x bond.
DB: `q5 → ANSWERED_BY_US`

### Cycle 6

| Question | DB Status | Action |
|---|---|---|
| q5 | `ANSWERED_BY_US`, latest answerer = our safe | skip (us) |

Answer finalizes after timeout. Next cycle: CleanupTrackedMarkets removes q5.

---

## Configuration Parameters

```yaml
# Watchdog scope
watched_creator_addresses: []          # market creator safes to monitor (required)
trusted_addresses: []                  # trusted answerers (our safe auto-included)

# Mech
mech_tool: "resolve-market-jury-v1"  # single tool for both answering and evaluating
                                                # will migrate to "resolve-market-jury" when live

# Answering & Challenging
initial_answer_bond: 10000000000000000000   # 10 xDAI in wei (for unanswered questions)
challenge_cooldown_fraction: 0.25      # wait this fraction of realitio_timeout before challenging
challenge_urgency_buffer: 3600         # 1 hour — challenge immediately if less than this remains
challenge_confidence_threshold: 0.85   # minimum Mech confidence to act
max_challenge_bond: 100000000000000000000  # 100 xDAI absolute cap in wei

# Limits
max_escalation_rounds: 5               # stop re-challenging after this many rounds
max_mech_retries: 3                    # max Mech request retries before giving up on a question
```

---

## Fraud Detection: Why This Works

### The Attack Pattern

```
1. Market: "Will X happen?" — odds 90% Yes / 10% No
2. Attacker buys cheap "No" tokens (10% implied price)
3. After openingTimestamp, attacker submits answer "No" with minimum bond
4. If unchallenged within timeout, "No" finalizes → attacker profits
5. LP (market creator) holds worthless "Yes" residual tokens → loss
```

### Our Defense

1. **Scan** catches the wrong answer from a non-trusted address
2. **Mech evaluation** confirms it's wrong (high confidence)
3. **Cooldown** waits strategically — minimizes resolution delay while maximizing surprise
4. **Challenge** posts correct answer with 2x bond
5. **If attacker re-escalates**: we re-evaluate and re-challenge (up to `max_escalation_rounds`)
6. **When correct answer finalizes**: `omen_funds_recoverer_abci` claims all accumulated bonds

### What the Resolver Wins

If correct: all accumulated bonds from the entire answer chain via `claimWinnings`.
If wrong: loses posted bonds. `challenge_confidence_threshold` is the critical safety parameter.

---

## Composed App Transition Mapping

```
FinishedRegistrationRound               → IdentifyServiceOwnerRound
FinishedIdentifyServiceOwnerRound       → FundsForwarderRound
FinishedIdentifyServiceOwnerError       → RemoveLiquidityRound
FinishedFundsForwarderWithTxRound       → TxSettlement
FinishedFundsForwarderNoTxRound         → RemoveLiquidityRound

# Fund recovery
FinishedWithRecoveryTxRound             → TxSettlement
FinishedWithoutRecoveryTxRound          → ScanPendingMarketsRound

# Watchdog
FinishedWithMechRequestRound            → MechVersionDetectionRound
FinishedMechResponseRound               → BuildChallengesTxRound
FinishedMechRequestSkipRound            → CleanupTrackedMarketsRound
FinishedMechResponseTimeoutRound        → CleanupTrackedMarketsRound
FinishedWithChallengeTxRound            → TxSettlement
FinishedTransactionSubmissionRound      → CleanupTrackedMarketsRound

# Done
FinishedResolutionRound                 → ResetAndPauseRound
FinishedResetAndPauseRound              → IdentifyServiceOwnerRound
FinishedResetAndPauseErrorRound         → RegistrationRound
```

---

## Implementation Phases

### Phase 1: Shared Skills + Runnable Agent ✅ COMPLETE

**Goal**: A running agent that recovers funds and forwards excess — no resolver logic yet.

1. ✅ Shared skills (`identify_service_owner_abci`, `funds_forwarder_abci`, `omen_funds_recoverer_abci`) consumed as **third_party** packages — pushed from market-creator via `autonomy push-all`.
2. ✅ Created composed app (`market_resolver_abci`) chaining: `AgentRegistrationAbciApp` → `IdentifyServiceOwnerAbciApp` → `FundsForwarderAbciApp` → `OmenFundsRecovererAbciApp` → `TxSettlementAbciApp` → `ResetPauseAbciApp` + `TerminationAbciApp`
3. ✅ Created `packages/valory/agents/market_resolver/` — agent config with connection overrides (abci, ledger, p2p, http_server) and skill model overrides
4. ✅ Created `packages/valory/services/market_resolver/` — service config mirroring all overridable params with `${ENV_VAR:type:default}` syntax
5. ✅ Created repo scaffolding: `pyproject.toml`, `tox.ini`, `.coveragerc`, `.gitignore`, `Makefile` (with `run-agent`), `config-mapping.json`, `.github/workflows/`, `.claude/`
6. ✅ Packages synced and locked
7. ✅ Agent boots and cycles through Registration → IdentifyServiceOwner → FundsForwarder → OmenFundsRecoverer → TxSettlement → ResetPause via `make run-agent`

**Key decisions made during Phase 1:**
- Subgraph URLs use TheGraph gateway with API key in URL path (set via `.env`): Omen (`9fUV...bbz`), Realitio (`E7ym...Nh`), CT (`7s9r...Vp2`)
- Ledger config uses both `ethereum` (for `default_ledger` fallback) and `gnosis` entries in `ledger_apis`, both pointing to Gnosis RPC — follows market-creator pattern (to be cleaned up later)
- No `scripts/` directory — all tox references removed
- All shared skills are third_party (not dev) — repo only owns `market_resolver_abci`, agent, and service packages

### Phase 2: Build `market_resolution_manager_abci` ✅ COMPLETE (skeleton + business logic)

**Goal**: Fully implemented core skill with all rounds working.

**Status**: All behaviours implemented with business logic. Remaining TODOs within the code:
- `EvaluateAnswersBehaviour`: write `mech_requests` to SynchronizedData for MechInteract pickup (needs Phase 3 composition wiring)
- `BuildChallengesTxBehaviour`: parse `mech_responses` from MechInteract, build actual `submitAnswer` tx via Realitio contract API, check safe balance
- `CleanupTrackedMarketsBehaviour`: subgraph query may need adjustment based on actual Omen subgraph schema for question-level filtering
- All behaviours: unused imports to clean up during lint phase

#### MechInteract integration pattern

The resolver uses `mech_interact_abci` the same way market-creator does. The core skill does NOT contain Mech rounds — it routes to/from the external `MechInteractAbciApp` via degenerate final states and composition transition mappings.

**How it works (from market-creator):**

1. Core skill's `EvaluateAnswersRound` prepares `mech_requests` in SynchronizedData and emits `DONE` → transitions to `FinishedWithMechRequestRound` (degenerate)
2. Composition maps `FinishedWithMechRequestRound` → `MechVersionDetectionRound` (start of MechInteract)
3. MechInteract handles the full request/response cycle internally:
   - `MechVersionDetectionRound` → detects mech type
   - `MechRequestRound` → submits request on-chain (via TxSettlement)
   - `MechResponseRound` → polls for response
4. MechInteract final states route back to core skill via composition:
   - `FinishedMechResponseRound` → `BuildChallengesTxRound` (success — Mech answered)
   - `FinishedMechRequestSkipRound` → `CleanupTrackedMarketsRound` (no request needed)
   - `FinishedMechResponseTimeoutRound` → `CleanupTrackedMarketsRound` (Mech timed out)
   - `FinishedMechRequestRound` → `TxSettlement` (Mech request needs on-chain tx)
   - `FinishedMechPurchaseSubscriptionRound` → `TxSettlement` (subscription purchase)
   - `FailedMechInformationRound` → `MechVersionDetectionRound` (retry)
   - Various marketplace/legacy detection rounds → `MechRequestRound`

**Key models needed from MechInteract:**
- `MechResponseSpecs` — API specs for polling Mech responses
- `MechToolsSpecs` — API specs for querying available tools
- `MechsSubgraph` — subgraph for Mech marketplace
- `MechInteractParams` — Mech-specific params (`mech_interact_round_timeout_seconds`, `mech_interaction_sleep_time`, `use_mech_marketplace`, `mech_marketplace_config`, etc.)

**Key data flow:**
- Core skill writes `mech_requests` to SynchronizedData before transitioning to MechInteract
- MechInteract writes `mech_responses` to SynchronizedData before transitioning back
- `BuildChallengesTxRound` reads `mech_responses` to decide what to do

#### 2a. Scaffold

1. Create skill directory structure:

   ```text
   packages/valory/skills/market_resolution_manager_abci/
   ├── __init__.py
   ├── behaviours/
   │   ├── __init__.py
   │   ├── base.py              # MarketResolutionManagerBaseBehaviour
   │   ├── scan_markets.py
   │   ├── evaluate_answers.py
   │   ├── build_challenges.py
   │   └── cleanup_tracking.py
   ├── rounds.py                # FSM rounds + transition function
   ├── states/
   │   └── base.py              # Event enum
   ├── payloads.py
   ├── handlers.py
   ├── dialogues.py
   ├── models.py                # MarketResolutionManagerParams
   ├── fsm_specification.yaml
   └── skill.yaml
   ```

2. Define `Event` enum: `DONE`, `NONE`, `NO_MAJORITY`, `ROUND_TIMEOUT`
3. Define all 7 rounds (4 active + 3 degenerate) with transition function
4. Stub behaviours — each returns `NONE` (no-op pass-through)
5. Wire `fsm_specification.yaml`
6. Verify: `autonomy analyse fsm-specs`, `autonomy analyse docstrings`

**Checkpoint**: Skeleton skill that cycles through `ScanPendingMarkets → Cleanup → Reset` doing nothing.

#### 2b. `ScanPendingMarketsRound` — Subgraph Queries & Classification

1. Implement `ScanPendingMarketsBehaviour`:
   - Query Omen subgraph for pending markets from `watched_creator_addresses`
   - Query Realitio subgraph for latest answerers
   - Load `questions_db` from SynchronizedData
   - Classify each question (OK / NEEDS_EVALUATION / NEEDS_ANSWER / CHALLENGE_PENDING / WAITING)
   - Sort by finalization urgency, pick first actionable
   - Submit payload with selected question + updated DB
2. Add `questions_db` to `cross_period_persisted_keys`

**Checkpoint**: Scan round correctly identifies which question to process.

#### 2c. `EvaluateAnswersRound` — Mech Request Construction

1. Implement `EvaluateAnswersBehaviour`:
   - Read selected question from SynchronizedData
   - If `NEEDS_ANSWER`: build resolve Mech request (question text + resolution date)
   - If `NEEDS_EVALUATION`: build evaluation Mech request (question + current answer + context)
   - Write `mech_requests` to SynchronizedData (format must match what `mech_interact_abci` expects)
   - Emit `DONE` → `FinishedWithMechRequestRound` → composition routes to MechInteract

**Checkpoint**: Mech requests correctly formatted for both answering and evaluation.

#### 2d. `BuildChallengesTxRound` — Decision & Tx Construction

**Entry**: This round is entered after MechInteract returns (`FinishedMechResponseRound` → `BuildChallengesTxRound` via composition).

1. Implement `BuildChallengesTxBehaviour`:
   - Read `mech_responses` from SynchronizedData (written by MechInteract)
   - If `NEEDS_ANSWER`: build `submitAnswer` tx with initial bond
   - If Mech agrees: update DB → `VERIFIED_OK`, emit NONE
   - If Mech disagrees: update DB → `CHALLENGE_PENDING`, check cooldown timing
   - If cooldown elapsed or urgent: build `submitAnswer` tx with 2x bond, update DB → `ANSWERED_BY_US`
   - If cooldown not elapsed: emit NONE (wait)
   - Economic checks: bond cap, balance, escalation limit
2. Implement Realitio contract interaction for `submitAnswer`

**Checkpoint**: Full challenge logic with economic safety checks.

#### 2e. `CleanupTrackedMarketsRound` — DB Maintenance

1. Implement `CleanupTrackedMarketsBehaviour`:
   - Query subgraph for finalized questions (or use data from ScanPendingMarkets)
   - Remove entries from `questions_db` where `answerFinalizedTimestamp != null`
   - Submit updated DB

**Checkpoint**: Clean DB lifecycle — entries are created, updated, and eventually removed.

**Deliverable**: Fully implemented `market_resolution_manager_abci` skill, all rounds working.

### Phase 3: Integrate `market_resolution_manager_abci` into Composed App ✅ COMPLETE (simulated Mech)

**Status**: Core skill wired into composed app. Mech is simulated (hardcoded answer=NO). Agent scans mainnet markets, classifies them, simulated Mech evaluates, `sys.exit(1)` breakpoint before any challenge tx.

**Key decisions:**
- `market_resolution_manager_abci` is `is_abstract: true` (required for chaining)
- `CleanupTrackedMarketsRound` added to `initial_states` (entered from composition after TxSettlement or MechInteract)
- `FinishedWithChallengeTxRound` has `most_voted_tx_hash` as post-condition (required by TxSettlement)
- `questions_db` stored on `SharedState` (not SynchronizedData) — too heavy for Tendermint consensus
- Payloads are lightweight: market counts, selected market ID, action strings
- DB keyed by FPMM market ID (not Realitio question ID) — both stored in each entry
- Two Omen subgraph queries: pending (unanswered) + finalizing (answered but not yet finalized)

### Phase 3b: Real MechInteract Integration ✅ COMPLETE

**Status (2026-04-01)**: Full MechInteract integration working. Agent scans mainnet, requests Mech evaluation, builds real submitAnswer txs via Realitio contract, and submits through Safe multisend + TxSettlement.

**What was built:**
- `EvaluateAnswersRound` with custom `end_block`: `mech_requests` → MechInteract, `evaluation_result` → BuildAnswerTx (skip Mech for re-challenges)
- `BuildAnswerTxBehaviour` builds real Realitio `submitAnswer` calldata, wraps in multisend, computes Safe tx hash, sends to TxSettlement
- Mech prompt uses market title (human-readable question), nonce is uuid4
- Parses `resolve-market-jury-v1` output: `is_valid`, `is_determinable`, `has_occurred`
- `is_determinable=false` → retry with cooldown (24h for unanswered, before finalization for challenged)
- Balance check in scan: skip markets where required bond > safe balance
- Bond limit check: skip markets where required bond > `max_challenge_bond`
- First challenge: immediate (no cooldown). Re-challenge: cooldown based on last challenge timestamp
- Priority: challenges (closest to finalization first), then unanswered markets
- `AnswerStatus` enum: `NEEDS_ANSWER`, `NEEDS_VERIFICATION`, `TRUSTED_ANSWER`, `VERIFIED`, `CHALLENGE_PENDING`

**Composition routing:**
- `FinishedWithMechRequestRound` → `MechVersionDetectionRound` (start Mech)
- `FinishedMechResponseRound` → `BuildAnswerTxRound` (Mech responded)
- `FinishedWithChallengeTxRound` → TxSettlement (submit answer/challenge)
- TxSettlement → `MechResponseRound` (poll for Mech delivery after Mech request tx)
- `FinishedResolutionRound` → ResetAndPause

**Debug artifacts — REMOVED (2026-04-01):**

To re-enable DB file dumps for debugging, add to `base.py` `questions_db` setter:
```python
import time, json
from pathlib import Path
hour = time.strftime("%Y%m%d_%H%M%S")
path = Path.home() / "git" / "market-resolver" / f"answers_database_{hour}.json"
with open(path, "w") as f:
    json.dump(value, f, indent=2, default=str)
```

### Phase 3c: Production Hardening -- COMPLETE (2026-04-01)

All items completed:

1. **FinishedWithChallengeTxRound -> FinishedWithAnswerTxRound** -- renamed for consistency
2. **PostTransactionRound** -- routes based on tx_submitter: Mech request -> MechResponse, answer tx -> Cleanup
3. **Cleanup round** -- queries subgraph for finalized markets (answerFinalizedTimestamp_gt: 0 and _lt: now), removes from DB
4. **Debug artifacts removed** -- DB dump, debug box removed. Production logging kept.
5. **max_escalation_rounds removed** -- max_challenge_bond is sole risk control
6. **AnswerStatus enum** -- clean status lifecycle
7. **Bond/balance checks** -- only logged for actionable markets (not trusted/verified)
8. **Production params set** -- initial_answer_bond=1 xDAI, max_challenge_bond=16 xDAI, reset_pause=5min
9. **Propel deployment configured** -- all secrets and variables set
6. **Verify economic params match `.env`** — many params in config-mapping are missing from `.env` ("Environment variable not found. Skipping..."). Add to `.env` or accept defaults.

**Nice to have (deferred):**

- **Re-challenge optimization** — skip Mech when `entry.evaluation` exists and on-chain answer unchanged. Currently uses custom `end_block` in `EvaluateAnswersRound` (already implemented)
- **Bond redemption** — claim bonds from finalized questions where we answered correctly
- **Multi-market per cycle** — currently processes one market per cycle. Could batch multiple submitAnswer txs in a single multisend
- **Monitoring dashboard** — expose DB state via HTTP server for monitoring

### Phase 4: Unit Tests & Coverage

**Goal**: 100% statement + branch coverage for all dev packages.

1. Create test directory structure for `market_resolution_manager_abci`:
   - `tests/conftest.py`, `test_rounds.py`, `test_payloads.py`, `test_handlers.py`, `test_dialogues.py`, `test_models.py`
   - `tests/test_behaviours/` — one test file per behaviour
2. Create tests for `market_resolver_abci` composed skill:
   - `test_composition.py`, `test_behaviours.py`, `test_dialogues.py`, `test_handlers.py`, `test_models.py`
3. Update `tox.ini` test commands to include all dev skill coverage
4. All external boundaries mocked: subgraph, Mech, ledger, contracts
5. Test all FSM transitions, classification branches, cooldown timing, economic limits
6. Verify: `tox -e py3.11-linux` (100% coverage), all linters pass

**Deliverable**: Full test suite, green CI.

### Phase 5: Integration Testing & Hardening

**Goal**: End-to-end validation and edge case hardening.

1. Add integration test scenarios:
   - Full cycle with no pending markets
   - Cycle with trusted answer (skip)
   - Cycle with wrong answer → evaluate → challenge
   - Multi-cycle escalation war (up to `max_escalation_rounds`)
   - Cooldown timing across cycles
   - Answer changes after `VERIFIED_OK` → re-evaluation
   - Bond cap exceeded → stop challenging
2. Test DB persistence across periods (`cross_period_persisted_keys`)
3. Test composed app transitions end-to-end
4. Verify full CI: `tox -e py3.11-linux`

**Deliverable**: Production-ready service with comprehensive test coverage.
