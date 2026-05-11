"""
scripts/rebalance_now.py - Run rebalance immediately from command line.

Fetches live balances, calculates 50/50 targets, executes wallet->Binance only.

Actions automated:
  wallet LINK surplus -> bridge to BSC via Synapse  (BINANCE_BEP20_LINK_ADDRESS)
  wallet USDT surplus -> ERC20 transfer to Binance  (BINANCE_USDT_DEPOSIT_ADDRESS)

Manual only:
  Binance USDT surplus -> withdraw via Arbitrum One on Binance manually

Usage:
    python scripts/rebalance_now.py
    python scripts/rebalance_now.py --execute
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv

load_dotenv()
from eth_abi import encode as abi_encode
from web3 import Web3

RPC_URL = os.getenv("ARB_RPC_URL", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
LINK_ARB = "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4"
USDT_ARB = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"
CHAIN_ID = 42161
MIN_LINK = 0.3
MIN_USDT = 3.0


def bal(w3, token, wallet, dec):
    padded = wallet.lower().replace("0x", "").zfill(64)
    raw = w3.eth.call({"to": Web3.to_checksum_address(token), "data": "0x70a08231" + padded})
    return int.from_bytes(bytes(raw[:32]), "big") / 10**dec


def binance_bal():
    from exchange.client import ExchangeClient

    c = ExchangeClient(
        {
            "apiKey": os.getenv("BINANCE_API_KEY"),
            "secret": os.getenv("BINANCE_SECRET"),
            "sandbox": False,
        }
    )
    b = c.fetch_balance()
    return float(b.get("LINK", {}).get("free", 0)), float(b.get("USDT", {}).get("free", 0))


def send_usdt(w3, acct, amount, dest, execute):
    wei = int(amount * 1e6)
    data = bytes.fromhex("a9059cbb") + abi_encode(["address", "uint256"], [dest, wei])
    gp = w3.eth.gas_price
    tx = {
        "to": Web3.to_checksum_address(USDT_ARB),
        "data": "0x" + data.hex(),
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": CHAIN_ID,
        "value": 0,
        "maxFeePerGas": gp * 2,
        "maxPriorityFeePerGas": gp,
    }
    tx["gas"] = int(w3.eth.estimate_gas({**tx, "from": acct.address}) * 1.2)
    if not execute:
        print(f"  [dry] would send ${amount:.2f} USDT to Binance")
        return
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    txh = w3.eth.send_raw_transaction(raw)
    w3.eth.wait_for_transaction_receipt(txh, timeout=60)
    print(f"  Sent ${amount:.2f} USDT -> Binance: {txh.hex()}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    exe = args.execute

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    acct = w3.eth.account.from_key(PRIVATE_KEY)

    print("Fetching balances...")
    w_link = bal(w3, LINK_ARB, acct.address, 18)
    w_usdt = bal(w3, USDT_ARB, acct.address, 6)
    b_link, b_usdt = binance_bal()

    tl = (w_link + b_link) / 2
    tu = (w_usdt + b_usdt) / 2

    print(f"\nWallet:  {w_link:.4f} LINK  ${w_usdt:.2f} USDT")
    print(f"Binance: {b_link:.4f} LINK  ${b_usdt:.2f} USDT")
    print(f"Target:  {tl:.4f} LINK each   ${tu:.2f} USDT each")
    print(f'Mode:    {"EXECUTE" if exe else "DRY RUN (pass --execute to transfer)"}')

    ld = w_link - tl
    ud = w_usdt - tu

    if ld >= MIN_LINK:
        bep = os.getenv("BINANCE_BEP20_LINK_ADDRESS", "")
        if not bep:
            print(f"\nLINK: bridge {ld:.4f} to BSC — set BINANCE_BEP20_LINK_ADDRESS in .env")
        else:
            print(f"\nLINK: bridging {ld:.4f} LINK wallet -> BSC (Synapse)...")
            cmd = [
                sys.executable,
                "scripts/bridge_link_bsc.py",
                "--to",
                bep,
                "--amount",
                f"{ld:.4f}",
            ]
            if not exe:
                cmd.append("--dry-run")
            subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    elif ld <= -MIN_LINK:
        print(f"\nLINK: Binance has {-ld:.4f} surplus — sell excess on Binance manually")
    else:
        print(f"\nLINK: balanced ({ld:+.4f})")

    if ud >= MIN_USDT:
        dep = os.getenv("BINANCE_USDT_DEPOSIT_ADDRESS", "")
        if not dep:
            print(f"\nUSDT: send ${ud:.2f} to Binance — set BINANCE_USDT_DEPOSIT_ADDRESS in .env")
        else:
            print(f"\nUSDT: sending ${ud:.2f} wallet -> Binance...")
            send_usdt(w3, acct, ud, dep, exe)
    elif ud <= -MIN_USDT:
        print(f"\nUSDT: Binance has ${-ud:.2f} surplus")
        print(f"  Manual: withdraw ${-ud:.2f} USDT from Binance -> wallet (Arbitrum One)")
    else:
        print(f"\nUSDT: balanced ({ud:+.2f})")


if __name__ == "__main__":
    main()
