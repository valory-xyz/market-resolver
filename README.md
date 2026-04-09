# Market Resolver

An autonomous service built on the [Open Autonomy](https://stack.olas.network/open-autonomy/) framework that resolves prediction markets on Gnosis Chain.

## What it does

Market Resolver handles the resolution side of Omen prediction markets:

- **Answers Realitio questions** -- fetches pending questions, evaluates them via Mech (AI oracle), and submits answers on-chain
- **Challenges incorrect answers** -- monitors existing answers from untrusted answerers and submits challenges when the Mech disagrees
- **Re-challenges** -- detects when someone flips an answer we previously set, reuses existing Mech evaluation to challenge back immediately (no wasted Mech credits)
- **Recovers locked funds** -- claims Realitio bonds, redeems conditional token positions, and removes liquidity from resolved markets
- **Forwards excess funds** -- sweeps xDAI above a configurable threshold back to the service owner

This is a sibling service to [market-creator](https://github.com/valory-xyz/market-creator), which handles market creation and LP management.

## Architecture

The service is composed of 8 chained ABCI apps:

```
Registration
    |
IdentifyServiceOwner -- identify the service owner for fund forwarding
    |
FundsForwarder -- sweep excess xDAI to service owner
    |
OmenFundsRecoverer -- remove liquidity, redeem positions, claim bonds
    |
MarketResolutionManager -- core logic (scan, evaluate, answer/challenge)
    |
MechInteract -- request/receive AI evaluation from Mech marketplace
    |
TransactionSettlement -- submit Safe multisig transactions on-chain
    |
    +---> PostTransaction -- route based on tx type:
    |         |--- Mech request tx --> MechResponse (poll for delivery)
    |         |--- Answer/challenge tx --> Cleanup
    |
    +---> CleanupTrackedMarkets -- remove finalized markets from DB
    |
ResetAndPause -- wait 5 min, then restart cycle
```

### Core Skill: MarketResolutionManager

The core FSM manages the market lifecycle:

```
ScanMarkets -- query Omen subgraph for pending + finalizing markets
    |
    |--- DONE (actionable market found) --> EvaluateAnswers
    |--- NONE (nothing to do) --> Cleanup
    |
EvaluateAnswers -- decide: request Mech or reuse existing evaluation
    |
    |--- DONE (needs Mech) --> MechInteract (external)
    |--- NONE (has evaluation) --> BuildAnswerTx (skip Mech)
    |
BuildAnswerTx -- build Realitio submitAnswer tx via Safe multisend
    |
    |--- DONE (tx built) --> TxSettlement (external)
    |--- NONE (verified/skipped) --> Cleanup
    |
PostTransaction -- route after TxSettlement based on tx_submitter
    |
    |--- MECH_REQUEST_DONE --> MechResponse (poll for delivery)
    |--- ANSWER_TX_DONE --> Cleanup
    |
CleanupTrackedMarkets -- remove finalized markets from DB
    |
    --> ResetAndPause
```

### Market Status Lifecycle (AnswerStatus enum)

```
New unanswered market              --> NEEDS_ANSWER
New market with untrusted answer   --> NEEDS_VERIFICATION
Mech agrees with untrusted answer  --> VERIFIED (no action needed)
Mech disagrees / answer tx built   --> CHALLENGE_PENDING
Answered by our safe or trusted addr --> TRUSTED_ANSWER
```

### Key Design Decisions

- **DB on SharedState, not SynchronizedData** -- the questions database is too heavy for Tendermint consensus. Each agent computes it deterministically from subgraph data.
- **One market per cycle** -- processes the highest-priority market each cycle (challenges first, then unanswered). ~3-5 min per cycle including Mech response time.
- **Bond-based risk control** -- `max_challenge_bond` caps the maximum xDAI the agent will put up. No `max_escalation_rounds` needed.
- **Mech reuse** -- when someone counter-challenges a market we already evaluated, the agent reuses the existing evaluation instead of making a new Mech request.

## Prerequisites

- Python 3.10-3.14
- [Poetry](https://python-poetry.org/)
- [tox](https://tox.wiki/)

## Setup

```bash
poetry install --no-root
```

## Development

```bash
# Sync third-party packages from IPFS
autonomy init --reset --author valory --remote --ipfs --ipfs-node "/dns/registry.autonolas.tech/tcp/443/https"
autonomy packages sync --all

# Run tests
tox -e py3.11-linux

# Format code
tox -e isort && tox -e black

# Lint
tox -e flake8 && tox -e mypy && tox -e pylint

# Verify FSM specs
autonomy analyse fsm-specs --package packages/valory/skills/market_resolution_manager_abci
autonomy analyse fsm-specs --package packages/valory/skills/market_resolver_abci

# Lock package hashes
autonomy packages lock
```

## Configuration

Key parameters (configurable via service.yaml or environment variables):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `initial_answer_bond` | 1 xDAI | Bond for first answer on unanswered markets |
| `max_challenge_bond` | 16 xDAI | Maximum bond the agent will put up |
| `reset_pause_duration` | 300 (5 min) | Pause between cycles |
| `max_mech_retries` | 10 | Max Mech requests per market |
| `mech_tool_resolve_market` | resolve-market-jury-v1 | Mech tool for evaluation |
| `watched_creator_addresses` | [] | Market creators to monitor |
| `trusted_addresses` | [] | Answerers whose answers we trust |

## License

Apache License 2.0
