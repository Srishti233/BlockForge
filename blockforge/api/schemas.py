"""Pydantic v2 request schemas. Strict types, no extra fields, bounded sizes.

`extra="forbid"` is also what makes every endpoint refuse a private key: there is no
field to put one in, and unknown fields are a validation error (whose message never
echoes the submitted value).
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Int64 = Annotated[int, Field(ge=-(2 ** 63), le=2 ** 63)]
Hex64 = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
Address = Annotated[str, Field(min_length=42, max_length=42, pattern=r"^bf[0-9a-f]{40}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TransactionModel(Strict):
    tx_id: str = Field(min_length=64, max_length=64)
    sender: str = Field(min_length=1, max_length=64)
    recipient: str = Field(min_length=1, max_length=64)
    amount: Int64
    fee: Int64
    nonce: Int64
    timestamp: Int64
    public_key: str = Field(max_length=66)
    signature: str = Field(max_length=128)
    chain_id: str = Field(min_length=1, max_length=64)


class BlockHeaderModel(Strict):
    height: Int64
    prev_hash: str = Field(min_length=64, max_length=64)
    merkle_root: str = Field(min_length=64, max_length=64)
    state_root: str = Field(min_length=64, max_length=64)
    timestamp: Int64
    difficulty_bits: Int64
    nonce: Int64
    miner: str = Field(min_length=1, max_length=64)


class BlockModel(Strict):
    hash: Optional[str] = Field(default=None, max_length=64)
    header: BlockHeaderModel
    transactions: list[TransactionModel] = Field(max_length=5000)


class HandshakeModel(Strict):
    node_id: str = Field(min_length=1, max_length=64)
    chain_id: str = Field(min_length=1, max_length=64)
    genesis_hash: str = Field(min_length=64, max_length=64)
    tip_height: Int64
    cumulative_work: str = Field(max_length=100, pattern=r"^[0-9]+$")
    addr: str = Field(min_length=3, max_length=260)
    version: int = 1


class TxMessage(Strict):
    tx: TransactionModel
    sender: Optional[str] = Field(default=None, max_length=260)


class BlockMessage(Strict):
    block: BlockModel
    sender: Optional[str] = Field(default=None, max_length=260)


class PeerAddModel(Strict):
    address: str = Field(min_length=3, max_length=260)


class AddressModel(Strict):
    address: Address


class ProofStep(Strict):
    hash: str = Field(max_length=64)
    side: Literal["left", "right"]


class MerkleVerifyModel(Strict):
    tx_id: str = Field(min_length=1, max_length=64)
    proof: list[ProofStep] = Field(max_length=64)
    root: str = Field(min_length=1, max_length=64)


class PartitionModel(Strict):
    addrs: list[str] = Field(max_length=64)
