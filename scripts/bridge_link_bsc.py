"""
scripts/bridge_link_bsc.py — Bridge LINK from Arbitrum One to BSC via Synapse Protocol.

Synapse SynapseBridge on Arbitrum: 0x6f4e8eba4d337f874ab57478acc2cb5bacdc19c9
Function: deposit(address to, uint256 chainId, address token, uint256 amount)
  - Locks LINK on Arbitrum, Synapse mints equivalent on BSC.
  - chainId for BSC = 56

Cost: ~$0.01 gas on Arbitrum + ~0.05% Synapse fee (~$0.025 on 5 LINK)
Time: ~1 minute

Usage:
    python scripts/bridge_link_bsc.py --to 0xBSC_ADDRESS --amount 5.0
    python scripts/bridge_link_bsc.py --to 0xBSC_ADDRESS            # bridges all wallet LINK
    python scripts/bridge_link_bsc.py --to 0xBSC_ADDRESS --dry-run  # quote only, no tx
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv

load_dotenv()
from eth_abi import encode as abi_encode
from web3 import Web3

RPC_URL = os.getenv("ARB_RPC_URL", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

LINK_ARB = Web3.to_checksum_address("0xf97f4df75117a78c1A5a0DBb814Af92458539FB4")
USDT_ARB = Web3.to_checksum_address("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9")

# Synapse SynapseBridge on Arbitrum One
SYNAPSE_BRIDGE = Web3.to_checksum_address("0x6f4e8eba4d337f874ab57478acc2cb5bacdc19c9")
BSC_CHAIN_ID = 56
CHAIN_ID_ARB = 42161

# deposit(address to, uint256 chainId, address token, uint256 amount)
DEPOSIT_SEL = Web3.keccak(text="deposit(address,uint256,address,uint256)")[:4].hex()


def get_link_balance(w3: Web3, wallet: str) -> float:
    padded = wallet.lower().replace("0x", "").zfill(64)
    raw = w3.eth.call({"to": LINK_ARB, "data": "0x70a08231" + padded})
    return int.from_bytes(bytes(raw[:32]), "big") / 1e18


def get_link_allowance(w3: Web3, wallet: str, spender: str) -> int:
    op = wallet.lower().replace("0x", "").zfill(64)
    sp = spender.lower().replace("0x", "").zfill(64)
    raw = w3.eth.call({"to": LINK_ARB, "data": "0xdd62ed3e" + op + sp})
    return int.from_bytes(bytes(raw[:32]), "big")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--to",
        default=os.getenv("BINANCE_BEP20_LINK_ADDRESS", ""),
        help="BEP20 destination (default: BINANCE_BEP20_LINK_ADDRESS from .env)",
    )
    parser.add_argument("--amount", type=float, default=0.0, help="LINK to bridge (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Quote only, no tx")
    args = parser.parse_args()

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    acct = w3.eth.account.from_key(PRIVATE_KEY)
    wallet = acct.address
    dest = Web3.to_checksum_address(args.to)

    balance = get_link_balance(w3, wallet)
    amount = args.amount if args.amount > 0 else balance
    amount = min(amount, balance)

    if amount <= 0:
        print("No LINK to bridge.")
        return

    amount_wei = int(amount * 1e18)
    eth_price = 2376.83  # rough ETH price for gas estimate
    gas_price = w3.eth.gas_price
    gas_est = 200_000  # typical Synapse bridge tx
    gas_usd = gas_est * gas_price / 1e18 * eth_price

    print(f"Wallet:  {wallet}")
    print(f"Bridge:  {amount:.4f} LINK  ->  BSC address {dest}")
    print(f"Gas est: ${gas_usd:.4f}")
    print(f"Synapse fee: ~0.05% = ${amount * 10.0 * 0.0005:.4f}")
    print(f"Chain:   Arbitrum ({CHAIN_ID_ARB}) -> BSC ({BSC_CHAIN_ID})")

    if args.dry_run:
        print("\nDRY RUN — no transaction sent.")
        return

    # Step 1: Approve LINK for Synapse bridge
    current_allowance = get_link_allowance(w3, wallet, SYNAPSE_BRIDGE)
    if current_allowance < amount_wei:
        print("\nApproving LINK for Synapse bridge...")
        uint256_max = 2**256 - 1
        approve_data = (
            "0x"
            + (
                bytes.fromhex("095ea7b3")
                + abi_encode(["address", "uint256"], [SYNAPSE_BRIDGE, uint256_max])
            ).hex()
        )
        nonce = w3.eth.get_transaction_count(wallet)
        approve_tx = {
            "to": LINK_ARB,
            "data": approve_data,
            "nonce": nonce,
            "gas": 80_000,
            "maxFeePerGas": gas_price * 2,
            "maxPriorityFeePerGas": gas_price,
            "chainId": CHAIN_ID_ARB,
            "value": 0,
        }
        signed = acct.sign_transaction(approve_tx)
        raw = (
            signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        )
        tx = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(tx, timeout=60)
        if receipt.status != 1:
            print("Approval failed.")
            return
        print(f"Approved: {tx.hex()}")

    # Step 2: Bridge via Synapse deposit()
    print("\nSending bridge transaction...")
    bridge_data = (
        "0x"
        + DEPOSIT_SEL
        + abi_encode(
            ["address", "uint256", "address", "uint256"], [dest, BSC_CHAIN_ID, LINK_ARB, amount_wei]
        ).hex()
    )

    nonce = w3.eth.get_transaction_count(wallet)
    bridge_tx_dict = {
        "to": SYNAPSE_BRIDGE,
        "data": bridge_data,
        "nonce": nonce,
        "maxFeePerGas": gas_price * 2,
        "maxPriorityFeePerGas": gas_price,
        "chainId": CHAIN_ID_ARB,
        "value": 0,
    }
    # Estimate gas
    bridge_tx_dict["gas"] = int(w3.eth.estimate_gas({**bridge_tx_dict, "from": wallet}) * 1.2)

    signed = acct.sign_transaction(bridge_tx_dict)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    print(f"Bridge tx sent: {tx_hash.hex()}")
    print(f"Track on Synapse: https://explorer.synapseprotocol.com/txid/{tx_hash.hex()}")
    print(f"Arbiscan: https://arbiscan.io/tx/{tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        print(f"\nBridge initiated! {amount:.4f} LINK will arrive on BSC in ~1 min.")
        print("Then deposit to Binance via BEP20 network.")
    else:
        print("Bridge tx reverted — check Synapse for unsupported token or try Jumper UI.")
        print("Fallback: https://jumper.exchange/bridge/arbitrum-link-to-bsc-bnb")


if __name__ == "__main__":
    main()
