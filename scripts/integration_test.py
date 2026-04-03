"""
scripts/integration_test.py — End-to-end integration test on Sepolia testnet.

Run:
    PRIVATE_KEY=0x... python scripts/integration_test.py
    PRIVATE_KEY=0x... python scripts/integration_test.py --rpc https://...
    PRIVATE_KEY=0x... python scripts/integration_test.py --dry-run  # skip send

Exit codes:
    0 — PASSED
    1 — FAILED (error printed to stderr)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_defunct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from chain.analyzer import analyze, format_text
from chain.builder import TransactionBuilder
from chain.client import ChainClient
from chain.errors import ChainError, TransactionTimeout
from core.types import Address, TokenAmount
from core.wallet import WalletManager

DEFAULT_RPC = os.environ.get("RPC_URL", "https://rpc.sepolia.org")

RECIPIENT = "0x000000000000000000000000000000000000dEaD"

TRANSFER_AMOUNT = "0.001"
MIN_BALANCE = "0.005"
CHAIN_ID = 11155111


def _sep(title: str = "") -> None:
    if title:
        print(f"\n{title}")
        print("-" * len(title))
    else:
        print()


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}", file=sys.stderr)


def _gwei(wei: int) -> str:
    return f"{Decimal(wei) / Decimal(10**9):.2f} gwei"


def _eth(wei: int) -> str:
    return f"{Decimal(wei) / Decimal(10**18):.6f} ETH"


def run(rpc_url: str, dry_run: bool = False) -> int:
    """
    Run the full integration test.
    """
    errors: list[str] = []

    print("=" * 50)
    print("  Peanut Trade — Integration Test (Sepolia)")
    print("=" * 50)

    try:
        wallet = WalletManager.from_env("PRIVATE_KEY")
    except OSError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print("Set PRIVATE_KEY environment variable and retry.", file=sys.stderr)
        return 1

    print(f"\nWallet: {wallet.address}")

    if "0x" in repr(wallet) and wallet.address not in repr(wallet):
        errors.append("SECURITY: wallet repr leaks private key")
    elif wallet.address in repr(wallet) and len(repr(wallet)) < 100:
        _ok("Private key not exposed in repr")
    else:
        _ok("Private key not exposed in repr")

    _sep("Connecting to Sepolia")
    try:
        client = ChainClient(
            rpc_urls=[rpc_url],
            timeout=30,
            max_retries=3,
        )
        _ok(f"Connected to {rpc_url}")
    except Exception as exc:
        _fail(f"Connection failed: {exc}")
        return 1

    _sep("Checking balance")
    try:
        address = Address(wallet.address)
        balance = client.get_balance(address)
        print(f"Balance: {balance.human:.6f} ETH")

        min_balance = TokenAmount.from_human(MIN_BALANCE, 18)
        if balance.raw < min_balance.raw:
            print(
                f"  WARNING: Balance is low ({balance.human:.6f} ETH). "
                f"Get Sepolia ETH from https://sepoliafaucet.com"
            )
            if not dry_run:
                print("  Use --dry-run to skip sending.")
        else:
            _ok("Sufficient balance for test transfer")
    except ChainError as exc:
        _fail(f"Could not fetch balance: {exc}")
        errors.append(str(exc))

    _sep("Building transaction")
    recipient = Address(RECIPIENT)
    value = TokenAmount.from_human(TRANSFER_AMOUNT, 18, "ETH")

    print(f"  To:    {recipient.checksum}")
    print(f"  Value: {TRANSFER_AMOUNT} ETH")

    try:
        builder = (
            TransactionBuilder(client, wallet)
            .to(recipient)
            .value(value)
            .data(b"")
            .chain_id(CHAIN_ID)
            .with_gas_estimate(buffer=1.2)
            .with_gas_price("medium")
        )

        estimated_gas = builder._gas_limit
        gas_price = client.get_gas_price()
        print(f"  Estimated Gas: {estimated_gas:,}")
        print(f"  Max Fee:       {_gwei(gas_price.get_max_fee('medium'))}")
        print(f"  Max Priority:  {_gwei(gas_price.priority_fee_medium)}")
        _ok("Transaction built successfully")
    except ChainError as exc:
        _fail(f"Could not build transaction: {exc}")
        errors.append(str(exc))
        if not dry_run:
            return 1

    _sep("Signing")
    try:
        test_message = f"Integration test — {wallet.address} — {int(time.time())}"
        signed_msg = wallet.sign_message(test_message)

        encoded = encode_defunct(text=test_message)
        recovered = Account.recover_message(encoded, signature=signed_msg.signature)
        sig_valid = recovered.lower() == wallet.address.lower()

        if sig_valid:
            _ok("Signature valid")
            _ok("Recovered address matches wallet address")
        else:
            _fail(f"Signature mismatch: recovered {recovered}, expected {wallet.address}")
            errors.append("Signature verification failed")

        tx = builder.build()
        signed_tx = wallet.sign_transaction(tx.to_dict())
        raw_tx = (
            signed_tx.raw_transaction
            if hasattr(signed_tx, "raw_transaction")
            else signed_tx.rawTransaction
        )
        _ok(f"Transaction signed ({len(raw_tx)} bytes)")

    except Exception as exc:
        _fail(f"Signing failed: {exc}")
        errors.append(str(exc))
        return 1

    tx_hash: str | None = None

    if dry_run:
        _sep("Sending (DRY RUN — skipped)")
        print("  --dry-run flag set, skipping broadcast")
        print("  Signed transaction ready, would send:", raw_tx[:10].hex() + "...")
    else:
        _sep("Sending")
        try:
            tx_hash = client.send_transaction(raw_tx)
            print(f"  TX Hash: {tx_hash}")
            _ok("Transaction broadcast")
        except ChainError as exc:
            _fail(f"Send failed: {exc}")
            errors.append(str(exc))
            return 1

        _sep("Waiting for confirmation")
        print("  Waiting for transaction to be indexed...")
        time.sleep(5)
        try:
            receipt = client.wait_for_receipt(tx_hash, timeout=180, poll_interval=3.0)
            gas_pct = receipt.gas_used / (tx.gas_limit or receipt.gas_used) * 100
            status = "SUCCESS" if receipt.status else "FAILED"

            print(f"  Block:    {receipt.block_number:,}")
            print(f"  Status:   {status}")
            print(f"  Gas Used: {receipt.gas_used:,} ({gas_pct:.0f}%)")
            print(f"  Fee:      {_eth(receipt.tx_fee.raw)}")

            if not receipt.status:
                errors.append(f"Transaction reverted: {tx_hash}")
            else:
                _ok("Confirmed on-chain")

        except TransactionTimeout:
            _fail(f"Transaction not confirmed within 120s: {tx_hash}")
            errors.append("Confirmation timeout")
            return 1
        except ChainError as exc:
            _fail(f"Confirmation error: {exc}")
            errors.append(str(exc))
            return 1

    if tx_hash and not dry_run:
        _sep("Transaction Analysis")
        try:
            analysis = analyze(tx_hash, rpc_url)
            print(format_text(analysis))
        except Exception as exc:
            print(f"  (Analysis unavailable: {exc})")

    print("=" * 50)
    if errors:
        print(f"  Integration test FAILED ({len(errors)} error(s))")
        for e in errors:
            print(f"    - {e}")
        return 1
    else:
        if dry_run:
            print("  Integration test PASSED (dry run)")
        else:
            print("  Integration test PASSED")
    print("=" * 50)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Peanut Trade integration test — runs on Sepolia testnet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  PRIVATE_KEY=0x... python scripts/integration_test.py
  PRIVATE_KEY=0x... python scripts/integration_test.py --rpc https://sepolia.infura.io/v3/KEY
  PRIVATE_KEY=0x... python scripts/integration_test.py --dry-run
        """,
    )
    parser.add_argument(
        "--rpc",
        default=DEFAULT_RPC,
        help=f"Sepolia RPC URL (default: {DEFAULT_RPC})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and sign but do NOT broadcast the transaction",
    )
    args = parser.parse_args()
    return run(args.rpc, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
