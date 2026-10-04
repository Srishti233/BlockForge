"""Proof-of-Work rules: target, work, and an interruptible nonce search."""
from __future__ import annotations

import threading
from typing import Optional

from blockforge.blockchain import crypto
from blockforge.blockchain.block import BlockHeader


def target_for(difficulty_bits: int) -> int:
    return 2 ** (256 - difficulty_bits)


def meets_target(block_hash: str, difficulty_bits: int) -> bool:
    return int(block_hash, 16) <= target_for(difficulty_bits)


def block_work(difficulty_bits: int) -> int:
    """Expected number of hashes to find a block: 2**256 // (target + 1)."""
    return 2 ** 256 // (target_for(difficulty_bits) + 1)


def mine_header(header: BlockHeader, stop_event: Optional[threading.Event] = None,
                max_nonce: int = 2 ** 63) -> Optional[BlockHeader]:
    """Search for a nonce satisfying the header's own difficulty.

    Returns the solved header, or None if stop_event was set (or the space ran out).
    The stop flag is polled every 256 attempts so new-block arrival interrupts quickly.
    """
    target = target_for(header.difficulty_bits)
    base = header.to_dict()
    nonce = header.nonce
    while nonce < max_nonce:
        if stop_event is not None and (nonce & 0xFF) == 0 and stop_event.is_set():
            return None
        base["nonce"] = nonce
        digest = crypto.sha256_hex(crypto.canonical_json(base))
        if int(digest, 16) <= target:
            return header.with_nonce(nonce)
        nonce += 1
    return None
