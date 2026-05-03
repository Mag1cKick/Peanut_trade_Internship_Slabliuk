"""
scripts/preflight.py — Pre-flight checklist for PeanutTrade arb bot.

Runs every automated check it can and prints the full checklist so you can
sign off on the manual items before going live.

Usage:
    python scripts/preflight.py                 # basic checks only
    python scripts/preflight.py --rpc <url>     # also check Arbitrum RPC
    python scripts/preflight.py --full          # all checks (needs env vars)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Windows: reconfigure stdout/stderr to UTF-8 so box-drawing and emoji work.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── colours ──────────────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"

PASS = f"{GREEN}[✓]{RESET}"
FAIL = f"{RED}[✗]{RESET}"
WARN = f"{YELLOW}[!]{RESET}"
MANUAL = f"{YELLOW}[□]{RESET}"


def ok(msg: str) -> str:
    return f"  {PASS}  {msg}"


def fail(msg: str) -> str:
    return f"  {FAIL}  {msg}"


def warn(msg: str) -> str:
    return f"  {WARN}  {msg}"


def manual(msg: str) -> str:
    return f"  {MANUAL}  {msg}"


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")
    print("─" * (len(title) + 2))


# ── individual checks ─────────────────────────────────────────────────────────


def check_kill_switch() -> list[str]:
    results = []
    from safety.constants import KILL_SWITCH_FILE, is_kill_switch_active, trigger_kill_switch

    # Make sure it is NOT already armed at startup
    if is_kill_switch_active():
        results.append(
            warn(
                f"Kill switch file already exists: {KILL_SWITCH_FILE} — remove it before going live"
            )
        )
    else:
        results.append(ok("Kill switch not currently armed"))

    # Test: arm + detect + disarm
    try:
        trigger_kill_switch("preflight-test")
        assert (
            is_kill_switch_active()
        ), "file was created but is_kill_switch_active() returned False"
        os.remove(KILL_SWITCH_FILE)
        assert (
            not is_kill_switch_active()
        ), "file was removed but is_kill_switch_active() returned True"
        results.append(ok("Kill switch arm / detect / disarm cycle passed"))
    except Exception as exc:
        results.append(fail(f"Kill switch test failed: {exc}"))

    return results


def check_safety_constants() -> list[str]:
    results = []
    from safety.constants import (
        ABSOLUTE_MAX_DAILY_LOSS,
        ABSOLUTE_MAX_TRADE_USD,
        ABSOLUTE_MAX_TRADES_PER_HOUR,
        ABSOLUTE_MIN_CAPITAL,
    )

    checks = [
        ("ABSOLUTE_MAX_TRADE_USD", ABSOLUTE_MAX_TRADE_USD, 0 < ABSOLUTE_MAX_TRADE_USD <= 100),
        ("ABSOLUTE_MAX_DAILY_LOSS", ABSOLUTE_MAX_DAILY_LOSS, 0 < ABSOLUTE_MAX_DAILY_LOSS <= 50),
        ("ABSOLUTE_MIN_CAPITAL", ABSOLUTE_MIN_CAPITAL, ABSOLUTE_MIN_CAPITAL >= 10),
        (
            "ABSOLUTE_MAX_TRADES_PER_HOUR",
            ABSOLUTE_MAX_TRADES_PER_HOUR,
            0 < ABSOLUTE_MAX_TRADES_PER_HOUR <= 60,
        ),
    ]
    for name, value, sane in checks:
        if sane:
            results.append(ok(f"{name} = {value} (hardcoded, not configurable)"))
        else:
            results.append(fail(f"{name} = {value} — value looks unsafe"))
    return results


def check_safety_check_function() -> list[str]:
    results = []
    from safety.constants import safety_check

    # Good trade
    ok_result, _ = safety_check(10.0, -5.0, 100.0, 5)
    if ok_result:
        results.append(ok("safety_check passes valid trade"))
    else:
        results.append(fail("safety_check incorrectly blocked valid trade"))

    # Over max trade
    blocked, reason = safety_check(9999.0, -5.0, 100.0, 5)
    if not blocked and "absolute max" in reason:
        results.append(ok("safety_check blocks oversized trade"))
    else:
        results.append(fail("safety_check did not block oversized trade"))

    # Under min capital
    blocked, reason = safety_check(5.0, -5.0, 1.0, 5)
    if not blocked and "minimum" in reason:
        results.append(ok("safety_check blocks low-capital trade"))
    else:
        results.append(fail("safety_check did not block low-capital trade"))

    return results


def check_circuit_breaker() -> list[str]:
    results = []
    try:
        from executor.recovery import CircuitBreaker, CircuitBreakerConfig

        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, cooldown_seconds=1))
        assert not cb.is_open(), "breaker should start closed"
        cb.record_failure()
        assert not cb.is_open(), "one failure should not open a 2-threshold breaker"
        cb.record_failure()
        assert cb.is_open(), "two failures should open the breaker"
        time.sleep(1.1)
        assert not cb.is_open(), "breaker should reset after cooldown"
        results.append(ok("Circuit breaker trips after threshold and resets after cooldown"))
    except Exception as exc:
        results.append(fail(f"Circuit breaker test failed: {exc}"))
    return results


def check_fee_calculation() -> list[str]:
    results = []
    try:
        from decimal import Decimal

        from config.settings import Config

        fees = Config.to_fee_structure()
        assert (
            float(fees.cex_taker_bps) == 10.0
        ), f"CEX fee should be 10bps, got {fees.cex_taker_bps}"
        assert float(fees.dex_swap_bps) == 30.0, f"DEX fee should be 30bps, got {fees.dex_swap_bps}"
        assert float(fees.gas_cost_usd) <= 0.20, f"Gas should be ≤$0.20, got {fees.gas_cost_usd}"

        notional = Decimal("100")
        total = fees.fee_usd(notional)
        assert total > 0, "fee_usd must be positive"
        results.append(
            ok(f"Fee structure: CEX 10bps, DEX 30bps, gas ${float(fees.gas_cost_usd):.2f}")
        )
        results.append(ok(f"fee_usd($100 notional) = ${float(total):.4f}"))
    except Exception as exc:
        results.append(fail(f"Fee calculation check failed: {exc}"))
    return results


def check_trading_rules() -> list[str]:
    results = []
    try:
        from config.settings import get_trading_rules

        rules = get_trading_rules("ETH/USDC")
        assert rules.step_size > 0
        assert rules.tick_size > 0
        assert rules.min_notional > 0
        ok_result, _ = rules.validate(0.01, 2000.0)
        assert ok_result, "valid order rejected"
        blocked, reason = rules.validate(0.000001, 2000.0)
        assert not blocked, "invalid order passed"
        results.append(
            ok(
                f"TradingRules for ETH/USDC: step={rules.step_size}, tick={rules.tick_size}, min_notional=${rules.min_notional}"
            )
        )
    except Exception as exc:
        results.append(fail(f"TradingRules check failed: {exc}"))
    return results


def check_logging_setup() -> list[str]:
    results = []
    logs_dir = Path("logs")
    if logs_dir.exists():
        results.append(ok("logs/ directory exists"))
    else:
        results.append(warn("logs/ directory missing — will be created on first bot start"))

    # Check that the logging config in arb_bot actually has a FileHandler
    try:
        # Just verify the _configure_logging function is present without running it
        bot_src = Path("scripts/arb_bot.py").read_text(encoding="utf-8")
        if "FileHandler" in bot_src and "_configure_logging" in bot_src:
            results.append(ok("arb_bot.py has FileHandler-based structured logging"))
        else:
            results.append(fail("arb_bot.py is missing FileHandler logging"))
    except Exception as exc:
        results.append(warn(f"Could not verify logging setup: {exc}"))
    return results


def check_gitignore() -> list[str]:
    results = []
    gitignore = Path(".gitignore")
    if not gitignore.exists():
        results.append(fail(".gitignore not found"))
        return results

    content = gitignore.read_text(encoding="utf-8")
    for entry in [".env", "*.env", ".env.*"]:
        if entry in content:
            results.append(ok(f".gitignore contains: {entry}"))
            break
    else:
        results.append(fail(".gitignore does not include .env — secrets may be committed"))

    for entry in ["logs/", "logs"]:
        if entry in content:
            results.append(ok(f".gitignore contains: {entry}"))
            break
    else:
        results.append(warn(".gitignore does not exclude logs/ directory"))

    return results


def check_no_secrets_in_git() -> list[str]:
    results = []
    try:
        out = subprocess.check_output(
            ["git", "log", "--all", "--oneline", "--diff-filter=A", "--name-only", "--format="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        suspicious = [
            line
            for line in out.splitlines()
            if any(s in line.lower() for s in [".env", "secret", "private_key", "api_key"])
            and not line.endswith(".py")  # ignore source files that reference these names
        ]
        if suspicious:
            results.append(warn("Possibly sensitive files in git history — review manually:"))
            for s in suspicious[:5]:
                results.append(f"      {s}")
        else:
            results.append(ok("No obviously sensitive files found in git history"))
    except Exception as exc:
        results.append(warn(f"Could not check git history: {exc}"))
    return results


def check_telegram() -> list[str]:
    results = []
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        results.append(ok("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set"))
    elif token or chat_id:
        results.append(warn("Only one of TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID is set"))
    else:
        results.append(
            warn(
                "Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing) — alerts will be silent"
            )
        )
    return results


def check_rpc(rpc_url: str) -> list[str]:
    results = []
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from chain.client import ChainClient
        from core.types import Address

        client = ChainClient([rpc_url])
        # Read WETH balance of a known address (zero address — should return 0)
        bal = client.get_balance(Address("0x0000000000000000000000000000000000000000"))
        results.append(ok(f"Arbitrum RPC reachable ({rpc_url[:40]}...)"))
        results.append(ok(f"eth_getBalance call succeeded (returned {bal.raw})"))
    except Exception as exc:
        results.append(fail(f"Arbitrum RPC check failed: {exc}"))
    return results


def check_binance(api_key: str, secret: str) -> list[str]:
    results = []
    try:
        from exchange.client import ExchangeClient

        client = ExchangeClient({"apiKey": api_key, "secret": secret, "sandbox": True})
        ob = client.fetch_order_book("ETH/USDT")
        if ob.get("bids") and ob.get("asks"):
            results.append(ok("Binance testnet reachable — order book fetched for ETH/USDT"))
        else:
            results.append(warn("Binance order book returned empty bids/asks"))
    except Exception as exc:
        results.append(fail(f"Binance connectivity check failed: {exc}"))
    return results


# ── report ────────────────────────────────────────────────────────────────────


def print_manual_section() -> None:
    section("Manual Checks (complete before signing off)")
    manual_items = [
        "API key: Spot Trading only (no Futures, no Margin)",
        "API key: IP whitelist set to your server IP",
        "API key: NO withdrawal permission enabled",
        "Dry run completed — at least 30 minutes of logs attached",
        "Binance app / web open and ready for manual intervention",
        "Emergency flatten procedure: know how to close all positions in Binance UI",
        "Risk limits reviewed and tightened for your actual capital",
        "Telegram alerts tested (received test message)",
    ]
    for item in manual_items:
        print(manual(item))
    print()
    print("  Student signature: ________________________________  Date: __________")
    print("  Instructor sign-off: _____________________________  Date: __________")


def main() -> None:
    parser = argparse.ArgumentParser(description="PeanutTrade pre-flight checklist")
    parser.add_argument("--rpc", metavar="URL", help="Arbitrum RPC URL to test connectivity")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also check Binance (uses BINANCE_TESTNET_API_KEY/SECRET env vars)",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}{'═' * 56}{RESET}")
    print(f"{BOLD}  PeanutTrade Pre-Flight Checklist{RESET}")
    print(f"{BOLD}{'═' * 56}{RESET}")

    all_lines: list[str] = []

    section("Safety Constants")
    all_lines += check_safety_constants()

    section("Safety Check Function")
    all_lines += check_safety_check_function()

    section("Kill Switch")
    all_lines += check_kill_switch()

    section("Circuit Breaker")
    all_lines += check_circuit_breaker()

    section("Fee Calculation")
    all_lines += check_fee_calculation()

    section("Trading Rules")
    all_lines += check_trading_rules()

    section("Logging")
    all_lines += check_logging_setup()

    section("Security")
    all_lines += check_gitignore()
    all_lines += check_no_secrets_in_git()

    section("Telegram Alerts")
    all_lines += check_telegram()

    if args.rpc:
        section("Arbitrum RPC Connectivity")
        all_lines += check_rpc(args.rpc)

    if args.full:
        api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
        secret = os.getenv("BINANCE_TESTNET_SECRET", "")
        if api_key and secret:
            section("Binance Testnet Connectivity")
            all_lines += check_binance(api_key, secret)
        else:
            print(warn("  --full: BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_SECRET not set"))

    for line in all_lines:
        print(line)

    print_manual_section()

    failures = sum(
        1
        for line in all_lines
        if FAIL.replace("\033[91m", "").replace("\033[0m", "") in line or "[✗]" in line
    )
    warnings = sum(
        1
        for line in all_lines
        if WARN.replace("\033[93m", "").replace("\033[0m", "") in line or "[!]" in line
    )

    print(f"\n{'─' * 56}")
    if failures:
        print(f"{RED}{BOLD}  {failures} automated check(s) FAILED — do not go live{RESET}")
        sys.exit(1)
    elif warnings:
        print(f"{YELLOW}{BOLD}  All automated checks passed with {warnings} warning(s){RESET}")
    else:
        print(f"{GREEN}{BOLD}  All automated checks passed ✓{RESET}")
    print()


if __name__ == "__main__":
    main()
