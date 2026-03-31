# CLAUDE.md — Market Resolver

## Project Overview

Market Resolver is an **autonomous service** built on the [Open Autonomy](https://stack.olas.network/open-autonomy/) framework. It resolves prediction markets on Gnosis Chain by answering Realitio questions, challenging incorrect answers, and recovering locked funds (bonds, conditional tokens, LP tokens).

**Sibling repo:** [valory-xyz/market-creator](https://github.com/valory-xyz/market-creator) — creates the markets this service resolves. Local clone at `/home/jose/git/market-creator`.
**Framework reference:** [valory-xyz/trader](https://github.com/valory-xyz/trader) (develop branch) — follow the same patterns for CI, tox, and package structure.

## Plans & Domain Documentation (in market-creator repo)

These files live in the market-creator repository and are the **single source of truth** — do not copy them here.

### Plans (`/home/jose/git/market-creator/.claude/plans/`)

- **`service_split.md`** — Architecture for splitting into market-creator + market-resolver. Contains composed FSM specs, transition mappings, and round assignments for both services.
- **`omen_funds_recoverer.md`** — The shared `omen_funds_recoverer_abci` skill that both services compose for fund recovery (LP tokens, conditional tokens, Realitio bonds).
- **`claim_winnings.md`** — Realitio `claimWinnings` implementation plan. Bond claiming logic used by the recovery skill.

### Domain docs (`/home/jose/git/market-creator/.claude/docs/`)

- **`omen_lifecycle.md`** — Full Omen prediction market lifecycle: contracts, phases, the two independent value flows (conditional tokens vs Realitio bonds), and FSM round mapping.
- **`ct_omen_realitio.md`** — Tutorial on Conditional Tokens framework, Omen, and Realitio: how they work together, contract interactions at each stage, fund recovery mechanisms.

### Local plan (`.claude/plans/`)

- **`market_resolver.md`** — The resolver-specific watchdog FSM: scan, evaluate, challenge, cleanup. Round details, fraud detection strategy, configuration parameters.

## What the Service Does

The service autonomously resolves Omen prediction markets on Gnosis Chain. Each period (cycle) it:

1. **Recovers locked funds** (`omen_funds_recoverer_abci`) — claims bonds, redeems conditional tokens, removes liquidity from resolved markets
2. **Gets pending questions** — queries Realitio for questions needing answers
3. **Requests Mech** — delegates question evaluation to a Mech agent (AI oracle) via `mech_interact_abci`
4. **Answers questions** — submits Mech responses as Realitio answers
5. **Challenges incorrect answers** — monitors existing answers and submits challenges when appropriate

## Repository Structure

```text
packages/valory/
├── contracts/
│   └── ... (third-party synced contracts)
├── skills/
│   ├── market_resolver_abci/             # Core skill: question answering & challenging FSM
│   └── ... (third-party synced skills)
├── agents/                               # Agent configurations
└── services/                             # Service configurations
```

### What you own vs. what is synced

Package ownership is defined in `packages/packages.json`:

- **`dev`** section: project-specific packages (owned by this repo, committed to git)
- **`third_party`** section: dependencies synced from IPFS via `autonomy packages sync --all`. Do not modify these directly — they are not committed to git.

## Development Commands

### Prerequisites

- Python 3.10–3.14
- [Poetry](https://python-poetry.org/)
- [tox](https://tox.wiki/)
- [tomte](https://github.com/valory-xyz/tomte) (installed via tox deps)

### Setup

```bash
poetry install --no-root
```

### Syncing third-party packages

Before running tests, sync all AEA packages from IPFS:

```bash
autonomy init --reset --author ci --remote --ipfs --ipfs-node "/dns/registry.autonolas.tech/tcp/443/https"
autonomy packages sync --all
```

This is done automatically by tox test environments.

### Running tests

```bash
# Run all unit tests (Linux, Python 3.11)
tox -e py3.11-linux

# Convenience: run all unit tests in one pytest invocation with combined coverage
tox -e unit-tests

# Recreate tox virtualenv (clear cache)
tox -e py3.11-linux -r
```

Test environments follow the pattern `py{version}-{platform}` where platform is `linux`, `win`, or `darwin`.

### Formatting (auto-fix)

```bash
tox -e black && tox -e isort
```

### Locking packages

After modifying any dev package, update the package hashes:

```bash
autonomy packages lock
```

### Linting & static analysis

```bash
tox -e black-check    # Code formatting check
tox -e isort-check    # Import sorting check
tox -e flake8         # Linting
tox -e mypy           # Type checking
tox -e pylint         # Pylint
tox -e darglint       # Docstring linting
tox -e bandit         # Security linting
tox -e safety         # Dependency vulnerability check
tox -e liccheck       # License compliance check
```

### Package integrity

```bash
tox -e check-hash           # Verify package hashes
tox -e check-packages       # Validate package structure
tox -e check-abciapp-specs  # Validate FSM specifications
```

## Testing

### Coverage enforcement

All test environments enforce **100% statement + branch coverage** via `--cov-fail-under=100`. Coverage is configured in `.coveragerc`.

### Test conventions

- All external boundaries are mocked (`MagicMock` / `patch`): ledger, subgraph, mech/LLM, contract wrappers
- Shared fixtures in `conftest.py` files
- No network/RPC calls — fully deterministic
- Tests assert on public outcomes (payloads, events), not implementation details

### Adding new tests

Place tests in the `tests/` directory of each package. Follow existing patterns in `conftest.py` for mocked context, synchronized data, and behaviour builders.

## CI

CI workflow: `.github/workflows/common_checks.yml`

- Cross-platform matrix: Ubuntu, macOS, Windows
- Python versions: 3.10–3.14
- Uses `tomte[tox]==0.6.2` for test orchestration

## Key Gotchas

### `packages/valory/__init__.py` must exist

This file is required for Python to resolve `packages.valory.*` imports from the local directory rather than site-packages. Without it, Windows CI fails with `ModuleNotFoundError` because Python falls back to namespace package resolution from installed wheels.

### `PYTHONPATH` uses `{env:PWD:%CD%}`

The tox config sets `PYTHONPATH={env:PWD:%CD%}` for cross-platform compatibility (`PWD` on Unix, `%CD%` on Windows). This is the standard pattern from the trader repo — do not change it to `{toxinidir}`.

### Third-party packages are not committed

Packages synced via `autonomy packages sync --all` are fetched from IPFS at test time. They appear in `packages/` but are in `.gitignore`. Do not commit them.

### liccheck and setuptools

`setuptools` is added to `[Authorized Packages]` in `tox.ini` because its license metadata (`UNKNOWN`) is not auto-detected by liccheck.

### tox cache

If you get stale dependency errors, clear tox cache: `rm -rf .tox` or use `tox -e <env> -r`.

## FSM Change Discipline

FSM definitions are tightly coupled across multiple files. When modifying events, rounds, or transitions, **all** of the following must stay in sync:

1. **`states/base.py`** — `Event` enum members
2. **`rounds.py`** — `transition_function` entries and the class docstring (transition table)
3. **`fsm_specification.yaml`** — `alphabet_in` and `transition_func` entries
4. **Test files** — parametrized transition test cases

After any FSM change, run:

```bash
autonomy analyse fsm-specs --package packages/valory/skills/market_resolver_abci
autonomy analyse docstrings
autonomy analyse handlers
```

## Open Autonomy Concepts

- **ABCI App**: FSM-based application where agents reach consensus on state transitions via Tendermint
- **Round**: A consensus round where agents submit payloads and vote
- **Behaviour**: Logic executed by each agent during a round (collects data, builds transactions)
- **Skill**: An AEA skill containing rounds, behaviours, handlers, payloads, and models
- **Composed app**: wires sub-skills together (registration, core logic, transaction settlement, mech interaction, reset/pause, termination)
- **`autonomy packages sync --all`**: Fetches all third-party dependencies declared in `packages.json` from IPFS

## Third-party Dependency Repositories

| Repository | What it provides |
|------------|-----------------|
| [open-autonomy](https://github.com/valory-xyz/open-autonomy) | Core framework: abstract_round_abci, registration, transaction_settlement, reset_pause, termination |
| [open-aea](https://github.com/valory-xyz/open-aea) | AEA framework: protocols, connections, base contracts (gnosis_safe, multisend, service_registry) |
| [mech-interact](https://github.com/valory-xyz/mech-interact) | mech_interact_abci skill, mech/mech_mm/ierc1155 contracts |
| [trader](https://github.com/valory-xyz/trader) | realitio, realitio_proxy, conditional_tokens contracts |

## Commit Conventions

Follow conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`
