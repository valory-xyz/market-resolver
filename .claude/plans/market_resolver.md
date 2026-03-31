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
  0. ScanMarketsRound              # scan pending markets, classify answers
  1. EvaluateAnswersRound          # prepare Mech request for one unverified answer
  2. BuildChallengesTxRound        # after Mech: answer/challenge or mark OK
  3. CleanupTrackingRound          # purge finalized markets from DB
  4. FinishedWithMechRequestRound  (degenerate → MechInteract)
  5. FinishedWithChallengeTxRound  (degenerate → TxSettlement)
  6. FinishedResolutionRound       (degenerate → ResetPause)

Initial state: ScanMarketsRound

Transition function:
  ScanMarketsRound:
    DONE          → EvaluateAnswersRound     # found unverified answer to evaluate
    NONE          → CleanupTrackingRound     # all answers OK or no pending markets
    NO_MAJORITY   → ScanMarketsRound
    ROUND_TIMEOUT → ScanMarketsRound

  EvaluateAnswersRound:
    DONE          → FinishedWithMechRequestRound  # → MechInteract → BuildChallenges
    NONE          → CleanupTrackingRound          # nothing to evaluate after filtering
    NO_MAJORITY   → CleanupTrackingRound
    ROUND_TIMEOUT → CleanupTrackingRound

  BuildChallengesTxRound:                         # entry point after MechInteract returns
    DONE          → FinishedWithChallengeTxRound  # → TxSettlement → Cleanup
    NONE          → CleanupTrackingRound          # Mech agreed, no challenge needed
    NO_MAJORITY   → CleanupTrackingRound
    ROUND_TIMEOUT → CleanupTrackingRound

  CleanupTrackingRound:
    DONE          → FinishedResolutionRound
    NO_MAJORITY   → FinishedResolutionRound
    ROUND_TIMEOUT → FinishedResolutionRound
```

### Flow Diagram

```
ScanMarkets ──DONE──► EvaluateAnswers ──DONE──► [MechInteract]
    │                       │                         │
    │──NONE──┐              │──NONE──┐                ▼
             │                       │         BuildChallengesTx
             │                       │                │
             │                       │     ──DONE──► [TxSettlement]
             │                       │                │
             ▼                       ▼                ▼
         CleanupTracking ◄────────────────────────────┘
             │
             ▼
       [FinishedResolution] → ResetPause
```

---

## Round Details

### `ScanMarketsRound`

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

**Classification logic** (questions sorted by closest to finalization first):

```
for each pending question (sorted by currentAnswerTimestamp + timeout, ascending):

    if no answer yet:
        → status = NEEDS_ANSWER (needs Mech + initial answer)

    elif latest_answerer == our_safe OR latest_answerer in trusted_addresses:
        → status = OK (trusted answer, skip)

    elif question_id in questions_db AND db_entry.status == VERIFIED_OK
         AND db_entry.on_chain_answer == current_on_chain_answer:
        → status = OK (already verified by Mech in a previous cycle)

    elif question_id in questions_db AND db_entry.status == VERIFIED_OK
         AND db_entry.on_chain_answer != current_on_chain_answer:
        → status = NEEDS_EVALUATION (answer changed since we verified — re-check)
        → invalidate DB entry

    elif question_id in questions_db AND db_entry.status == CHALLENGE_PENDING:
        → check if cooldown has elapsed (see Challenge Timing below)
        → if elapsed: status = NEEDS_CHALLENGE
        → if not elapsed: status = WAITING (skip this cycle)

    else:
        → status = NEEDS_EVALUATION (first time seeing this non-trusted answer)
```

**Pick first actionable**: Select the first question with status `NEEDS_ANSWER`, `NEEDS_EVALUATION`, or `NEEDS_CHALLENGE` (in finalization-urgency order). Only one question is processed per cycle to avoid missing urgent ones.

**Events**: `DONE` (found one question to process), `NONE` (all OK or no pending markets)

### `EvaluateAnswersRound`

**Purpose**: Build a Mech request for the selected question.

**Logic**:
1. Read the selected question from SynchronizedData
2. If `NEEDS_ANSWER`: build standard Mech request (question text, resolution date)
3. If `NEEDS_EVALUATION`: build evaluation request (question text + current answer + bond + market context)
4. Store as `mech_requests` → route to MechInteract

**Events**: `DONE` (mech request ready), `NONE` (nothing to evaluate)

### `BuildChallengesTxRound`

**Purpose**: After MechInteract returns, decide action based on Mech response.

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

### `CleanupTrackingRound`

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
        "status": "NEEDS_EVALUATION" | "VERIFIED_OK" | "CHALLENGE_PENDING" | "ANSWERED_BY_US",
        "on_chain_answer": "0x00...00",           # answer when last examined
        "on_chain_bond": 10000000000000000000,     # bond in wei when last examined
        "last_answerer": "0xabc...",               # who posted the current answer
        "detected_at": 1711900000,                 # timestamp when we first saw this state
        "mech_answer": "0x00...01",                # Mech's answer (if evaluated). null if not yet evaluated
        "mech_confidence": 0.91,                   # Mech confidence (if evaluated). null if not yet evaluated
        "market_closing_timestamp": 1712000000,    # openingTimestamp of the market (when question opens)
        "realitio_timeout": 86400,                 # Realitio timeout for this question (seconds)
    }
}
```

### Entry Lifecycle

```
First seen (non-trusted answer) → NEEDS_EVALUATION
  ↓ Mech agrees                → VERIFIED_OK (stays until finalized or answer changes)
  ↓ Mech disagrees             → CHALLENGE_PENDING (waits for cooldown, then challenged)
  ↓ We answer/challenge        → ANSWERED_BY_US (until someone re-challenges)
  ↓ Someone re-challenges us   → NEEDS_EVALUATION (re-evaluate with Mech)
  ↓ Question finalizes         → removed from DB (CleanupTrackingRound)
```

### State Transitions

| Current Status | Trigger | New Status |
|---|---|---|
| (new entry) | Non-trusted answer seen | `NEEDS_EVALUATION` |
| `NEEDS_EVALUATION` | Mech agrees | `VERIFIED_OK` |
| `NEEDS_EVALUATION` | Mech disagrees | `CHALLENGE_PENDING` |
| `CHALLENGE_PENDING` | Cooldown elapsed + tx submitted | `ANSWERED_BY_US` |
| `VERIFIED_OK` | On-chain answer changed | `NEEDS_EVALUATION` |
| `ANSWERED_BY_US` | We're still latest answerer | skip (OK) |
| `ANSWERED_BY_US` | Someone re-challenged | `NEEDS_EVALUATION` |
| any | `answerFinalizedTimestamp != null` | removed |

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

Everything else OK → `NONE` → CleanupTracking

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

Answer finalizes after timeout. Next cycle: CleanupTracking removes q5.

---

## Configuration Parameters

```yaml
# Watchdog scope
watched_creator_addresses: []          # market creator safes to monitor (required)
trusted_addresses: []                  # trusted answerers (our safe auto-included)

# Answering (for unanswered questions)
initial_answer_bond: 10000000000000000000   # 10 xDAI in wei
mech_tool_resolve: "resolve-market-reasoning-gpt-4.1"

# Challenging
challenge_cooldown_fraction: 0.25      # wait this fraction of realitio_timeout before challenging
challenge_urgency_buffer: 3600         # 1 hour — challenge immediately if less than this remains
challenge_confidence_threshold: 0.85   # minimum Mech confidence to challenge
max_challenge_bond: 100000000000000000000  # 100 xDAI absolute cap in wei
mech_tool_evaluate: "evaluate-answer-reasoning-gpt-4.1"

# Escalation limits
max_escalation_rounds: 5               # stop re-challenging after this many rounds
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
FinishedWithoutRecoveryTxRound          → ScanMarketsRound

# Watchdog
FinishedWithMechRequestRound            → MechVersionDetectionRound
FinishedMechResponseRound               → BuildChallengesTxRound
FinishedMechRequestSkipRound            → CleanupTrackingRound
FinishedMechResponseTimeoutRound        → CleanupTrackingRound
FinishedWithChallengeTxRound            → TxSettlement
FinishedTransactionSubmissionRound      → CleanupTrackingRound

# Done
FinishedResolutionRound                 → ResetAndPauseRound
FinishedResetAndPauseRound              → IdentifyServiceOwnerRound
FinishedResetAndPauseErrorRound         → RegistrationRound
```

---

## Implementation Phases

### Phase 1: Shared Skills + Runnable Agent

**Goal**: A running agent that recovers funds and forwards excess — no resolver logic yet.

1. Shared skills (`identify_service_owner_abci`, `funds_forwarder_abci`, `omen_funds_recoverer_abci`) consumed as **third_party** packages — must be `autonomy push`-ed from market-creator first.
2. Create a minimal composed app (`market_resolver_abci`) that chains:
   - `AgentRegistrationAbciApp` → `IdentifyServiceOwnerAbciApp` → `FundsForwarderAbciApp` → `OmenFundsRecovererAbciApp` → `ResetPauseAbciApp` + `TerminationAbciApp`
   - No `MarketResolutionManagerAbciApp` yet — recovery feeds directly into reset
3. Create `packages/valory/agents/market_resolver/` — agent config
4. Create `packages/valory/services/market_resolver/` — service config
5. Update `.coveragerc` source paths, `tox.ini` (`SERVICE_SPECIFIC_PACKAGES`, test commands), `.gitignore`
6. Run `autonomy packages sync --all && autonomy packages lock`
7. Verify: `tox -e check-packages`, `tox -e check-hash`, `tox -e check-abciapp-specs`, `tox -e check-handlers`, linters pass
8. Deploy and run locally via `make run-agent` — confirm the agent cycles through Registration → Owner → Funds → Recovery → Reset

**Deliverable**: Running agent that recovers Omen funds and sweeps excess to service owner. Green CI.

### Phase 2: Build `market_resolution_manager_abci`

**Goal**: Fully implemented core skill with all rounds working.

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

**Checkpoint**: Skeleton skill that cycles through `ScanMarkets → Cleanup → Reset` doing nothing.

#### 2b. `ScanMarketsRound` — Subgraph Queries & Classification

1. Implement `ScanMarketsBehaviour`:
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
   - Store `mech_requests` for MechInteract

**Checkpoint**: Mech requests correctly formatted for both answering and evaluation.

#### 2d. `BuildChallengesTxRound` — Decision & Tx Construction

1. Implement `BuildChallengesTxBehaviour`:
   - Read Mech response from SynchronizedData
   - If `NEEDS_ANSWER`: build `submitAnswer` tx with initial bond
   - If Mech agrees: update DB → `VERIFIED_OK`, emit NONE
   - If Mech disagrees: update DB → `CHALLENGE_PENDING`, check cooldown timing
   - If cooldown elapsed or urgent: build `submitAnswer` tx with 2x bond, update DB → `ANSWERED_BY_US`
   - If cooldown not elapsed: emit NONE (wait)
   - Economic checks: bond cap, balance, escalation limit
2. Implement Realitio contract interaction for `submitAnswer`

**Checkpoint**: Full challenge logic with economic safety checks.

#### 2e. `CleanupTrackingRound` — DB Maintenance

1. Implement `CleanupTrackingBehaviour`:
   - Query subgraph for finalized questions (or use data from ScanMarkets)
   - Remove entries from `questions_db` where `answerFinalizedTimestamp != null`
   - Submit updated DB

**Checkpoint**: Clean DB lifecycle — entries are created, updated, and eventually removed.

**Deliverable**: Fully implemented `market_resolution_manager_abci` skill, all rounds working.

### Phase 3: Integrate `market_resolution_manager_abci` into Composed App

**Goal**: Wire the core skill into the running agent from Phase 1.

1. Update `market_resolver_abci/composition.py`:
   - Insert `MarketResolutionManagerAbciApp` between `OmenFundsRecovererAbciApp` and `TransactionSettlementAbciApp`
   - Add `MechInteractAbciApp` to the chain
   - Wire all transition mappings (see "Composed App Transition Mapping" above)
2. Update agent and service configs with new parameters (`watched_creator_addresses`, `trusted_addresses`, challenge config, etc.)
3. Verify: `autonomy analyse fsm-specs`, `tox -e check-abciapp-specs`, `tox -e analyse-service`, `tox -e check-handlers`
4. Deploy and run locally via `make run-agent` — confirm full cycle: Registration → Owner → Funds → Recovery → Scan → Evaluate → Challenge → Reset

**Deliverable**: Fully integrated agent with resolver logic active.

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
