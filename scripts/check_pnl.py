"""
scripts/check_pnl.py — Ground-truth PnL from live balances.

Reads actual balances from Binance + Arbitrum wallet,
prices everything in USDT, and shows net PnL vs starting capital.

Usage:
    python scripts/check_pnl.py
    python scripts/check_pnl.py --start 100
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv

load_dotenv()

from web3 import Web3

from exchange.client import ExchangeClient

STARTING_CAPITAL = 100.0

ARBITRUM_TOKENS = {
    "USDT": ("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6),
    "LINK": ("0xf97f4df75117a78c1A5a0DBb814Af92458539FB4", 18),
}

WALLET_ADDR = os.getenv("ADDR", "")
RPC_URL = os.getenv("ARB_RPC_URL", "")


def wallet_balance(symbol: str) -> float:
    if not WALLET_ADDR or not RPC_URL:
        return 0.0
    addr, decimals = ARBITRUM_TOKENS.get(symbol, (None, 18))
    if not addr:
        return 0.0
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    padded = WALLET_ADDR.lower().replace("0x", "").zfill(64)
    raw = w3.eth.call({"to": Web3.to_checksum_address(addr), "data": "0x70a08231" + padded})
    return int.from_bytes(bytes(raw[:32]), "big") / 10**decimals


def binance_balances() -> dict:
    client = ExchangeClient(
        {
            "apiKey": os.getenv("BINANCE_API_KEY"),
            "secret": os.getenv("BINANCE_SECRET"),
            "sandbox": False,
        }
    )
    return client.fetch_balance()


def link_price(client_balances: dict) -> float:
    import json
    import urllib.request

    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=LINKUSDT"
        r = json.loads(urllib.request.urlopen(url, timeout=4).read())
        return float(r["price"])
    except Exception:
        return 9.90


def main(start: float) -> None:
    print("Fetching balances...\n")

    w_usdt = wallet_balance("USDT")
    w_link = wallet_balance("LINK")

    bals = binance_balances()
    b_usdt = float(bals.get("USDT", {}).get("free", 0))
    b_link = float(bals.get("LINK", {}).get("free", 0))

    price = link_price(bals)

    w_link_usd = w_link * price
    b_link_usd = b_link * price
    total_usd = w_usdt + w_link_usd + b_usdt + b_link_usd
    pnl = total_usd - start
    pnl_pct = pnl / start * 100

    print(f"LINK price: ${price:.4f}")
    print()
    print(f"{'':30s} {'USDT':>10s} {'LINK':>10s} {'USD value':>12s}")
    print("-" * 65)
    print(f"{'Arbitrum wallet':30s} {w_usdt:>10.4f} {w_link:>10.4f} {w_usdt + w_link_usd:>12.4f}")
    print(f"{'Binance':30s} {b_usdt:>10.4f} {b_link:>10.4f} {b_usdt + b_link_usd:>12.4f}")
    print("-" * 65)
    print(f"{'TOTAL':30s} {w_usdt+b_usdt:>10.4f} {w_link+b_link:>10.4f} {total_usd:>12.4f}")
    print()
    print(f"Starting capital: ${start:.2f}")
    print(f"Current value:    ${total_usd:.2f}")
    sign = "+" if pnl >= 0 else ""
    print(f"Net PnL:          {sign}${pnl:.4f}  ({sign}{pnl_pct:.2f}%)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=float, default=STARTING_CAPITAL)
    args = parser.parse_args()
    main(args.start)
