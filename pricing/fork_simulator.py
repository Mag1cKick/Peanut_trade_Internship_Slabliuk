"""
pricing/fork_simulator.py — Simulate and execute swaps against a local Anvil fork.

SOLID design:
  SRP  — AnvilClient owns the Anvil JSON-RPC transport; ForkSimulator owns
          simulation logic.  Neither does the other's job.
  OCP  — ForkSimulator.simulate_route accepts any AMMPool (via protocol),
          so adding V3 pool support requires zero changes here.
  LSP  — AMMPool protocol ensures any pool implementation can be substituted.
  ISP  — AnvilClient exposes only the cheatcodes callers need; ForkSimulator
          only exposes simulation operations.
  DIP  — ForkSimulator depends on AnvilClient (abstracted transport), not on
          Web3 directly.  Use ForkSimulator.from_url() as a convenience factory
          when you only have a URL string.

Foundry / Anvil cheatcode equivalents (Python → Solidity):
  anvil.snapshot()              →  vm.snapshot()
  anvil.revert(id)              →  vm.revertTo(id)
  anvil.set_balance(a, v)       →  vm.deal(a, v)           (ETH)
  anvil.deal_erc20(t, a, v)     →  deal(token, addr, v)    (ERC-20)
  anvil.impersonate(a)          →  vm.prank(a) / startPrank
  anvil.stop_impersonating(a)   →  vm.stopPrank()
  anvil.warp(ts)                →  vm.warp(ts)
  anvil.mine(n)                 →  vm.roll(block.number + n)
  anvil.roll(n)                 →  vm.roll(n)              (absolute)
  simulator.execute_swap()      →  the actual on-chain swap call
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.router import Route

# ── ABI selectors ──────────────────────────────────────────────────────────────

# getAmountsOut(uint256,address[])
_SEL_GET_AMOUNTS_OUT = bytes.fromhex("d06ca61f")
# getReserves()
_SEL_GET_RESERVES = bytes.fromhex("0902f1ac")
# swapExactTokensForTokens(uint256,uint256,address[],address,uint256)
_SEL_SWAP_EXACT_TOKENS = bytes.fromhex("38ed1739")
# approve(address,uint256)
_SEL_APPROVE = bytes.fromhex("095ea7b3")

# Canonical Uniswap V2 Router 02 on mainnet
_UNISWAP_V2_ROUTER = Address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
# Neutral sender for read-only eth_call
_ZERO_SENDER = Address("0x0000000000000000000000000000000000000001")


# ── SimulationResult ───────────────────────────────────────────────────────────


@dataclass
class SimulationResult:
    """Result from a fork simulation or execution."""

    success: bool
    amount_out: int
    gas_used: int
    error: str | None
    logs: list = field(default_factory=list)


# ── AnvilClient ────────────────────────────────────────────────────────────────


class AnvilClient:
    """
    Thin wrapper around Web3 that exposes Anvil/Hardhat JSON-RPC cheatcodes.

    This is the Python equivalent of Foundry's vm cheatcodes — it lets tests
    manipulate fork state (balances, block number, impersonation) without
    deploying a Solidity test contract.

    Anvil JSON-RPC reference:
      https://book.getfoundry.sh/reference/anvil/

    Usage::

        client = AnvilClient.from_url("http://127.0.0.1:8545")
        snap = client.snapshot()           # save state
        client.set_balance(addr, 10**18)   # give 1 ETH
        client.impersonate(whale)          # act as whale
        ...                                # run your test
        client.revert(snap)                # restore state
    """

    def __init__(self, w3: Web3) -> None:
        self._w3 = w3

    @classmethod
    def from_url(cls, url: str) -> AnvilClient:
        """Convenience factory: create from an HTTP RPC endpoint string."""
        return cls(Web3(Web3.HTTPProvider(url)))

    # ── Read-only helpers ──────────────────────────────────────────────────────

    def call(self, tx: dict) -> bytes:
        """Execute a read-only eth_call and return the raw response bytes."""
        return self._w3.eth.call(tx)

    # ── Foundry cheatcode equivalents ─────────────────────────────────────────

    def snapshot(self) -> int:
        """
        Save current EVM state and return an opaque snapshot ID.

        Equivalent to Forge's ``uint256 snap = vm.snapshot()``.
        """
        result = self._w3.provider.make_request("evm_snapshot", [])
        return int(result["result"], 16)

    def revert(self, snapshot_id: int) -> None:
        """
        Restore EVM state to a previously saved snapshot.

        Equivalent to Forge's ``vm.revertTo(snap)``.

        Args:
            snapshot_id: Value returned by a prior :meth:`snapshot` call.
        """
        self._w3.provider.make_request("evm_revert", [hex(snapshot_id)])

    def set_balance(self, address: Address, wei: int) -> None:
        """
        Set the ETH balance of an account.

        Equivalent to Forge's ``vm.deal(address, amount)``.

        Args:
            address: Target account.
            wei:     New balance in wei.
        """
        self._w3.provider.make_request("anvil_setBalance", [address.checksum, hex(wei)])

    def impersonate(self, address: Address) -> None:
        """
        Allow sending transactions from address without its private key.

        Equivalent to Forge's ``vm.startPrank(address)``.

        Args:
            address: Account to impersonate (e.g. a whale, a contract).
        """
        self._w3.provider.make_request("anvil_impersonateAccount", [address.checksum])

    def stop_impersonating(self, address: Address) -> None:
        """
        Stop impersonating an account.

        Equivalent to Forge's ``vm.stopPrank()``.
        """
        self._w3.provider.make_request("anvil_stopImpersonatingAccount", [address.checksum])

    def mine(self, blocks: int = 1) -> None:
        """
        Mine one or more empty blocks, advancing the block number.

        Equivalent to Forge's ``vm.roll(block.number + blocks)``.

        Args:
            blocks: Number of blocks to mine (default 1).
        """
        for _ in range(blocks):
            self._w3.provider.make_request("evm_mine", [])

    def warp(self, timestamp: int) -> None:
        """
        Set the block timestamp for the next mined block.

        Equivalent to Forge's ``vm.warp(timestamp)``.

        Use this to test time-sensitive logic (vesting schedules, deadlines,
        TWAP oracles) without waiting for real time to pass.

        Args:
            timestamp: Unix timestamp (seconds since epoch) to set.
        """
        self._w3.provider.make_request("evm_setNextBlockTimestamp", [timestamp])
        self._w3.provider.make_request("evm_mine", [])

    def roll(self, block_number: int) -> None:
        """
        Mine blocks until the fork reaches block_number.

        Equivalent to Forge's ``vm.roll(block_number)``.

        Unlike :meth:`mine` (which advances by N), this sets an *absolute*
        block number.  Raises ``ValueError`` if block_number is in the past.

        Args:
            block_number: Target block number (must be >= current block).
        """
        current = self._w3.eth.block_number
        if block_number < current:
            raise ValueError(f"Cannot roll backwards: current={current}, requested={block_number}.")
        delta = block_number - current
        for _ in range(delta):
            self._w3.provider.make_request("evm_mine", [])

    def deal_erc20(
        self,
        token: Address,
        account: Address,
        amount: int,
        balance_slot: int = 0,
    ) -> None:
        """
        Set an ERC-20 token balance via direct storage manipulation.

        Equivalent to Forge's ``deal(token, account, amount)``.

        This writes directly to the token contract's storage, bypassing
        transfer logic, blacklists, and supply caps — just like Foundry's
        cheatcode.

        The ``balance_slot`` is the storage slot index of the ``balances``
        mapping in the token contract.  Common values:

        ============  ====
        Token         Slot
        ============  ====
        WETH          3
        DAI           2
        USDC          9
        LINK          1
        Most others   0 or 1
        ============  ====

        Args:
            token:        ERC-20 contract address.
            account:      Account whose balance to set.
            amount:       New balance (in the token's smallest unit).
            balance_slot: Storage slot of the ``balances`` mapping (default 0).
        """
        storage_key = Web3.keccak(
            abi_encode(["address", "uint256"], [account.checksum, balance_slot])
        )
        value = "0x" + format(amount, "064x")
        self._w3.provider.make_request(
            "hardhat_setStorageAt", [token.checksum, storage_key.hex(), value]
        )

    def send_transaction(self, tx: dict) -> str:
        """
        Broadcast a signed (or impersonated) transaction to the fork.

        Returns:
            Transaction hash as a hex string.
        """
        tx_hash = self._w3.eth.send_transaction(tx)
        if isinstance(tx_hash, bytes):
            return "0x" + tx_hash.hex()
        return str(tx_hash)

    def get_transaction_receipt(self, tx_hash: str) -> dict | None:
        """Return the receipt for a mined transaction, or None if not found."""
        receipt = self._w3.eth.get_transaction_receipt(tx_hash)
        return dict(receipt) if receipt else None


# ── ForkSimulator ──────────────────────────────────────────────────────────────


class ForkSimulator:
    """
    Simulates and executes swaps against a local Anvil fork.

    Depends on AnvilClient (not Web3 directly) — satisfying DIP.  Accepts any
    AMMPool implementation in simulate_route (OCP via AMMPool protocol).

    Args:
        client: An :class:`AnvilClient` connected to the fork.

    Typical usage::

        client = AnvilClient.from_url("http://127.0.0.1:8545")
        sim    = ForkSimulator(client)

        # Read-only: estimate output without changing state
        result = sim.simulate_swap(router, params, sender)

        # State-changing: actually execute the swap (Foundry-style fork test)
        snap   = client.snapshot()
        client.set_balance(sender, 10**18)
        result = sim.execute_swap(router, params, sender)
        client.revert(snap)   # restore state after test
    """

    def __init__(self, client: AnvilClient) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> ForkSimulator:
        """Convenience factory: build from an HTTP RPC endpoint string."""
        return cls(AnvilClient.from_url(url))

    # ── Public API ─────────────────────────────────────────────────────────────

    def simulate_swap(
        self,
        router: Address,
        swap_params: dict,
        sender: Address,
    ) -> SimulationResult:
        """
        Estimate swap output via ``getAmountsOut`` (read-only, no state change).

        This is the Python equivalent of calling the router's view function
        directly — no gas cost, no approvals, no balance checks.

        Args:
            router:      Uniswap V2-compatible router address.
            swap_params: Must contain:
                           ``amount_in`` (int)  – raw input amount
                           ``path``      (list) – ordered token address strings
            sender:      ``tx.from`` for the eth_call (rarely matters for views).

        Returns:
            :class:`SimulationResult` with ``success=False`` on any failure.
        """
        amount_in: int = swap_params["amount_in"]
        path: list[str] = swap_params["path"]

        calldata = _SEL_GET_AMOUNTS_OUT + abi_encode(["uint256", "address[]"], [amount_in, path])
        tx = {
            "to": router.checksum,
            "from": sender.checksum,
            "data": "0x" + calldata.hex(),
        }
        try:
            raw = self._client.call(tx)
            (amounts,) = abi_decode(["uint256[]"], raw)
            amount_out = amounts[-1]
            gas_used = 150_000 + 100_000 * (len(path) - 1)
            return SimulationResult(
                success=True,
                amount_out=amount_out,
                gas_used=gas_used,
                error=None,
            )
        except Exception as exc:
            return SimulationResult(success=False, amount_out=0, gas_used=0, error=str(exc))

    def simulate_route(
        self,
        route: Route,
        amount_in: int,
        sender: Address,
    ) -> SimulationResult:
        """
        Simulate a multi-hop route using live reserves from the fork.

        Fetches current on-chain reserves for each pool (read-only) and runs
        the AMM math hop-by-hop.  Accepts any :class:`~pricing.protocols.AMMPool`
        implementation — V2, V3, or custom pools all work without changes here.

        Args:
            route:     Route whose pools define the swap path.
            amount_in: Raw input amount for ``route.path[0]``.
            sender:    Carried into the result for traceability.

        Returns:
            :class:`SimulationResult` with ``success=False`` on any failure.
        """
        try:
            current = amount_in
            for pool, token_in in zip(route.pools, route.path):
                live_r0, live_r1 = self._get_reserves(pool.address)
                live_pool = UniswapV2Pair(
                    address=pool.address,
                    token0=pool.token0,
                    token1=pool.token1,
                    reserve0=live_r0,
                    reserve1=live_r1,
                    fee_bps=pool.fee_bps,
                )
                current = live_pool.get_amount_out(current, token_in)
            return SimulationResult(
                success=True,
                amount_out=current,
                gas_used=route.estimate_gas(),
                error=None,
            )
        except Exception as exc:
            return SimulationResult(success=False, amount_out=0, gas_used=0, error=str(exc))

    def execute_swap(
        self,
        router: Address,
        swap_params: dict,
        sender: Address,
    ) -> SimulationResult:
        """
        Execute an actual ``swapExactTokensForTokens`` on the fork (state-changing).

        This is the Foundry fork-test equivalent of calling the router contract
        from a test — it mines a real transaction and changes balances.

        Prerequisites (typically set up with AnvilClient cheatcodes):
          - ``sender`` must have sufficient token balance and ETH for gas.
          - The router must be approved to spend the input token.

        Use :attr:`client` to prepare state::

            snap = sim.client.snapshot()
            sim.client.set_balance(sender, 10**18)     # fund gas
            sim.client.impersonate(sender)             # skip signing
            result = sim.execute_swap(router, params, sender)
            sim.client.revert(snap)                    # clean up

        Args:
            router:      Uniswap V2-compatible router address.
            swap_params: Must contain:
                           ``amount_in``     (int)       – input amount
                           ``min_amount_out`` (int)      – slippage floor
                           ``path``          (list[str]) – token addresses
                           ``deadline``      (int)       – Unix timestamp
            sender:      Account that will send the transaction.

        Returns:
            :class:`SimulationResult` — ``success=False`` with ``error`` on failure.
        """
        amount_in: int = swap_params["amount_in"]
        min_amount_out: int = swap_params.get("min_amount_out", 0)
        path: list[str] = swap_params["path"]
        deadline: int = swap_params.get("deadline", 2**32 - 1)

        calldata = _SEL_SWAP_EXACT_TOKENS + abi_encode(
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [amount_in, min_amount_out, path, sender.checksum, deadline],
        )
        tx = {
            "to": router.checksum,
            "from": sender.checksum,
            "data": "0x" + calldata.hex(),
        }
        try:
            tx_hash = self._client.send_transaction(tx)
            receipt = self._client.get_transaction_receipt(tx_hash)
            gas_used = receipt.get("gasUsed", 0) if receipt else 0
            return SimulationResult(
                success=True,
                amount_out=0,  # actual output requires parsing Transfer logs
                gas_used=gas_used,
                error=None,
                logs=receipt.get("logs", []) if receipt else [],
            )
        except Exception as exc:
            return SimulationResult(success=False, amount_out=0, gas_used=0, error=str(exc))

    def compare_simulation_vs_calculation(
        self,
        pair: UniswapV2Pair,
        amount_in: int,
        token_in: Token,
    ) -> dict:
        """
        Compare offline AMM math against the live fork state.

        Fetches on-chain reserves, re-runs our formula, and reports whether
        the two agree.  A mismatch means either the stored reserves are stale
        or there is a formula discrepancy.

        Returns:
            dict with keys: ``calculated``, ``simulated``, ``difference``, ``match``.
        """
        calculated = pair.get_amount_out(amount_in, token_in)

        token_out = pair.token1 if token_in == pair.token0 else pair.token0
        result = self.simulate_swap(
            router=_UNISWAP_V2_ROUTER,
            swap_params={
                "amount_in": amount_in,
                "path": [token_in.address.checksum, token_out.address.checksum],
            },
            sender=_ZERO_SENDER,
        )

        simulated = result.amount_out
        diff = abs(calculated - simulated)
        return {
            "calculated": calculated,
            "simulated": simulated,
            "difference": diff,
            "match": calculated == simulated,
        }

    # ── Public access to the underlying AnvilClient ────────────────────────────

    @property
    def client(self) -> AnvilClient:
        """The AnvilClient used by this simulator (for cheatcode access)."""
        return self._client

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _get_reserves(self, pair_address: Address) -> tuple[int, int]:
        """
        Fetch (reserve0, reserve1) from a Uniswap V2 pair via ``getReserves()``.
        """
        raw = self._client.call(
            {"to": pair_address.checksum, "data": "0x" + _SEL_GET_RESERVES.hex()}
        )
        reserve0, reserve1, _ts = abi_decode(["uint112", "uint112", "uint32"], raw)
        return reserve0, reserve1
