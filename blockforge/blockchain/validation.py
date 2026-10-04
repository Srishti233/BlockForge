"""Block and chain validation. add_block() and validate_chain() share this code."""
from __future__ import annotations

from typing import Any, Optional, Sequence

from blockforge.blockchain import crypto
from blockforge.blockchain.block import ZERO_HASH, Block, BlockHeader
from blockforge.blockchain.consensus import meets_target
from blockforge.blockchain.merkle import merkle_root
from blockforge.blockchain.state import Diff, StateError, WorldState
from blockforge.blockchain.transaction import (
    COINBASE,
    Transaction,
    ValidationError,
    validate_transaction_stateless,
)
from blockforge.config import Config

MAX_FUTURE_SECONDS = 2 * 60 * 60


def build_genesis(cfg: Config) -> tuple[Block, WorldState]:
    """Deterministic genesis: same config -> same hash on every node."""
    state = WorldState({a: (amt, 0) for a, amt in cfg.genesis_allocations.items()})
    header = BlockHeader(
        height=0, prev_hash=ZERO_HASH, merkle_root=merkle_root([]), state_root=state.root(),
        timestamp=cfg.genesis_timestamp, difficulty_bits=cfg.difficulty_bits,
        nonce=0, miner="GENESIS")
    return Block(header=header, transactions=()), state


def check_block_structure(block: Block, parent: BlockHeader, parent_hash: str, cfg: Config,
                          now: int) -> list[ValidationError]:
    """Every check that does not need account state. Collects all errors."""
    errs: list[ValidationError] = []
    h = block.header
    real_hash = block.hash

    def add(code: str, msg: str) -> None:
        errs.append(ValidationError(code, msg))

    if block.claimed_hash is not None and block.claimed_hash != real_hash:
        add("BAD_HASH", f"stored hash {block.claimed_hash[:12]} != recomputed {real_hash[:12]}")
    if h.height != parent.height + 1:
        add("BAD_HEIGHT", f"height {h.height}, expected {parent.height + 1}")
    if h.prev_hash != parent_hash:
        add("BAD_PREV_HASH", f"prev_hash {h.prev_hash[:12]} != parent hash {parent_hash[:12]}")
    if h.difficulty_bits != cfg.difficulty_bits:
        add("BAD_DIFFICULTY", f"difficulty {h.difficulty_bits}, expected {cfg.difficulty_bits}")
    elif not meets_target(real_hash, h.difficulty_bits):
        add("BAD_POW", "block hash does not meet the difficulty target")
    if h.timestamp < parent.timestamp:
        add("BAD_TIMESTAMP", "timestamp earlier than parent's")
    if h.timestamp > now + MAX_FUTURE_SECONDS:
        add("BAD_TIMESTAMP", "timestamp more than 2 hours in the future")

    txs = block.transactions
    if len(txs) > cfg.max_tx_per_block + 1:
        add("TOO_MANY_TXS", f"{len(txs)} transactions (max {cfg.max_tx_per_block} + coinbase)")
    if block.size_bytes() > cfg.max_block_bytes:
        add("BLOCK_TOO_LARGE", f"block exceeds {cfg.max_block_bytes} bytes")
    if merkle_root(block.tx_ids) != h.merkle_root:
        add("BAD_MERKLE", "merkle_root does not match transactions")
    if len(set(block.tx_ids)) != len(txs):
        add("DUPLICATE", "duplicate transaction ids inside block")

    coinbases = [i for i, t in enumerate(txs) if t.sender == COINBASE]
    if len(coinbases) == 0:
        add("NO_COINBASE", "block has no coinbase transaction")
    else:
        if len(coinbases) > 1:
            add("MULTIPLE_COINBASE", f"{len(coinbases)} coinbase transactions")
        if coinbases[0] != 0:
            add("BAD_COINBASE", "coinbase must be the first transaction")
        cb = txs[coinbases[0]]
        if (cb.fee != 0 or cb.signature != "" or cb.public_key != "" or cb.nonce != h.height
                or cb.recipient != h.miner or cb.chain_id != cfg.chain_id
                or cb.timestamp != h.timestamp or cb.tx_id != cb.compute_id()):
            add("BAD_COINBASE", "coinbase fields are not canonical for this block")
        fees = sum(t.fee for t in txs if t.sender != COINBASE)
        if cb.amount != cfg.block_reward + fees:
            add("BAD_COINBASE_AMOUNT",
                f"coinbase pays {cb.amount}, expected {cfg.block_reward + fees}")
    if not crypto.is_valid_address(h.miner):
        add("MALFORMED", "miner is not a valid address")
    for i, tx in enumerate(txs):
        if tx.sender == COINBASE:
            continue
        e = validate_transaction_stateless(tx, cfg.chain_id)
        if e is not None:
            add(e.code, f"tx[{i}] {tx.tx_id[:12]}: {e.message}")
    return errs


def check_block_state(block: Block, state: WorldState) -> tuple[list[ValidationError], Optional[Diff]]:
    """Apply the block to `state` and verify the state root.

    Returns (errors, diff). diff is None when the transactions could not be applied
    (state untouched). If diff is not None the state HAS been advanced; the caller
    must call state.undo_block(diff) if it wants to reject the block.
    """
    try:
        diff = state.apply_block(block)
    except StateError as exc:
        return [ValidationError(exc.code, exc.message)], None
    errs: list[ValidationError] = []
    if state.root() != block.header.state_root:
        errs.append(ValidationError("BAD_STATE_ROOT", "header state_root does not match resulting state"))
    return errs, diff


def validate_block(block: Block, parent: BlockHeader, parent_hash: str, state: WorldState,
                   cfg: Config, now: int) -> tuple[list[ValidationError], Optional[Diff]]:
    """Full validation of a block that extends the chain `state` currently represents."""
    errs = check_block_structure(block, parent, parent_hash, cfg, now)
    state_errs, diff = check_block_state(block, state)
    return errs + state_errs, diff


def validate_chain(blocks: Sequence[Block], cfg: Config, now: int) -> dict[str, Any]:
    """Replay from genesis, collecting ALL errors.

    Returns {valid, checked_blocks, tip_hash, errors:[{height, code, message}]}.
    """
    errors: list[dict[str, Any]] = []

    def err(height: int, code: str, message: str) -> None:
        errors.append({"height": height, "code": code, "message": message})

    if not blocks:
        err(0, "NO_GENESIS", "chain is empty")
        return {"valid": False, "checked_blocks": 0, "tip_hash": None, "errors": errors}

    expected_genesis, state = build_genesis(cfg)
    g = blocks[0]
    if g.hash != expected_genesis.hash or g.transactions:
        err(0, "BAD_GENESIS", "genesis block does not match the configured genesis")
    if g.claimed_hash is not None and g.claimed_hash != g.hash:
        err(0, "BAD_HASH", "stored genesis hash does not match recomputed hash")
    # Continue from the *configured* genesis state, whatever the stored genesis says.
    parent_header, parent_hash = g.header, g.hash
    state_usable = True
    for blk in blocks[1:]:
        height = blk.header.height
        block_errs = check_block_structure(blk, parent_header, parent_hash, cfg, now)
        if state_usable:
            st_errs, diff = check_block_state(blk, state)
            block_errs += st_errs
            if diff is None:
                # State cannot advance past a block whose txs do not apply.
                state_usable = False
                err(height, "STATE_REPLAY_STOPPED",
                    "later blocks' balances/nonces/state roots are not checked after this point")
        for e in block_errs:
            err(height, e.code, e.message)
        parent_header, parent_hash = blk.header, blk.hash
    return {"valid": not errors, "checked_blocks": len(blocks), "tip_hash": blocks[-1].hash,
            "errors": errors}
