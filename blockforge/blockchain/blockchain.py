"""The Blockchain: block index, fork choice, reorgs, mempool, mining templates, queries."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from blockforge.blockchain import merkle
from blockforge.blockchain.block import Block, BlockHeader
from blockforge.blockchain.consensus import block_work, mine_header
from blockforge.blockchain.mempool import Mempool
from blockforge.blockchain.state import Diff, WorldState
from blockforge.blockchain.transaction import Transaction, ValidationError
from blockforge.blockchain.validation import (
    build_genesis,
    check_block_state,
    check_block_structure,
    validate_block,
    validate_chain,
)
from blockforge.config import Config
from blockforge.logging_setup import log_event
from blockforge.storage.database import Database

log = logging.getLogger("blockforge.chain")
MAX_ORPHANS = 256


class ChainIntegrityError(Exception):
    """The stored chain failed validation on load. `report` has the details."""

    def __init__(self, message: str, report: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class AddResult:
    """Outcome of add_block. status: accepted | side | reorg | duplicate | orphan | rejected."""
    status: str
    block_hash: str
    errors: list[ValidationError] = field(default_factory=list)
    missing_parent: Optional[str] = None
    fork_event: Optional[dict[str, Any]] = None

    @property
    def is_new(self) -> bool:
        return self.status in ("accepted", "side", "reorg")

    @property
    def ok(self) -> bool:
        return self.status in ("accepted", "side", "reorg", "duplicate")


class Blockchain:
    def __init__(self, cfg: Config, db: Optional[Database] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.db = db
        self.clock = clock
        self.lock = threading.RLock()
        self.genesis, genesis_state = build_genesis(cfg)
        gh = self.genesis.hash
        self.blocks: dict[str, Block] = {gh: self.genesis}
        self.cum_work: dict[str, int] = {gh: block_work(cfg.difficulty_bits)}
        self.main: list[str] = [gh]
        self.state: WorldState = genesis_state
        self.diffs: dict[str, Diff] = {}
        self.invalid: set[str] = set()
        self.orphans: dict[str, Block] = {}
        self.mempool = Mempool(cfg.chain_id, cfg.mempool_max)
        self.fork_events: list[dict[str, Any]] = []
        self.tx_index: dict[str, tuple[str, int]] = {}
        self.listeners: list[Callable[[Block, AddResult], None]] = []
        if db is not None:
            self._load_or_init()

    # ================================================================ persistence
    def _load_or_init(self) -> None:
        assert self.db is not None
        if self.db.get_tip() is None:
            self.db.commit_main_block(self.genesis, self.cum_work[self.genesis.hash])
            return
        main = [b for b, _ in self.db.load_blocks("main")]
        report = validate_chain(main, self.cfg, int(self.clock()))
        if not report["valid"]:
            raise ChainIntegrityError(
                f"stored chain failed validation ({len(report['errors'])} errors)", report)
        if main[0].hash != self.genesis.hash:  # pragma: no cover - covered by validate_chain
            raise ChainIntegrityError("stored genesis differs from configured genesis")
        for blk in main[1:]:
            self._register(blk)
            self.diffs[blk.hash] = self.state.apply_block(blk)
            self.main.append(blk.hash)
        if self.db.get_tip() != self.main[-1]:
            raise ChainIntegrityError("stored tip does not match the main chain")
        for blk, _ in self.db.load_blocks("side"):
            if blk.header.prev_hash in self.blocks:
                self._register(blk)
        for blk, _ in self.db.load_blocks("invalid"):
            self.invalid.add(blk.hash)
        self.fork_events = self.db.load_fork_events()
        self._rebuild_tx_index()
        self.mempool.revalidate(self.state)

    def _register(self, blk: Block) -> None:
        self.blocks[blk.hash] = blk
        self.cum_work[blk.hash] = (self.cum_work[blk.header.prev_hash]
                                   + block_work(blk.header.difficulty_bits))

    def _rebuild_tx_index(self) -> None:
        self.tx_index = {}
        for h in self.main:
            blk = self.blocks[h]
            for i, tx in enumerate(blk.transactions):
                self.tx_index[tx.tx_id] = (h, i)

    # ================================================================== read side
    def height(self) -> int:
        return len(self.main) - 1

    def tip_hash(self) -> str:
        return self.main[-1]

    def tip(self) -> Block:
        return self.blocks[self.main[-1]]

    def cumulative_work(self) -> int:
        return self.cum_work[self.main[-1]]

    def state_root(self) -> str:
        return self.state.root()

    def has_block(self, h: str) -> bool:
        return h in self.blocks

    def get_block_by_height(self, height: int) -> Optional[Block]:
        with self.lock:
            return self.blocks[self.main[height]] if 0 <= height < len(self.main) else None

    def get_block(self, height_or_hash: str | int) -> Optional[Block]:
        with self.lock:
            if isinstance(height_or_hash, int) or str(height_or_hash).isdigit():
                return self.get_block_by_height(int(height_or_hash))
            return self.blocks.get(str(height_or_hash))

    def is_main(self, h: str) -> bool:
        with self.lock:
            blk = self.blocks.get(h)
            return blk is not None and blk.height < len(self.main) and self.main[blk.height] == h

    def main_blocks(self, start: int, limit: int) -> list[Block]:
        with self.lock:
            return [self.blocks[h] for h in self.main[max(start, 0):max(start, 0) + limit]]

    def list_blocks(self, limit: int, offset: int) -> list[Block]:
        """Newest first."""
        with self.lock:
            hashes = list(reversed(self.main))[offset:offset + limit]
            return [self.blocks[h] for h in hashes]

    def find_tx(self, tx_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            loc = self.tx_index.get(tx_id)
            if loc is None:
                pending = self.mempool.get(tx_id)
                if pending is None:
                    return None
                return {"transaction": pending.to_dict(), "status": "pending", "block_hash": None,
                        "block_height": None, "index": None, "confirmations": 0,
                        "merkle_proof": None, "merkle_root": None}
            bh, idx = loc
            blk = self.blocks[bh]
            return {
                "transaction": blk.transactions[idx].to_dict(), "status": "confirmed",
                "block_hash": bh, "block_height": blk.height, "index": idx,
                "confirmations": self.height() - blk.height + 1,
                "merkle_root": blk.header.merkle_root,
                "merkle_proof": merkle.merkle_proof(blk.tx_ids, idx),
            }

    def account(self, address: str) -> dict[str, Any]:
        with self.lock:
            confirmed = self.state.nonce(address)
            pending = [t for t in self.mempool.all() if t.sender == address]
            return {"address": address, "balance": self.state.balance(address),
                    "nonce": confirmed, "next_nonce": confirmed + len(pending),
                    "pending_transactions": len(pending)}

    def info(self) -> dict[str, Any]:
        with self.lock:
            tip = self.tip()
            return {
                "chain_id": self.cfg.chain_id, "genesis_hash": self.genesis.hash,
                "height": self.height(), "tip_hash": tip.hash,
                "state_root": self.state.root(), "difficulty_bits": self.cfg.difficulty_bits,
                "block_reward": self.cfg.block_reward, "cumulative_work": str(self.cumulative_work()),
                "mempool_size": len(self.mempool), "tip_timestamp": tip.header.timestamp,
                "total_blocks_known": len(self.blocks), "side_blocks": len(self.blocks) - len(self.main),
            }

    def validate(self) -> dict[str, Any]:
        """Full replay of the current main chain from genesis."""
        with self.lock:
            blocks = [self.blocks[h] for h in self.main]
        report = validate_chain(blocks, self.cfg, int(self.clock()))
        if not report["valid"]:
            log_event(log, "validation_failure", logging.ERROR, errors=len(report["errors"]))
        return report

    # ================================================================== transactions
    def submit_transaction(self, tx: Transaction) -> Optional[ValidationError]:
        with self.lock:
            err = self.mempool.add(tx, self.state)
        if err is None:
            log_event(log, "tx_accepted", tx=tx.tx_id[:12], sender=tx.sender[:10], amount=tx.amount)
        else:
            log_event(log, "tx_rejected", logging.WARNING, tx=tx.tx_id[:12], code=err.code,
                      reason=err.message)
        return err

    # ================================================================== adding blocks
    def add_block(self, block: Block, now: Optional[int] = None) -> AddResult:
        with self.lock:
            now = int(self.clock()) if now is None else now
            result = self._add_one(block, now)
            if result.is_new:
                self._adopt_orphans(block.hash, now)
            return result

    def _adopt_orphans(self, parent_hash: str, now: int) -> None:
        stack = [parent_hash]
        while stack:
            ph = stack.pop()
            for oh in [h for h, o in self.orphans.items() if o.header.prev_hash == ph]:
                orphan = self.orphans.pop(oh)
                res = self._add_one(orphan, now)
                if res.is_new:
                    stack.append(oh)

    def _notify(self, block: Block, result: AddResult) -> None:
        for fn in list(self.listeners):
            try:
                fn(block, result)
            except Exception:  # listeners must never break consensus
                log.exception("block listener failed")

    def _reject(self, block: Block, errs: list[ValidationError]) -> AddResult:
        log_event(log, "block_rejected", logging.WARNING, hash=block.hash[:12], height=block.height,
                  errors=",".join(sorted({e.code for e in errs})))
        return AddResult("rejected", block.hash, errs)

    def _add_one(self, block: Block, now: int) -> AddResult:
        h = block.hash
        if h in self.invalid:
            return AddResult("rejected", h, [ValidationError("KNOWN_INVALID", "block previously found invalid")])
        if h in self.blocks:
            return AddResult("duplicate", h)
        prev = block.header.prev_hash
        if prev in self.invalid:
            return AddResult("rejected", h, [ValidationError("KNOWN_INVALID", "parent is invalid")])
        parent = self.blocks.get(prev)
        if parent is None:
            if len(self.orphans) >= MAX_ORPHANS:
                self.orphans.pop(next(iter(self.orphans)))
            self.orphans[h] = block
            log_event(log, "block_orphan", hash=h[:12], height=block.height, missing=prev[:12])
            return AddResult("orphan", h, missing_parent=prev)
        log_event(log, "block_received", hash=h[:12], height=block.height)

        if prev == self.main[-1]:
            errs, diff = validate_block(block, parent.header, parent.hash, self.state, self.cfg, now)
            if errs:
                if diff is not None:
                    self.state.undo_block(diff)
                return self._reject(block, errs)
            self._connect(block, diff or {})
            result = AddResult("accepted", h)
            log_event(log, "block_accepted", hash=h[:12], height=block.height, txs=len(block.transactions))
            self._notify(block, result)
            return result

        errs = check_block_structure(block, parent.header, parent.hash, self.cfg, now)
        if errs:
            return self._reject(block, errs)
        self._register(block)
        if self.db is not None:
            self.db.save_side_block(block, self.cum_work[h])
        if self.cum_work[h] > self.cumulative_work():
            result = self._reorg_to(h)
        else:
            result = AddResult("side", h)
            log_event(log, "block_side_branch", hash=h[:12], height=block.height)
        if result.is_new:
            self._notify(block, result)
        return result

    def _connect(self, block: Block, diff: Diff) -> None:
        """Make `block` (already applied to self.state) the new tip."""
        h = block.hash
        work = self.cum_work[block.header.prev_hash] + block_work(block.header.difficulty_bits)
        if self.db is not None:
            try:
                self.db.commit_main_block(block, work)
            except BaseException:
                self.state.undo_block(diff)
                raise
        self.blocks[h] = block
        self.cum_work[h] = work
        self.diffs[h] = diff
        self.main.append(h)
        for i, tx in enumerate(block.transactions):
            self.tx_index[tx.tx_id] = (h, i)
        self.mempool.remove_included(block.tx_ids)
        self.mempool.revalidate(self.state)

    # ------------------------------------------------------------------ reorg
    def _reorg_to(self, new_tip: str) -> AddResult:
        branch: list[str] = []
        cur = new_tip
        while not self.is_main(cur):
            branch.append(cur)
            cur = self.blocks[cur].header.prev_hash
        branch.reverse()
        anc_hash = cur
        anc_height = self.blocks[anc_hash].height
        old_tip = self.main[-1]
        old_hashes = self.main[anc_height + 1:]
        depth = len(old_hashes)
        if depth > self.cfg.max_reorg_depth:
            err = ValidationError("REORG_TOO_DEEP",
                                  f"reorg depth {depth} exceeds max {self.cfg.max_reorg_depth}")
            log_event(log, "reorg_refused", logging.WARNING, depth=depth)
            return AddResult("side", new_tip, [err])

        old_txs = [t for h in old_hashes for t in self.blocks[h].transactions if not t.is_coinbase]
        for h in reversed(old_hashes):
            self.state.undo_block(self.diffs.pop(h))
        applied: list[str] = []

        def rollback() -> None:
            for hh in reversed(applied):
                self.state.undo_block(self.diffs.pop(hh))
            for oh in old_hashes:
                self.diffs[oh] = self.state.apply_block(self.blocks[oh])

        for h in branch:
            errs, diff = check_block_state(self.blocks[h], self.state)
            if errs:
                if diff is not None:
                    self.state.undo_block(diff)
                rollback()
                bad = self._descendants_inclusive(h)
                failing = self.blocks[h]
                self.invalid.update(bad)
                if self.db is not None:
                    self.db.mark_invalid(list(bad))
                for bh in bad:  # invalid blocks are forgotten in memory; the hashes stay in self.invalid
                    self.blocks.pop(bh, None)
                    self.cum_work.pop(bh, None)
                return self._reject(failing, errs)
            self.diffs[h] = diff or {}
            applied.append(h)

        removed = set(self.mempool.revalidate(self.state, extra=old_txs))
        event = {"time": int(self.clock()), "ancestor_height": anc_height,
                 "ancestor_hash": anc_hash, "old_tip": old_tip, "new_tip": new_tip,
                 "depth": depth, "new_branch_length": len(branch),
                 "orphaned_txs": len(old_txs),
                 "orphaned_txs_returned": sum(1 for t in old_txs if t.tx_id in self.mempool)}
        if self.db is not None:
            try:
                self.db.commit_reorg(old_hashes, [(self.blocks[h], self.cum_work[h]) for h in branch],
                                     new_tip, event)
            except BaseException:
                rollback()
                self.mempool.revalidate(self.state)
                raise
        self.main = self.main[:anc_height + 1] + branch
        self.fork_events.append(event)
        self._rebuild_tx_index()
        log_event(log, "fork_reorg", ancestor=anc_height, depth=depth, new_len=len(branch),
                  old_tip=old_tip[:12], new_tip=new_tip[:12],
                  txs_returned=event["orphaned_txs_returned"], txs_dropped=len(removed))
        return AddResult("reorg", new_tip, fork_event=event)

    def _descendants_inclusive(self, h: str) -> set[str]:
        out = {h}
        grew = True
        while grew:
            grew = False
            for bh, blk in self.blocks.items():
                if bh not in out and blk.header.prev_hash in out:
                    out.add(bh)
                    grew = True
        return out

    # ================================================================== mining
    def build_template(self, miner: str, now: Optional[int] = None) -> Block:
        """An unsolved block (nonce 0) containing the best mempool txs + coinbase."""
        with self.lock:
            now = int(self.clock()) if now is None else now
            tip = self.tip()
            height = tip.height + 1
            ts = max(now, tip.header.timestamp)
            txs = self.mempool.select(self.cfg.max_tx_per_block, self.state)
            while True:
                fees = sum(t.fee for t in txs)
                cb = Transaction.coinbase(miner, self.cfg.block_reward + fees, height, ts,
                                          self.cfg.chain_id)
                body = (cb, *txs)
                header = BlockHeader(
                    height=height, prev_hash=tip.hash, merkle_root=merkle.merkle_root([t.tx_id for t in body]),
                    state_root="", timestamp=ts, difficulty_bits=self.cfg.difficulty_bits,
                    nonce=0, miner=miner)
                trial = Block(header, body)
                if trial.size_bytes() <= self.cfg.max_block_bytes or not txs:
                    break
                txs = txs[:-1]
            diff = self.state.apply_block(trial)
            root = self.state.root()
            self.state.undo_block(diff)
            header = BlockHeader(**{**header.to_dict(), "state_root": root})
            return Block(header, body)

    def mine_block(self, miner: str, stop_event: Optional[threading.Event] = None) -> Optional[Block]:
        """Build a template and search for a nonce. Returns None if interrupted.

        The search runs outside the chain lock so the node stays responsive.
        """
        template = self.build_template(miner)
        solved = mine_header(template.header, stop_event)
        if solved is None:
            return None
        log_event(log, "block_mined", height=solved.height, hash=solved.hash()[:12], nonce=solved.nonce)
        return Block(solved, template.transactions)


def validate_database(cfg: Config, db_path: str) -> dict[str, Any]:
    """Open a DB file read-only-ish and run validate_chain on its stored main chain.

    Unlike constructing a Blockchain (which refuses a corrupt DB), this reports every error.
    """
    db = Database(db_path)
    try:
        main = [b for b, _ in db.load_blocks("main")]
    finally:
        db.close()
    return validate_chain(main, cfg, int(time.time()))
