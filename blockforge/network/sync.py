"""Chain synchronization: batch download, ancestor fetching, fork discovery.

Nothing a peer says is trusted: every block goes through Blockchain.add_block, which
runs the same full validation as locally mined blocks.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from blockforge.blockchain.block import Block
from blockforge.blockchain.blockchain import Blockchain
from blockforge.blockchain.transaction import MalformedError
from blockforge.logging_setup import log_event
from blockforge.network.peer import PeerClient
from blockforge.network.protocol import (
    MAX_ANCESTOR_FETCH,
    PENALTY_BAD_BLOCK,
    PENALTY_MALFORMED,
    SYNC_BATCH,
)

log = logging.getLogger("blockforge.sync")


class Syncer:
    def __init__(self, chain: Blockchain, penalize: Callable[[str, int], None]) -> None:
        self.chain = chain
        self._penalize = penalize
        self._lock = threading.Lock()   # one sync at a time

    def sync_from(self, addr: str, client: PeerClient, max_rounds: int = 500) -> int:
        """Download and validate blocks from `client` until we have its best chain.

        Starts just above our tip. If the first block of a batch has an unknown parent
        (the peer is on a fork) the start height backs off exponentially until the two
        chains connect; add_block then handles side branches and the reorg itself.
        Returns the number of new blocks accepted (main or side).
        """
        if not self._lock.acquire(blocking=False):
            return 0
        added = 0
        try:
            start = self.chain.height() + 1
            step = 1
            for _ in range(max_rounds):
                raw = client.blocks(start, SYNC_BATCH)
                if not raw:
                    break
                needs_backoff = False
                for i, item in enumerate(raw):
                    try:
                        block = Block.from_dict(item)
                    except (MalformedError, TypeError, ValueError, KeyError):
                        self._penalize(addr, PENALTY_MALFORMED)
                        return added
                    res = self.chain.add_block(block)
                    if res.status == "rejected":
                        self._penalize(addr, PENALTY_BAD_BLOCK)
                        log_event(log, "sync_rejected_block", logging.WARNING, peer=addr,
                                  height=block.height,
                                  codes=",".join(e.code for e in res.errors))
                        return added
                    if res.is_new:
                        added += 1
                    if i == 0 and res.status == "orphan":
                        needs_backoff = True
                if needs_backoff:
                    if start <= 1:
                        self._penalize(addr, PENALTY_BAD_BLOCK)   # claims a chain with no common genesis
                        break
                    start = max(1, start - step)
                    step *= 2
                    continue
                start += len(raw)
                log_event(log, "sync_progress", peer=addr, next_height=start, tip=self.chain.height())
                if len(raw) < SYNC_BATCH:
                    break
            return added
        finally:
            self._lock.release()

    def fetch_ancestors(self, addr: str, client: PeerClient, block: Block) -> bool:
        """Given a block with an unknown parent, walk back by hash until a known ancestor
        is found, then add the fetched ancestors oldest-first. Returns True if the original
        block ends up known."""
        pending: list[Block] = []
        cursor = block
        for _ in range(MAX_ANCESTOR_FETCH):
            parent_hash = cursor.header.prev_hash
            if self.chain.has_block(parent_hash):
                break
            try:
                parent = Block.from_dict(client.block(parent_hash))
            except (MalformedError, TypeError, ValueError, KeyError):
                self._penalize(addr, PENALTY_MALFORMED)
                return False
            if parent.hash != parent_hash:      # peer sent the wrong block for that hash
                self._penalize(addr, PENALTY_BAD_BLOCK)
                return False
            pending.append(parent)
            cursor = parent
        else:
            return False                         # too deep: caller falls back to full sync
        for blk in reversed(pending):
            res = self.chain.add_block(blk)
            if res.status == "rejected":
                self._penalize(addr, PENALTY_BAD_BLOCK)
                return False
        return self.chain.add_block(block).status != "rejected"
