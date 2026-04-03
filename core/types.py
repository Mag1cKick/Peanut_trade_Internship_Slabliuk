"""
core/types.py — Base types for the trading system.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from eth_utils import is_hex_address, to_checksum_address


@dataclass(frozen=True)
class Address:
    """
    Ethereum address with validation and EIP-55 checksumming.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(f"Address value must be a str, got {type(self.value).__name__}.")
        raw = self.value.strip()
        if not raw.startswith("0x") and not raw.startswith("0X"):
            raise ValueError(
                f"{self.value!r} is not a valid Ethereum address. "
                "Expected a 20-byte hex string starting with '0x'."
            )
        if not is_hex_address(raw):
            raise ValueError(
                f"{self.value!r} is not a valid Ethereum address. "
                "Expected a 20-byte hex string starting with '0x'."
            )
        object.__setattr__(self, "value", to_checksum_address(raw))

    @classmethod
    def from_string(cls, s: str) -> Address:
        """Alias for the constructor — explicit factory for readability."""
        return cls(s)

    @property
    def checksum(self) -> str:
        """EIP-55 checksummed address string."""
        return self.value

    @property
    def lower(self) -> str:
        """Lowercase address (useful as dict key / hash input)."""
        return self.value.lower()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Address):
            return self.value.lower() == other.value.lower()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value.lower())

    def __repr__(self) -> str:
        return f"Address({self.value})"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TokenAmount:
    """
    Represents a token amount with correct decimal handling.
    """

    raw: int
    decimals: int
    symbol: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw, int):
            raise TypeError(
                f"raw must be an int, got {type(self.raw).__name__}. "
                "Never pass floats — use TokenAmount.from_human() with a string."
            )
        if not isinstance(self.decimals, int) or self.decimals < 0:
            raise ValueError(f"decimals must be a non-negative int, got {self.decimals!r}.")

    @classmethod
    def from_human(
        cls,
        amount: str | Decimal,
        decimals: int,
        symbol: str | None = None,
    ) -> TokenAmount:
        """
        Create from a human-readable amount string or Decimal.
        """
        if isinstance(amount, float):
            raise TypeError(
                "float is not allowed for token amounts — precision will be lost. "
                "Pass a string like '1.5' or a Decimal instead."
            )
        try:
            d = Decimal(str(amount))
        except InvalidOperation:
            raise ValueError(f"{amount!r} is not a valid decimal number.")
        if d < 0:
            raise ValueError(f"Token amount cannot be negative, got {amount!r}.")
        multiplier = Decimal(10) ** decimals
        raw = int(d * multiplier)
        return cls(raw=raw, decimals=decimals, symbol=symbol)

    @property
    def human(self) -> Decimal:
        """Return exact human-readable Decimal (no float involved)."""
        return Decimal(self.raw) / (Decimal(10) ** self.decimals)

    def __add__(self, other: TokenAmount) -> TokenAmount:
        if not isinstance(other, TokenAmount):
            return NotImplemented
        if self.decimals != other.decimals:
            raise ValueError(
                f"Cannot add TokenAmounts with different decimals: "
                f"{self.decimals} vs {other.decimals}. "
                "Convert to the same denomination first."
            )
        symbol = self.symbol if self.symbol == other.symbol else None
        return TokenAmount(raw=self.raw + other.raw, decimals=self.decimals, symbol=symbol)

    def __sub__(self, other: TokenAmount) -> TokenAmount:
        if not isinstance(other, TokenAmount):
            return NotImplemented
        if self.decimals != other.decimals:
            raise ValueError(
                f"Cannot subtract TokenAmounts with different decimals: "
                f"{self.decimals} vs {other.decimals}."
            )
        if other.raw > self.raw:
            raise ValueError(
                f"Subtraction would result in negative amount: " f"{self.raw} - {other.raw} < 0."
            )
        symbol = self.symbol if self.symbol == other.symbol else None
        return TokenAmount(raw=self.raw - other.raw, decimals=self.decimals, symbol=symbol)

    def __mul__(self, factor: int | Decimal) -> TokenAmount:
        if isinstance(factor, float):
            raise TypeError(
                "float multiplier is not allowed — precision will be lost. "
                "Use int or Decimal instead."
            )
        if isinstance(factor, int):
            return TokenAmount(raw=self.raw * factor, decimals=self.decimals, symbol=self.symbol)
        if isinstance(factor, Decimal):
            raw = int(Decimal(self.raw) * factor)
            return TokenAmount(raw=raw, decimals=self.decimals, symbol=self.symbol)
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TokenAmount):
            return self.raw == other.raw and self.decimals == other.decimals
        return NotImplemented

    def __lt__(self, other: TokenAmount) -> bool:
        if not isinstance(other, TokenAmount):
            return NotImplemented
        if self.decimals != other.decimals:
            raise ValueError("Cannot compare TokenAmounts with different decimals.")
        return self.raw < other.raw

    def __le__(self, other: TokenAmount) -> bool:
        return self == other or self < other

    def __str__(self) -> str:
        suffix = self.symbol or ""
        return f"{self.human}{suffix}"

    def __repr__(self) -> str:
        return f"TokenAmount(raw={self.raw}, decimals={self.decimals}, symbol={self.symbol!r})"

    def __hash__(self) -> int:
        return hash((self.raw, self.decimals))


@dataclass(frozen=True, eq=False)
class Token:
    """
    Represents an ERC-20 token with its on-chain metadata.
    """

    address: Address
    symbol: str
    decimals: int

    def __post_init__(self) -> None:
        if not isinstance(self.address, Address):
            raise TypeError(
                f"address must be an Address instance, got {type(self.address).__name__}."
            )
        if self.decimals < 0:
            raise ValueError(f"Token decimals cannot be negative, got {self.decimals}.")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Token):
            return self.address == other.address  # delegates to Address.__eq__ (case-insensitive)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.address.lower)

    def __repr__(self) -> str:
        return f"Token({self.symbol},{self.address.checksum})"

    def amount(self, human: str | Decimal) -> TokenAmount:
        """Convenience: create a TokenAmount for this token."""
        return TokenAmount.from_human(human, self.decimals, self.symbol)


WETH = Token(
    address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
    symbol="WETH",
    decimals=18,
)

USDC = Token(
    address=Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    symbol="USDC",
    decimals=6,
)


@dataclass
class TransactionRequest:
    """A transaction ready to be signed and sent."""

    to: Address
    value: TokenAmount
    data: bytes
    nonce: int | None = None
    gas_limit: int | None = None
    max_fee_per_gas: int | None = None
    max_priority_fee: int | None = None
    chain_id: int = 1

    def to_dict(self) -> dict:
        """
        Convert to a web3.py-compatible transaction dict.
        """
        tx: dict = {
            "to": self.to.checksum,
            "value": self.value.raw,
            "data": self.data,
            "chainId": self.chain_id,
        }
        if self.nonce is not None:
            tx["nonce"] = self.nonce
        if self.gas_limit is not None:
            tx["gas"] = self.gas_limit
        if self.max_fee_per_gas is not None:
            tx["maxFeePerGas"] = self.max_fee_per_gas
        if self.max_priority_fee is not None:
            tx["maxPriorityFeePerGas"] = self.max_priority_fee
        return tx


@dataclass
class TransactionReceipt:
    """Parsed Ethereum transaction receipt."""

    tx_hash: str
    block_number: int
    status: bool
    gas_used: int
    effective_gas_price: int  # in wei
    logs: list

    @property
    def tx_fee(self) -> TokenAmount:
        """Transaction fee paid, expressed as ETH (18 decimals)."""
        fee_wei = self.gas_used * self.effective_gas_price
        return TokenAmount(raw=fee_wei, decimals=18, symbol="ETH")

    @classmethod
    def from_web3(cls, receipt: dict) -> TransactionReceipt:
        """
        Parse from a web3.py receipt dict (or AttributeDict).
        """

        def get(key_camel: str, key_snake: str, default=None):
            return receipt.get(key_camel, receipt.get(key_snake, default))

        tx_hash = get("transactionHash", "transaction_hash")
        if hasattr(tx_hash, "hex"):
            tx_hash = tx_hash.hex()

        return cls(
            tx_hash=tx_hash,
            block_number=get("blockNumber", "block_number"),
            status=bool(get("status", "status", False)),
            gas_used=get("gasUsed", "gas_used"),
            effective_gas_price=get("effectiveGasPrice", "effective_gas_price", 0),
            logs=list(get("logs", "logs", [])),
        )
