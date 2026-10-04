"""Transactions: canonical body, tx id, signing and stateless validation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from blockforge.blockchain import crypto

if TYPE_CHECKING:  # pragma: no cover
    from blockforge.blockchain.wallet import Wallet

COINBASE = "COINBASE"
MAX_AMOUNT = 2 ** 63 - 1  # keeps every integer within 64 bits


@dataclass(frozen=True)
class ValidationError:
    """Structured error: a stable machine code plus a human message."""
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class MalformedError(Exception):
    """Raised when a dict cannot even be parsed into a Transaction."""


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass(frozen=True)
class Transaction:
    tx_id: str
    sender: str
    recipient: str
    amount: int
    fee: int
    nonce: int
    timestamp: int
    public_key: str
    signature: str
    chain_id: str

    # ---- canonical forms
    def body(self) -> dict[str, Any]:
        """The signed, unsigned-body fields (everything except tx_id and signature)."""
        return {
            "sender": self.sender, "recipient": self.recipient, "amount": self.amount,
            "fee": self.fee, "nonce": self.nonce, "timestamp": self.timestamp,
            "public_key": self.public_key, "chain_id": self.chain_id,
        }

    def body_bytes(self) -> bytes:
        return crypto.canonical_json(self.body())

    def compute_id(self) -> str:
        return crypto.sha256_hex(self.body_bytes())

    @property
    def is_coinbase(self) -> bool:
        return self.sender == COINBASE

    def to_dict(self) -> dict[str, Any]:
        d = self.body()
        d["tx_id"] = self.tx_id
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "Transaction":
        """Strictly parse a dict. Raises MalformedError on any type/shape problem."""
        if not isinstance(d, dict):
            raise MalformedError("transaction must be an object")
        expected = {"tx_id", "sender", "recipient", "amount", "fee", "nonce",
                    "timestamp", "public_key", "signature", "chain_id"}
        if set(d.keys()) != expected:
            raise MalformedError(f"transaction fields must be exactly {sorted(expected)}")
        for k in ("tx_id", "sender", "recipient", "public_key", "signature", "chain_id"):
            if not isinstance(d[k], str):
                raise MalformedError(f"{k} must be a string")
        for k in ("amount", "fee", "nonce", "timestamp"):
            if not _is_int(d[k]):
                raise MalformedError(f"{k} must be an integer")
        return cls(**{k: d[k] for k in expected})

    # ---- construction
    @classmethod
    def create_signed(cls, wallet: "Wallet", *, recipient: str, amount: int, fee: int,
                      nonce: int, chain_id: str, timestamp: int) -> "Transaction":
        unsigned = cls(tx_id="", sender=wallet.address, recipient=recipient, amount=amount,
                       fee=fee, nonce=nonce, timestamp=timestamp, public_key=wallet.public_key,
                       signature="", chain_id=chain_id)
        body = unsigned.body_bytes()
        return cls(**{**unsigned.__dict__, "tx_id": crypto.sha256_hex(body),
                      "signature": wallet.sign_message(body)})

    @classmethod
    def coinbase(cls, miner: str, amount: int, height: int, timestamp: int,
                 chain_id: str) -> "Transaction":
        """Coinbase: unsigned, nonce = block height so its id is unique per block."""
        tmp = cls(tx_id="", sender=COINBASE, recipient=miner, amount=amount, fee=0,
                  nonce=height, timestamp=timestamp, public_key="", signature="",
                  chain_id=chain_id)
        return cls(**{**tmp.__dict__, "tx_id": tmp.compute_id()})


def validate_transaction_stateless(tx: Transaction, chain_id: str) -> Optional[ValidationError]:
    """Checks that need no chain state. Returns the first error or None.

    Coinbase transactions are validated by block validation, not here.
    """
    if tx.sender == COINBASE:
        return ValidationError("MALFORMED", "COINBASE transactions are only valid inside blocks")
    if not crypto.is_valid_address(tx.sender) or not crypto.is_valid_address(tx.recipient):
        return ValidationError("MALFORMED", "sender/recipient must be 'bf' + 40 lowercase hex chars")
    if tx.chain_id != chain_id:
        return ValidationError("WRONG_CHAIN", f"transaction chain_id {tx.chain_id!r} != {chain_id!r}")
    if not (1 <= tx.amount <= MAX_AMOUNT):
        return ValidationError("BAD_AMOUNT", "amount must be an integer >= 1")
    if not (0 <= tx.fee <= MAX_AMOUNT):
        return ValidationError("BAD_AMOUNT", "fee must be an integer >= 0")
    if not (0 <= tx.nonce <= MAX_AMOUNT) or not (0 <= tx.timestamp <= MAX_AMOUNT):
        return ValidationError("MALFORMED", "nonce and timestamp must be non-negative integers")
    if tx.amount + tx.fee > MAX_AMOUNT:
        return ValidationError("BAD_AMOUNT", "amount + fee overflows 63 bits")
    try:
        if crypto.address_from_public_key(tx.public_key) != tx.sender:
            return ValidationError("BAD_OWNERSHIP", "public key does not hash to sender address")
    except crypto.CryptoError:
        return ValidationError("MALFORMED", "public_key is not valid hex")
    if tx.tx_id != tx.compute_id():
        return ValidationError("MALFORMED", "tx_id does not match the transaction body")
    if not crypto.verify(tx.public_key, tx.body_bytes(), tx.signature):
        return ValidationError("INVALID_SIGNATURE", "signature verification failed")
    return None
