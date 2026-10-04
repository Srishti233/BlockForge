"""Block header and block."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from blockforge.blockchain import crypto
from blockforge.blockchain.transaction import MalformedError, Transaction, _is_int

ZERO_HASH = "0" * 64
_HEADER_INT_FIELDS = ("height", "timestamp", "difficulty_bits", "nonce")


@dataclass(frozen=True)
class BlockHeader:
    height: int
    prev_hash: str
    merkle_root: str
    state_root: str
    timestamp: int
    difficulty_bits: int
    nonce: int
    miner: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "height": self.height, "prev_hash": self.prev_hash,
            "merkle_root": self.merkle_root, "state_root": self.state_root,
            "timestamp": self.timestamp, "difficulty_bits": self.difficulty_bits,
            "nonce": self.nonce, "miner": self.miner,
        }

    def hash(self) -> str:
        return crypto.sha256_hex(crypto.canonical_json(self.to_dict()))

    def with_nonce(self, nonce: int) -> "BlockHeader":
        return replace(self, nonce=nonce)

    @classmethod
    def from_dict(cls, d: Any) -> "BlockHeader":
        if not isinstance(d, dict) or set(d) != {
                "height", "prev_hash", "merkle_root", "state_root", "timestamp",
                "difficulty_bits", "nonce", "miner"}:
            raise MalformedError("malformed block header")
        for k in _HEADER_INT_FIELDS:
            if not _is_int(d[k]):
                raise MalformedError(f"header.{k} must be an integer")
        for k in ("prev_hash", "merkle_root", "state_root", "miner"):
            if not isinstance(d[k], str):
                raise MalformedError(f"header.{k} must be a string")
        return cls(**d)


@dataclass(frozen=True)
class Block:
    header: BlockHeader
    transactions: tuple[Transaction, ...]
    # Hash claimed by whoever supplied the block (e.g. a peer or the DB).
    # Validation recomputes the real hash and rejects a mismatch.
    claimed_hash: Optional[str] = None

    @property
    def hash(self) -> str:
        return self.header.hash()

    @property
    def height(self) -> int:
        return self.header.height

    @property
    def tx_ids(self) -> list[str]:
        return [t.tx_id for t in self.transactions]

    def to_dict(self) -> dict[str, Any]:
        return {"hash": self.hash, "header": self.header.to_dict(),
                "transactions": [t.to_dict() for t in self.transactions]}

    def size_bytes(self) -> int:
        return len(crypto.canonical_json(self.to_dict()))

    @classmethod
    def from_dict(cls, d: Any) -> "Block":
        if not isinstance(d, dict) or "header" not in d or "transactions" not in d:
            raise MalformedError("block must have header and transactions")
        txs = d["transactions"]
        if not isinstance(txs, list):
            raise MalformedError("transactions must be a list")
        claimed = d.get("hash")
        if claimed is not None and not isinstance(claimed, str):
            raise MalformedError("hash must be a string")
        return cls(header=BlockHeader.from_dict(d["header"]),
                   transactions=tuple(Transaction.from_dict(t) for t in txs),
                   claimed_hash=claimed)
