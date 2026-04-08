#!/bin/bash
# scripts/start_fork.sh — Start a local Anvil fork of Ethereum mainnet.
#
# Prerequisites:
#   Install Foundry: curl -L https://foundry.paradigm.xyz | bash && foundryup
#
# Usage:
#   export ETH_RPC_URL=https://mainnet.infura.io/v3/YOUR_KEY
#   bash scripts/start_fork.sh
#
# Optional env vars:
#   FORK_PORT          — port to listen on (default: 8545)
#   FORK_BLOCK_NUMBER  — pin to a specific block (default: latest)
#   FORK_ACCOUNTS      — number of funded test accounts (default: 10)
#   FORK_BALANCE       — ETH balance per account (default: 10000)

set -euo pipefail

: "${ETH_RPC_URL:?ETH_RPC_URL must be set to a mainnet RPC URL}"

PORT="${FORK_PORT:-8545}"
BLOCK="${FORK_BLOCK_NUMBER:-latest}"
ACCOUNTS="${FORK_ACCOUNTS:-10}"
BALANCE="${FORK_BALANCE:-10000}"

echo "Starting Anvil fork..."
echo "  RPC:   ${ETH_RPC_URL}"
echo "  Block: ${BLOCK}"
echo "  Port:  ${PORT}"

anvil \
    --fork-url "${ETH_RPC_URL}" \
    --fork-block-number "${BLOCK}" \
    --port "${PORT}" \
    --accounts "${ACCOUNTS}" \
    --balance "${BALANCE}"
