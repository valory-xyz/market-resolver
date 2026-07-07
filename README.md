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

Each cycle walks through a recovery chain (forward excess funds, remove
LP, redeem CT positions, claim Realitio bonds), then the resolution
core (scan markets, evaluate via Mech, answer or challenge), then
pauses. Any step that needs to go on-chain routes through a shared
`TransactionSettlement` + `PostTransaction` multiplexer.

```
Registration
    |
    v
IdentifyServiceOwner
    |
    v
+-- FundsForwarder            --+
|                               |
+-- OmenFpmmRemoveLiquidity   --+-- recovery chain
|                               |  (each step: tx via TxSettlement,
+-- OmenCtRedeemTokens        --+   or no-op; PostTransaction routes
|                               |   back to the next step)
+-- OmenRealitioWithdrawBonds --+
    |
    v
MarketResolutionManager  --  pick one market; evaluate via MechInteract
    |                         if needed; submit answer / challenge
    v
ResetAndPause  --  wait, then next cycle
```

### Market Status Lifecycle (AnswerStatus enum)

| Status | Meaning |
|--------|---------|
| `NEEDS_ANSWER` | Not answered on-chain yet |
| `NEEDS_VERIFICATION` | Answered by an untrusted party, no Mech evaluation yet |
| `VERIFIED` | Mech agrees with the on-chain answer |
| `TRUSTED_ANSWER` | Answered by an address in `TRUSTED_ADDRESSES` |
| `TRANSACTION_PENDING` | Tx built / in-flight (initial answer or challenge) |

### Key Design Decisions

- **DB on SharedState, not SynchronizedData** -- the questions database is too heavy for Tendermint consensus. Each agent computes it deterministically from subgraph data.
- **One market per cycle** -- processes the highest-priority market each cycle (challenges first, then unanswered). ~3-5 min per cycle including Mech response time.
- **Bond-based risk control** -- `max_challenge_bond` caps the maximum xDAI the agent will put up. No `max_escalation_rounds` needed.
- **Mech reuse** -- when someone counter-challenges a market we already evaluated, the agent reuses the existing evaluation instead of making a new Mech request.

## Prepare the environment

- System requirements:

  - Python `>=3.10, <3.15`
  - [Tendermint](https://docs.tendermint.com/v0.34/introduction/install.html) `==0.34.19`
  - [uv](https://docs.astral.sh/uv/)
  - [Docker Engine](https://docs.docker.com/engine/install/)
  - [Docker Compose](https://docs.docker.com/compose/install/)

Clone and install:

      git clone https://github.com/valory-xyz/market-resolver.git

- Create development environment:

      uv sync --all-groups
      source .venv/bin/activate

- Configure the Open Autonomy framework:

      autonomy init --reset --author valory --remote --ipfs --ipfs-node "/dns/registry.autonolas.tech/tcp/443/https"

- Pull packages required to run the service:

      autonomy packages sync --update-packages

## Configuration

Description of selected variables to configure the service:

| Variable | Default | Description |
|------|---------|-------------|
| `ALL_PARTICIPANTS` | `["0x0000000000000000000000000000000000000000"]` | List of agent EOAs in the service. |
| `CT_SUBGRAPH_URL` | `https://gateway.thegraph.com/api/api-key/subgraphs/id/7s9rGBffUTL8kDZuxvvpuc46v44iuDarbrADBFw5uVp2` | Conditional Tokens subgraph. |
| `EXPECTED_SERVICE_OWNER_ADDRESS` | `0x0000000000000000000000000000000000000000` | Address used by `IdentifyServiceOwner` to verify the fund-forwarder target. |
| `GNOSIS_LEDGER_RPC` | `https://rpc.gnosischain.com` | Gnosis Chain RPC endpoint. The public default is rate-limited; use a private RPC in production. |
| `INITIAL_ANSWER_BOND` | `10000000000000000` (0.01 xDAI) | Bond for the first answer on an unanswered market. Realitio doubles per escalation.  |
| `MAX_CHALLENGE_BOND` | `40000000000000000` (0.04 xDAI) | Cap on challenge escalation; agent stops bidding once the next bond would exceed this. |
| `OMEN_SUBGRAPH_URL` | `https://gateway.thegraph.com/api/api-key/subgraphs/id/9fUVQpFwzpdWS9bq5WkAnmKbNNcoBwatMR4yZq81pbbz` | Omen subgraph. |
| `ON_CHAIN_SERVICE_ID` | `null` | Olas registry service id. |
| `REALITIO_SUBGRAPH_URL` | `https://gateway.thegraph.com/api/api-key/subgraphs/id/E7ymrCnNcQdAAgLbdFWzGE5mvr5Mb5T9VfT43FqA7bNh` | Realitio subgraph. |
| `SAFE_CONTRACT_ADDRESS` | `0x0000000000000000000000000000000000000000` | Gnosis Safe multisig controlled by the agents. |
| `STORE_PATH` | `/data` | Directory for the kv_store SQLite database that tracks fired mech requests ("have I already asked about this market?"). **Must be a persistent volume**: if the store is wiped, every market's request history is re-seeded from the subgraph, and requests fired after the migration to the off-chain path are not in the subgraph, so those markets would be re-requested (paid) up to `max_mech_retries` times. On Propel the default `/data` is a PVC and works as-is. On bare metal, point it at a persisted directory the agent user can write to; the agent creates the directory on first run and fails with `PermissionError` if it can't. |
| `TRUSTED_ADDRESSES` | `[]` (fill in) | Answerers whose answers we treat as authoritative. |
| `WATCHED_CREATOR_ADDRESSES` | `[]` (fill in) | Market creators whose markets we scan. |

## License

Apache License 2.0
