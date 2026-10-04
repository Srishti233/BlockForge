import dataclasses

import pytest

from blockforge.blockchain import merkle
from blockforge.blockchain.block import Block, BlockHeader
from blockforge.blockchain.blockchain import Blockchain
from blockforge.blockchain.consensus import block_work, meets_target, mine_header, target_for
from blockforge.blockchain.transaction import MalformedError, Transaction
from blockforge.blockchain.validation import build_genesis, validate_block
from blockforge.blockchain.wallet import Wallet
from tests.helpers import make_cfg, mine, mine_unadded

import threading


def rebuild(block: Block, **hdr) -> Block:
    """Re-solve PoW after editing header fields (so only the intended check fails)."""
    header = dataclasses.replace(block.header, **hdr)
    solved = mine_header(header)
    assert solved is not None
    return Block(solved, block.transactions)


def check(chain: Blockchain, block: Block):
    parent = chain.tip()
    errs, diff = validate_block(block, parent.header, parent.hash, chain.state.copy(), chain.cfg,
                                int(chain.clock()))
    return {e.code for e in errs}


def test_hash_determinism_and_serialization(cfg):
    chain = Blockchain(cfg)
    miner = Wallet.generate()
    blk = mine_unadded(chain, miner.address)
    assert blk.hash == blk.header.hash() == Block.from_dict(blk.to_dict()).hash
    assert blk.header.with_nonce(blk.header.nonce + 1).hash() != blk.hash
    assert Block.from_dict(blk.to_dict()).claimed_hash == blk.hash


def test_genesis_deterministic_and_config_sensitive(tmp_path):
    a = Wallet.generate()
    c1 = make_cfg(tmp_path, genesis_allocations={a.address: 100})
    c2 = make_cfg(tmp_path, genesis_allocations={a.address: 100})
    c3 = make_cfg(tmp_path, genesis_allocations={a.address: 101})
    c4 = make_cfg(tmp_path, difficulty_bits=7, genesis_allocations={a.address: 100})
    g1, s1 = build_genesis(c1)
    assert g1.hash == build_genesis(c2)[0].hash
    assert g1.hash != build_genesis(c3)[0].hash
    assert g1.hash != build_genesis(c4)[0].hash
    assert s1.balance(a.address) == 100 and g1.header.state_root == s1.root()


def test_target_and_work():
    assert target_for(0) == 2 ** 256
    assert block_work(0) == 0            # why config requires difficulty_bits >= 1
    assert block_work(1) == 1
    assert block_work(8) == 2 ** 8 - 1   # 2**256 // (2**248 + 1)
    assert block_work(9) > block_work(8)
    assert meets_target("0" * 64, 40) and not meets_target("f" * 64, 1)


def test_mine_header_interrupt(cfg):
    chain = Blockchain(cfg)
    tpl = chain.build_template(Wallet.generate().address)
    hard = dataclasses.replace(tpl.header, difficulty_bits=40)
    stop = threading.Event()
    stop.set()
    assert mine_header(hard, stop) is None
    assert mine_header(dataclasses.replace(tpl.header, difficulty_bits=0), None, max_nonce=0) is None


def test_valid_block_passes(cfg):
    chain = Blockchain(cfg)
    blk = mine_unadded(chain, Wallet.generate().address)
    assert check(chain, blk) == set()


def test_bad_prev_hash_and_height(cfg):
    chain = Blockchain(cfg)
    blk = mine_unadded(chain, Wallet.generate().address)
    assert "BAD_PREV_HASH" in check(chain, rebuild(blk, prev_hash="11" * 32))
    assert "BAD_HEIGHT" in check(chain, rebuild(blk, height=5))


def test_bad_block_hash_claim(cfg):
    chain = Blockchain(cfg)
    blk = mine_unadded(chain, Wallet.generate().address)
    forged = dataclasses.replace(blk, claimed_hash="00" * 32)
    assert "BAD_HASH" in check(chain, forged)


def test_bad_nonce_pow(cfg):
    cfg = make_cfg(difficulty_bits=16)
    chain = Blockchain(cfg)
    blk = mine_unadded(chain, Wallet.generate().address)
    for n in range(blk.header.nonce + 1, blk.header.nonce + 50):
        bad = Block(blk.header.with_nonce(n), blk.transactions)
        if not meets_target(bad.hash, 16):
            assert "BAD_POW" in check(chain, bad)
            return
    raise AssertionError("could not find a failing nonce")


def test_difficulty_enforced(cfg):
    chain = Blockchain(cfg)
    blk = mine_unadded(chain, Wallet.generate().address)
    assert "BAD_DIFFICULTY" in check(chain, rebuild(blk, difficulty_bits=1))


def test_wrong_merkle_and_state_root(cfg):
    chain = Blockchain(cfg)
    blk = mine_unadded(chain, Wallet.generate().address)
    assert "BAD_MERKLE" in check(chain, rebuild(blk, merkle_root="22" * 32))
    assert "BAD_STATE_ROOT" in check(chain, rebuild(blk, state_root="33" * 32))


def test_coinbase_rules(cfg):
    chain = Blockchain(cfg)
    miner = Wallet.generate()
    blk = mine_unadded(chain, miner.address)
    cb = blk.transactions[0]
    # double coinbase
    cb2 = Transaction.coinbase(miner.address, cfg.block_reward, 2, blk.header.timestamp, cfg.chain_id)
    two = (cb, cb2)
    b2 = rebuild(Block(blk.header, two), merkle_root=merkle.merkle_root([t.tx_id for t in two]))
    assert "MULTIPLE_COINBASE" in check(chain, b2)
    # wrong amount
    rich = Transaction.coinbase(miner.address, cfg.block_reward + 1, 1, blk.header.timestamp, cfg.chain_id)
    b3 = rebuild(Block(blk.header, (rich,)), merkle_root=merkle.merkle_root([rich.tx_id]))
    assert "BAD_COINBASE_AMOUNT" in check(chain, b3)
    # no coinbase
    b4 = rebuild(Block(blk.header, ()), merkle_root=merkle.merkle_root([]))
    assert "NO_COINBASE" in check(chain, b4)
    # coinbase not first
    other = Wallet.generate()
    chain2 = Blockchain(make_cfg(genesis_allocations={other.address: 100}))
    tx = other.create_transaction(miner.address, 5, 1, 0, "test-chain")
    assert chain2.submit_transaction(tx) is None
    good = mine_unadded(chain2, miner.address)
    swapped = (good.transactions[1], good.transactions[0])
    b5 = rebuild(Block(good.header, swapped), merkle_root=merkle.merkle_root([t.tx_id for t in swapped]))
    assert "BAD_COINBASE" in check(chain2, b5)


def test_timestamp_rules(cfg):
    chain = Blockchain(cfg)
    miner = Wallet.generate().address
    blk = mine_unadded(chain, miner)
    assert "BAD_TIMESTAMP" in check(chain, rebuild(blk, timestamp=cfg.genesis_timestamp - 1))
    far = int(chain.clock()) + 3 * 3600
    assert "BAD_TIMESTAMP" in check(chain, rebuild(blk, timestamp=far))


def test_size_and_count_limits(tmp_path):
    a, b = Wallet.generate(), Wallet.generate()
    chain = Blockchain(make_cfg(tmp_path, genesis_allocations={a.address: 100}))
    for n in range(2):
        assert chain.submit_transaction(a.create_transaction(b.address, 1, 1, n, "test-chain")) is None
    blk = mine_unadded(chain, b.address)
    assert len(blk.transactions) == 3 and check(chain, blk) == set()
    strict = make_cfg(tmp_path, max_tx_per_block=1, genesis_allocations={a.address: 100})
    parent = chain.tip()
    errs = {e.code for e in validate_block(blk, parent.header, parent.hash, chain.state.copy(),
                                           strict, int(chain.clock()))[0]}
    assert "TOO_MANY_TXS" in errs
    tiny = make_cfg(tmp_path, max_block_bytes=1000, genesis_allocations={a.address: 100})
    errs = {e.code for e in validate_block(blk, parent.header, parent.hash, chain.state.copy(),
                                           tiny, int(chain.clock()))[0]}
    assert "BLOCK_TOO_LARGE" in errs


def test_template_respects_max_tx(tmp_path):
    a, b = Wallet.generate(), Wallet.generate()
    chain = Blockchain(make_cfg(tmp_path, max_tx_per_block=1, genesis_allocations={a.address: 100}))
    for n in range(2):
        assert chain.submit_transaction(a.create_transaction(b.address, 1, 1, n, "test-chain")) is None
    assert len(chain.build_template(b.address).transactions) == 2   # coinbase + 1 tx


def test_from_dict_errors():
    with pytest.raises(MalformedError):
        Block.from_dict({"header": {}, "transactions": []})
    with pytest.raises(MalformedError):
        Block.from_dict("x")
    with pytest.raises(MalformedError):
        Block.from_dict({"header": {}, "transactions": "no"})
    hdr = BlockHeader(1, "0" * 64, "0" * 64, "0" * 64, 1, 1, 0, "m").to_dict()
    with pytest.raises(MalformedError):
        BlockHeader.from_dict({**hdr, "height": "1"})
    with pytest.raises(MalformedError):
        BlockHeader.from_dict({**hdr, "miner": 5})
    with pytest.raises(MalformedError):
        Block.from_dict({"header": hdr, "transactions": [], "hash": 5})
