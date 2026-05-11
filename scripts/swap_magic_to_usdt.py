"""
scripts/swap_magic_to_usdt.py

Swaps all MAGIC in the Arbitrum wallet to USDT via SushiSwap V2.
Route: MAGIC -> WETH -> USDT  (uses the deep SushiSwap MAGIC/WETH pool)

Usage:
    python scripts/swap_magic_to_usdt.py           # dry-run (shows quote only)
    python scripts/swap_magic_to_usdt.py --execute  # actually sends the tx
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv

load_dotenv()
from eth_abi import encode as abi_encode
from web3 import Web3

RPC_URL = os.getenv("ARB_RPC_URL", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

MAGIC = Web3.to_checksum_address("0x539bdE0d7Dbd336b79148AA742883198BBF60342")
WETH = Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1")
USDT = Web3.to_checksum_address("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9")
ROUTER = Web3.to_checksum_address("0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506")  # SushiSwap V2

SLIPPAGE = 0.02  # 2% slippage tolerance
CHAIN_ID = 42161  # Arbitrum One


def main(execute: bool) -> None:
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    acct = w3.eth.account.from_key(PRIVATE_KEY)
    wallet = acct.address
    print(f"Wallet: {wallet}")

    # ── Balances ────────────────────────────────────────────────────────
    def balanceof(token, decimals):
        raw = w3.eth.call(
            {"to": token, "data": "0x70a08231" + wallet.lower().replace("0x", "").zfill(64)}
        )
        return int.from_bytes(bytes(raw[:32]), "big") / 10**decimals

    magic_bal = balanceof(MAGIC, 18)
    usdt_bal = balanceof(USDT, 6)
    eth_bal = w3.eth.get_balance(wallet) / 1e18

    print(f"Balances: {magic_bal:.4f} MAGIC  |  {usdt_bal:.4f} USDT  |  {eth_bal:.6f} ETH")

    if magic_bal < 0.01:
        print("No MAGIC to swap.")
        return

    amount_in = int(magic_bal * 1e18)

    # ── Quote ────────────────────────────────────────────────────────────
    # getAmountsOut(uint256 amountIn, address[] path) -> uint256[]
    path = [MAGIC, WETH, USDT]
    calldata = "0xd06ca61f" + abi_encode(["uint256", "address[]"], [amount_in, path]).hex()
    raw = w3.eth.call({"to": ROUTER, "data": calldata})
    # returns (uint256[], offset, length, val0, val1, val2)
    amounts = [
        int.from_bytes(bytes(raw[i * 32 : (i + 1) * 32]), "big") for i in range(len(raw) // 32)
    ]
    # amounts[3] = MAGIC in, amounts[4] = WETH mid, amounts[5] = USDT out
    usdt_out = amounts[5] / 1e6 if len(amounts) >= 6 else 0

    if usdt_out <= 0:
        # fallback: last 3 values
        usdt_out = amounts[-1] / 1e6

    min_out = int(usdt_out * (1 - SLIPPAGE) * 1e6)
    eff_price = usdt_out / magic_bal

    print("\nSwap quote:")
    print(f"  In:  {magic_bal:.4f} MAGIC")
    print(f"  Out: {usdt_out:.4f} USDT  (${eff_price:.5f}/MAGIC)")
    print(f"  Min: {min_out/1e6:.4f} USDT  (2% slippage guard)")
    print("  Route: MAGIC -> WETH -> USDT via SushiSwap V2")

    if not execute:
        print("\nDRY RUN -- pass --execute to send the transaction")
        return

    print("\nExecuting swap...")

    # ── Approve MAGIC ────────────────────────────────────────────────────
    # Check current allowance
    allow_data = (
        "0xdd62ed3e"
        + wallet.lower().replace("0x", "").zfill(64)
        + ROUTER.lower().replace("0x", "").zfill(64)
    )
    allow_raw = w3.eth.call({"to": MAGIC, "data": allow_data})
    current_allowance = int.from_bytes(bytes(allow_raw[:32]), "big")

    if current_allowance < amount_in:
        print("  Approving MAGIC for SushiSwap router...")
        uint256_max = 2**256 - 1
        approve_data = bytes.fromhex("095ea7b3") + abi_encode(
            ["address", "uint256"], [ROUTER, uint256_max]
        )
        nonce = w3.eth.get_transaction_count(wallet)
        gas_price = w3.eth.gas_price
        approve_tx = {
            "to": MAGIC,
            "data": "0x" + approve_data.hex(),
            "nonce": nonce,
            "gas": 100_000,
            "maxFeePerGas": gas_price * 2,
            "maxPriorityFeePerGas": gas_price,
            "chainId": CHAIN_ID,
            "value": 0,
        }
        signed = acct.sign_transaction(approve_tx)
        raw_tx = (
            signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        )
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        print(f"  Approval tx: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status != 1:
            print("  Approval FAILED")
            return
        print("  Approval confirmed")
    else:
        print("  MAGIC already approved")

    # ── Swap ─────────────────────────────────────────────────────────────
    deadline = int(time.time()) + 300  # 5 min
    swap_calldata = bytes.fromhex("38ed1739") + abi_encode(
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [amount_in, min_out, path, wallet, deadline],
    )
    nonce = w3.eth.get_transaction_count(wallet)
    gas_price = w3.eth.gas_price
    swap_tx = {
        "to": ROUTER,
        "data": "0x" + swap_calldata.hex(),
        "nonce": nonce,
        "chainId": CHAIN_ID,
        "value": 0,
        "maxFeePerGas": gas_price * 2,
        "maxPriorityFeePerGas": gas_price,
    }
    gas = w3.eth.estimate_gas({**swap_tx, "from": wallet})
    swap_tx["gas"] = int(gas * 1.2)

    signed = acct.sign_transaction(swap_tx)
    raw_tx = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw_tx)
    print(f"  Swap tx: {tx_hash.hex()}")
    print(f"  Arbiscan: https://arbiscan.io/tx/{tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        usdt_after = balanceof(USDT, 6)
        print(f"\nSuccess! USDT balance: {usdt_after:.4f} (+{usdt_after - usdt_bal:.4f})")
    else:
        print("\nSwap FAILED — check Arbiscan")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually send the transaction")
    args = parser.parse_args()
    main(execute=args.execute)
