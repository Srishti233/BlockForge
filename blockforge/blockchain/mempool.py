"""Mempool: dedupe, per-sender nonce ordering, fee-priority selection, size cap."""
from __future__ import annotations

import heapq
import threading
from typing import Iterable, Optional

from blockforge.blockchain.state import WorldState
from blockforge.blockchain.transaction import (
    Transaction,
    ValidationError,
    validate_transaction_stateless,
)


class Mempool:
    def __init__(self, chain_id: str, max_size: int = 5000) -> None:
        self.chain_id = chain_id
        self.max_size = max_size
        self._txs: dict[str, Transaction] = {}
        self._by_sender: dict[str, dict[int, Transaction]] = {}
        self._lock = threading.RLock()

    def __len__(self) -> int:
        return len(self._txs)

    def __contains__(self, tx_id: str) -> bool:
        return tx_id in self._txs

    def get(self, tx_id: str) -> Optional[Transaction]:
        return self._txs.get(tx_id)

    def all(self) -> list[Transaction]:
        with self._lock:
            return sorted(self._txs.values(), key=lambda t: (-t.fee, t.timestamp, t.tx_id))

    # ------------------------------------------------------------------ add
    def add(self, tx: Transaction, state: WorldState) -> Optional[ValidationError]:
        """Validate against the current chain state and queue. Returns an error or None.

        Rule: a sender's pending nonces must form a gapless run starting at the
        account's confirmed nonce, and the pending total must be affordable.
        """
        with self._lock:
            if tx.tx_id in self._txs:
                return ValidationError("DUPLICATE", "transaction already in mempool")
            err = validate_transaction_stateless(tx, self.chain_id)
            if err is not None:
                return err
            pending = self._by_sender.get(tx.sender, {})
            confirmed_nonce = state.nonce(tx.sender)
            if tx.nonce < confirmed_nonce:
                return ValidationError(
                    "BAD_NONCE", f"nonce {tx.nonce} already used (next is {confirmed_nonce})")
            if tx.nonce in pending:
                return ValidationError(
                    "BAD_NONCE", f"nonce {tx.nonce} already has a pending transaction (double-spend)")
            expected = confirmed_nonce + len(pending)
            if tx.nonce != expected:
                return ValidationError("BAD_NONCE", f"nonce {tx.nonce}, expected {expected}")
            committed = sum(t.amount + t.fee for t in pending.values())
            if state.balance(tx.sender) < committed + tx.amount + tx.fee:
                return ValidationError(
                    "INSUFFICIENT_BALANCE",
                    f"balance {state.balance(tx.sender)} cannot cover pending {committed} "
                    f"+ {tx.amount + tx.fee}")
            if len(self._txs) >= self.max_size:
                worst = min(self._txs.values(), key=lambda t: (t.fee, -t.timestamp))
                if tx.fee <= worst.fee:
                    return ValidationError("MEMPOOL_FULL", "mempool full and fee too low")
                self._remove_with_dependents(worst)
            self._txs[tx.tx_id] = tx
            self._by_sender.setdefault(tx.sender, {})[tx.nonce] = tx
            return None

    # --------------------------------------------------------------- remove
    def _remove_one(self, tx: Transaction) -> None:
        self._txs.pop(tx.tx_id, None)
        per = self._by_sender.get(tx.sender)
        if per is not None:
            per.pop(tx.nonce, None)
            if not per:
                del self._by_sender[tx.sender]

    def _remove_with_dependents(self, tx: Transaction) -> None:
        """Remove tx and every later-nonce tx from the same sender (they'd have a gap)."""
        for t in [t for t in self._by_sender.get(tx.sender, {}).values() if t.nonce >= tx.nonce]:
            self._remove_one(t)

    def remove_included(self, tx_ids: Iterable[str]) -> None:
        with self._lock:
            for tid in list(tx_ids):
                tx = self._txs.get(tid)
                if tx is not None:
                    self._remove_one(tx)

    def revalidate(self, state: WorldState, extra: Iterable[Transaction] = ()) -> list[str]:
        """Rebuild the pool against `state`, optionally folding in `extra` transactions
        (e.g. those from orphaned blocks). Anything no longer valid is dropped.
        Returns the ids that were removed (or rejected)."""
        with self._lock:
            merged = {t.tx_id: t for t in list(self._txs.values()) + list(extra)}
            self._txs.clear()
            self._by_sender.clear()
            removed = []
            for tx in sorted(merged.values(), key=lambda t: (t.sender, t.nonce)):
                if self.add(tx, state) is not None:
                    removed.append(tx.tx_id)
            return removed

    # --------------------------------------------------------------- select
    def select(self, max_count: int, state: WorldState) -> list[Transaction]:
        """Highest-fee-first selection that never breaks a sender's nonce order."""
        with self._lock:
            next_nonce: dict[str, int] = {}
            heap: list[tuple[int, int, str, Transaction]] = []
            counter = 0

            def push(sender: str) -> None:
                nonlocal counter
                nn = next_nonce.get(sender, state.nonce(sender))
                tx = self._by_sender.get(sender, {}).get(nn)
                if tx is not None:
                    counter += 1
                    heapq.heappush(heap, (-tx.fee, counter, tx.tx_id, tx))

            for sender in self._by_sender:
                push(sender)
            chosen: list[Transaction] = []
            while heap and len(chosen) < max_count:
                _, _, _, tx = heapq.heappop(heap)
                chosen.append(tx)
                next_nonce[tx.sender] = tx.nonce + 1
                push(tx.sender)
            return chosen
