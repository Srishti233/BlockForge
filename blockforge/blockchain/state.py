"""Account-model world state with atomic block application and exact undo."""
from __future__ import annotations

from typing import Optional

from blockforge.blockchain import crypto
from blockforge.blockchain.block import Block
from blockforge.blockchain.transaction import COINBASE

# A diff maps address -> previous (balance, nonce), or None if the account did not exist.
Diff = dict[str, Optional[tuple[int, int]]]


class StateError(Exception):
    """A transaction cannot be applied. `code` matches the structured error codes."""

    def __init__(self, code: str, message: str, tx_index: int = -1) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.tx_index = tx_index


class WorldState:
    def __init__(self, accounts: Optional[dict[str, tuple[int, int]]] = None) -> None:
        self.accounts: dict[str, tuple[int, int]] = dict(accounts or {})

    # ---- reads
    def balance(self, address: str) -> int:
        return self.accounts.get(address, (0, 0))[0]

    def nonce(self, address: str) -> int:
        """The nonce the account's NEXT transaction must use."""
        return self.accounts.get(address, (0, 0))[1]

    def copy(self) -> "WorldState":
        return WorldState(self.accounts)

    def root(self) -> str:
        """SHA-256 of canonical JSON of {address: [balance, nonce]}."""
        return crypto.sha256_hex(crypto.canonical_json(
            {a: [b, n] for a, (b, n) in self.accounts.items()}))

    # ---- writes
    def apply_block(self, block: Block) -> Diff:
        """Apply all transactions or none. Returns the diff needed by undo_block."""
        touched: dict[str, tuple[int, int]] = {}

        def get(addr: str) -> tuple[int, int]:
            return touched.get(addr) or self.accounts.get(addr, (0, 0))

        for i, tx in enumerate(block.transactions):
            if tx.sender == COINBASE:
                if i != 0:
                    raise StateError("BAD_COINBASE", "coinbase must be the first transaction", i)
                bal, nonce = get(tx.recipient)
                touched[tx.recipient] = (bal + tx.amount, nonce)
                continue
            s_bal, s_nonce = get(tx.sender)
            if tx.nonce != s_nonce:
                raise StateError("BAD_NONCE",
                                 f"tx {tx.tx_id[:12]}: nonce {tx.nonce}, expected {s_nonce}", i)
            if s_bal < tx.amount + tx.fee:
                raise StateError("INSUFFICIENT_BALANCE",
                                 f"tx {tx.tx_id[:12]}: balance {s_bal} < {tx.amount + tx.fee}", i)
            touched[tx.sender] = (s_bal - tx.amount - tx.fee, s_nonce + 1)
            r_bal, r_nonce = get(tx.recipient)
            touched[tx.recipient] = (r_bal + tx.amount, r_nonce)
        # Commit: nothing above mutated self.accounts, so a raise leaves state untouched.
        diff: Diff = {a: self.accounts.get(a) for a in touched}
        self.accounts.update(touched)
        return diff

    def undo_block(self, diff: Diff) -> None:
        for addr, old in diff.items():
            if old is None:
                self.accounts.pop(addr, None)
            else:
                self.accounts[addr] = old
