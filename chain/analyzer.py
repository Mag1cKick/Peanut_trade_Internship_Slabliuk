"""
chain/analyzer.py — CLI tool to analyze any Ethereum transaction.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal

from web3 import Web3

KNOWN_FUNCTIONS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    # ERC-20
    "0xa9059cbb": ("transfer(address,uint256)", [("to", "address"), ("amount", "uint256")]),
    "0x095ea7b3": ("approve(address,uint256)", [("spender", "address"), ("amount", "uint256")]),
    "0x23b872dd": (
        "transferFrom(address,address,uint256)",
        [("from", "address"), ("to", "address"), ("amount", "uint256")],
    ),
    # Uniswap V2
    "0x38ed1739": (
        "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)",
        [
            ("amountIn", "uint256"),
            ("amountOutMin", "uint256"),
            ("path", "address[]"),
            ("to", "address"),
            ("deadline", "uint256"),
        ],
    ),
    "0x7ff36ab5": (
        "swapExactETHForTokens(uint256,address[],address,uint256)",
        [
            ("amountOutMin", "uint256"),
            ("path", "address[]"),
            ("to", "address"),
            ("deadline", "uint256"),
        ],
    ),
    "0x18cbafe5": (
        "swapExactTokensForETH(uint256,uint256,address[],address,uint256)",
        [
            ("amountIn", "uint256"),
            ("amountOutMin", "uint256"),
            ("path", "address[]"),
            ("to", "address"),
            ("deadline", "uint256"),
        ],
    ),
    "0x8803dbee": (
        "swapTokensForExactTokens(uint256,uint256,address[],address,uint256)",
        [
            ("amountOut", "uint256"),
            ("amountInMax", "uint256"),
            ("path", "address[]"),
            ("to", "address"),
            ("deadline", "uint256"),
        ],
    ),
    "0xe8e33700": (
        "addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)",
        [
            ("tokenA", "address"),
            ("tokenB", "address"),
            ("amountADesired", "uint256"),
            ("amountBDesired", "uint256"),
            ("amountAMin", "uint256"),
            ("amountBMin", "uint256"),
            ("to", "address"),
            ("deadline", "uint256"),
        ],
    ),
    "0xbaa2abde": (
        "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)",
        [
            ("tokenA", "address"),
            ("tokenB", "address"),
            ("liquidity", "uint256"),
            ("amountAMin", "uint256"),
            ("amountBMin", "uint256"),
            ("to", "address"),
            ("deadline", "uint256"),
        ],
    ),
    "0xac9650d8": ("multicall(bytes[])", [("data", "bytes[]")]),
    "0x414bf389": (
        "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))",
        [
            ("params", "tuple"),
        ],
    ),
    "0xc04b8d59": ("exactInput((bytes,address,uint256,uint256,uint256))", [("params", "tuple")]),
    "0xdb3e2198": (
        "exactOutputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))",
        [
            ("params", "tuple"),
        ],
    ),
    "0xf28c0498": ("exactOutput((bytes,address,uint256,uint256,uint256))", [("params", "tuple")]),
}

ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
UNISWAP_V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
UNISWAP_V2_SYNC_TOPIC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"

ERC20_ABI = [
    {
        "name": "symbol",
        "type": "function",
        "inputs": [],
        "outputs": [{"type": "string"}],
        "stateMutability": "view",
    },
    {
        "name": "decimals",
        "type": "function",
        "inputs": [],
        "outputs": [{"type": "uint8"}],
        "stateMutability": "view",
    },
]
_token_cache: dict[str, dict] = {}


def _get_token_info(w3: Web3, address: str) -> dict:
    """Fetch symbol and decimals for a token address, with caching."""
    addr_lower = address.lower()
    if addr_lower in _token_cache:
        return _token_cache[addr_lower]

    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(address),
            abi=ERC20_ABI,
        )
        symbol = contract.functions.symbol().call()
        decimals = contract.functions.decimals().call()
        info = {"symbol": symbol, "decimals": decimals}
    except Exception:
        info = {"symbol": address[:8] + "…", "decimals": 18}

    _token_cache[addr_lower] = info
    return info


def _fmt_token_amount(raw: int, decimals: int, symbol: str) -> str:
    amount = Decimal(raw) / Decimal(10**decimals)
    return f"{amount:,.6f} {symbol}".rstrip("0").rstrip(".")


def decode_function(data: bytes) -> dict:
    """
    Decode transaction calldata into function name and raw arguments.
    """
    if not data or len(data) < 4:
        return {"selector": None, "name": "ETH Transfer", "args": []}

    selector = "0x" + data[:4].hex()
    known = KNOWN_FUNCTIONS.get(selector)

    if not known:
        return {
            "selector": selector,
            "name": "Unknown",
            "args": [{"name": "data", "raw": "0x" + data[4:].hex()}],
        }

    func_name, param_defs = known
    payload = data[4:]
    args = []

    try:
        chunks = [payload[i : i + 32] for i in range(0, len(payload), 32)]
        for i, (arg_name, arg_type) in enumerate(param_defs):
            if i >= len(chunks):
                break
            chunk = chunks[i]
            if arg_type == "address":
                value = "0x" + chunk[-20:].hex()
                try:
                    value = Web3.to_checksum_address(value)
                except Exception:
                    pass
                args.append(
                    {
                        "name": arg_name,
                        "type": arg_type,
                        "value": value,
                        "raw": int.from_bytes(chunk, "big"),
                    }
                )
            elif arg_type == "uint256":
                value = int.from_bytes(chunk, "big")
                args.append({"name": arg_name, "type": arg_type, "value": value, "raw": value})
            else:
                args.append(
                    {
                        "name": arg_name,
                        "type": arg_type,
                        "value": "0x" + chunk.hex(),
                        "raw": chunk.hex(),
                    }
                )
    except Exception as exc:
        args = [
            {
                "name": "decode_error",
                "type": "error",
                "value": str(exc),
                "raw": "0x" + data[4:].hex(),
            }
        ]

    return {"selector": selector, "name": func_name, "args": args}


def parse_logs(logs: list, w3: Web3) -> dict:
    """Parse transaction logs into transfers, swaps, and sync events."""
    transfers = []
    swaps = []
    syncs = []

    for log in logs:
        topics = log.get("topics", [])
        if not topics:
            continue

        raw_topic = topics[0]
        if isinstance(raw_topic, bytes | bytearray):
            topic0 = "0x" + raw_topic.hex()
        else:
            topic0 = raw_topic if raw_topic.startswith("0x") else "0x" + raw_topic

        if topic0 == ERC20_TRANSFER_TOPIC and len(topics) >= 3:
            token_addr = log.get("address", "")
            token_info = _get_token_info(w3, token_addr)
            from_addr = (
                "0x" + topics[1][-20:].hex()
                if hasattr(topics[1], "hex")
                else "0x" + bytes.fromhex(topics[1][2:])[-20:].hex()
            )
            to_addr = (
                "0x" + topics[2][-20:].hex()
                if hasattr(topics[2], "hex")
                else "0x" + bytes.fromhex(topics[2][2:])[-20:].hex()
            )
            raw_data = log.get("data", b"")
            if isinstance(raw_data, bytes | bytearray):
                amount_raw = int.from_bytes(raw_data[:32], "big") if len(raw_data) >= 32 else 0
            else:
                amount_raw = int(raw_data, 16) if raw_data and raw_data != "0x" else 0
            transfers.append(
                {
                    "token": token_info["symbol"],
                    "token_address": token_addr,
                    "from": Web3.to_checksum_address(from_addr),
                    "to": Web3.to_checksum_address(to_addr),
                    "amount_raw": amount_raw,
                    "decimals": token_info["decimals"],
                    "amount_fmt": _fmt_token_amount(
                        amount_raw, token_info["decimals"], token_info["symbol"]
                    ),
                }
            )

        elif topic0 == UNISWAP_V2_SWAP_TOPIC:
            raw_data = log.get("data", b"")
            if isinstance(raw_data, bytes | bytearray) and len(raw_data) >= 128:
                amounts = [int.from_bytes(raw_data[i : i + 32], "big") for i in range(0, 128, 32)]
                swaps.append(
                    {
                        "pair": log.get("address", ""),
                        "amount0In": amounts[0],
                        "amount1In": amounts[1],
                        "amount0Out": amounts[2],
                        "amount1Out": amounts[3],
                    }
                )

        elif topic0 == UNISWAP_V2_SYNC_TOPIC:
            raw_data = log.get("data", b"")
            if isinstance(raw_data, bytes | bytearray) and len(raw_data) >= 64:
                syncs.append(
                    {
                        "pair": log.get("address", ""),
                        "reserve0": int.from_bytes(raw_data[:32], "big"),
                        "reserve1": int.from_bytes(raw_data[32:64], "big"),
                    }
                )

    return {"transfers": transfers, "swaps": swaps, "syncs": syncs}


def _gwei(wei: int) -> str:
    return f"{Decimal(wei) / Decimal(10**9):.2f}"


def _eth(wei: int) -> str:
    return f"{Decimal(wei) / Decimal(10**18):.6f}"


def _short(addr: str) -> str:
    return addr[:10] + "…" + addr[-8:] if len(addr) > 20 else addr


def format_text(analysis: dict) -> str:
    """Render analysis dict as a human-readable text report."""
    lines = []
    a = analysis
    tx = a["transaction"]
    receipt = a.get("receipt")

    lines += [
        "",
        "Transaction Analysis",
        "====================",
        f"Hash:           {tx['hash']}",
    ]

    if receipt:
        lines.append(f"Block:          {receipt['block_number']:,}")

    ts = a.get("timestamp")
    if ts:
        dt = datetime.fromtimestamp(ts, tz=UTC)
        lines.append(f"Timestamp:      {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")

    if receipt:
        status = "SUCCESS" if receipt["status"] else "FAILED"
        lines.append(f"Status:         {status}")
    else:
        lines.append("Status:         PENDING")

    lines += [
        "",
        f"From:           {tx.get('from', 'unknown')}",
        f"To:             {tx.get('to', 'contract creation')}",
        f"Value:          {_eth(tx.get('value', 0))} ETH",
    ]

    if receipt:
        gas_used = receipt["gas_used"]
        gas_limit = tx.get("gas", gas_used)
        pct = gas_used / gas_limit * 100 if gas_limit else 0
        eff_price = receipt["effective_gas_price"]
        fee_wei = gas_used * eff_price
        lines += [
            "",
            "Gas Analysis",
            "------------",
            f"Gas Limit:      {gas_limit:,}",
            f"Gas Used:       {gas_used:,} ({pct:.2f}%)",
            f"Effective Price: {_gwei(eff_price)} gwei",
            f"Transaction Fee: {_eth(fee_wei)} ETH",
        ]

    func = a.get("function")
    if func:
        lines += [
            "",
            "Function Called",
            "---------------",
        ]
        if func["selector"]:
            lines.append(f"Selector:       {func['selector']}")
        lines.append(f"Function:       {func['name']}")
        if func["args"]:
            lines.append("Arguments:")
            for arg in func["args"]:
                val = arg.get("value", arg.get("raw", ""))
                lines.append(f"  - {arg['name']:<16} {val}")

    events = a.get("events", {})
    transfers = events.get("transfers", [])
    if transfers:
        lines += ["", "Token Transfers", "---------------"]
        for i, t in enumerate(transfers, 1):
            from_s = _short(t["from"])
            to_s = _short(t["to"])
            lines.append(f"{i}. {t['amount_fmt']}  {from_s} → {to_s}")

    revert = a.get("revert_reason")
    if revert:
        lines += ["", "Revert Reason", "-------------", revert]

    lines.append("")
    return "\n".join(lines)


def analyze(tx_hash: str, rpc_url: str) -> dict:
    """
    Fetch and analyze a transaction. Returns a structured dict.
    """
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise ValueError(
            f"Invalid transaction hash: {tx_hash!r}. "
            "Expected a 32-byte hex string starting with '0x' (66 chars total)."
        )

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))

    try:
        tx = dict(w3.eth.get_transaction(tx_hash))
    except Exception as exc:
        raise RuntimeError(f"Could not fetch transaction {tx_hash}: {exc}") from exc

    result: dict = {
        "transaction": {
            "hash": tx_hash,
            "from": tx.get("from"),
            "to": tx.get("to"),
            "value": tx.get("value", 0),
            "gas": tx.get("gas"),
            "nonce": tx.get("nonce"),
            "input": "0x" + tx.get("input", b"").hex()
            if isinstance(tx.get("input"), bytes)
            else tx.get("input", "0x"),
        }
    }

    raw_input = tx.get("input", b"")
    if isinstance(raw_input, str):
        raw_input = (
            bytes.fromhex(raw_input[2:]) if raw_input.startswith("0x") else bytes.fromhex(raw_input)
        )
    result["function"] = decode_function(raw_input)

    try:
        raw_receipt = w3.eth.get_transaction_receipt(tx_hash)
        if raw_receipt is not None:
            receipt_dict = dict(raw_receipt)
            result["receipt"] = {
                "block_number": receipt_dict.get("blockNumber"),
                "status": bool(receipt_dict.get("status", False)),
                "gas_used": receipt_dict.get("gasUsed", 0),
                "effective_gas_price": receipt_dict.get("effectiveGasPrice", 0),
                "logs": [dict(log) for log in receipt_dict.get("logs", [])],
            }
            result["events"] = parse_logs(receipt_dict.get("logs", []), w3)
            if not receipt_dict.get("status"):
                try:
                    w3.eth.call(
                        {
                            "from": tx.get("from"),
                            "to": tx.get("to"),
                            "data": raw_input,
                            "value": tx.get("value", 0),
                        },
                        receipt_dict["blockNumber"] - 1,
                    )
                except Exception as revert_exc:
                    result["revert_reason"] = str(revert_exc)
            try:
                block = w3.eth.get_block(receipt_dict["blockNumber"])
                result["timestamp"] = block.get("timestamp")
            except Exception:
                pass
        else:
            result["receipt"] = None  # pending
    except Exception:
        result["receipt"] = None

    return result


DEFAULT_RPC = "https://eth-mainnet.g.alchemy.com/v2/demo"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze an Ethereum transaction",
        prog="python -m chain.analyzer",
    )
    parser.add_argument("tx_hash", help="Transaction hash (0x...)")
    parser.add_argument(
        "--rpc",
        default=DEFAULT_RPC,
        help=f"RPC URL (default: {DEFAULT_RPC})",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    args = parser.parse_args(argv)

    try:
        analysis = analyze(args.tx_hash, args.rpc)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":

        def _serialise(obj):
            if isinstance(obj, bytes):
                return "0x" + obj.hex()
            raise TypeError(f"Not serialisable: {type(obj)}")

        print(json.dumps(analysis, indent=2, default=_serialise))
    else:
        print(format_text(analysis))

    return 0


if __name__ == "__main__":
    sys.exit(main())
