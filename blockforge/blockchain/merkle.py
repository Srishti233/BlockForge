"""Merkle tree over transaction ids.

Leaf hash     = SHA-256(0x00 || tx_id_bytes)
Internal hash = SHA-256(0x01 || left || right)
Odd level     = the last node is duplicated.

KNOWN CAVEAT (CVE-2012-2459 style): duplicating the last node means the lists
[a, b, c] and [a, b, c, c] share a Merkle root. Here block validation also
rejects duplicate transaction ids inside a block, which closes the practical hole.
"""
from __future__ import annotations

from typing import Sequence

from blockforge.blockchain.crypto import sha256_bytes

EMPTY_ROOT = sha256_bytes(b"").hex()


def leaf_hash(tx_id_hex: str) -> bytes:
    return sha256_bytes(b"\x00" + bytes.fromhex(tx_id_hex))


def node_hash(left: bytes, right: bytes) -> bytes:
    return sha256_bytes(b"\x01" + left + right)


def _levels(tx_ids: Sequence[str]) -> list[list[bytes]]:
    level = [leaf_hash(t) for t in tx_ids]
    levels = [level]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level = level + [level[-1]]
        level = [node_hash(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        levels.append(level)
    return levels


def merkle_root(tx_ids: Sequence[str]) -> str:
    if not tx_ids:
        return EMPTY_ROOT
    return _levels(tx_ids)[-1][0].hex()


def merkle_proof(tx_ids: Sequence[str], index: int) -> list[dict[str, str]]:
    """Proof for tx_ids[index]: per level the sibling hash and which side it is on."""
    if not 0 <= index < len(tx_ids):
        raise IndexError("leaf index out of range")
    proof: list[dict[str, str]] = []
    idx = index
    for level in _levels(tx_ids)[:-1]:
        padded = level + [level[-1]] if len(level) % 2 == 1 else level
        if idx % 2 == 0:
            proof.append({"hash": padded[idx + 1].hex(), "side": "right"})
        else:
            proof.append({"hash": padded[idx - 1].hex(), "side": "left"})
        idx //= 2
    return proof


def verify_proof(leaf_tx_id: str, proof: Sequence[dict[str, str]], root: str) -> bool:
    """Standalone verification; never raises on malformed input."""
    try:
        current = leaf_hash(leaf_tx_id)
        for step in proof:
            sibling = bytes.fromhex(step["hash"])
            if step["side"] == "left":
                current = node_hash(sibling, current)
            elif step["side"] == "right":
                current = node_hash(current, sibling)
            else:
                return False
        return current.hex() == root
    except (ValueError, KeyError, TypeError):
        return False
