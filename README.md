# Market Resolver

An autonomous service built on the [Open Autonomy](https://docs.autonolas.network/) framework that resolves prediction markets on Gnosis Chain.

## What it does

Market Resolver handles the resolution side of Omen prediction markets:

- **Answers Realitio questions** — fetches pending questions, evaluates them via Mech (AI oracle), and submits answers on-chain
- **Challenges incorrect answers** — monitors existing answers and submits challenges when appropriate
- **Recovers locked funds** — claims Realitio bonds, redeems conditional token positions, and removes liquidity from resolved markets

This is a sibling service to [market-creator](https://github.com/valory-xyz/market-creator), which handles market creation and LP management.

## Prerequisites

- Python 3.10–3.14
- [Poetry](https://python-poetry.org/)
- [tox](https://tox.wiki/)

## Setup

```bash
poetry install --no-root
```

## Development

```bash
# Sync third-party packages from IPFS
autonomy init --reset --author ci --remote --ipfs --ipfs-node "/dns/registry.autonolas.tech/tcp/443/https"
autonomy packages sync --all

# Run tests
tox -e py3.11-linux

# Format code
tox -e isort && tox -e black

# Lint
tox -e flake8 && tox -e mypy && tox -e pylint
```

## License

Apache License 2.0
