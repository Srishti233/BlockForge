import dataclasses

from blockforge.blockchain import merkle
from blockforge.blockchain.block import Block, BlockHeader
from blockforge.blockchain.blockchain import Blockchain
from blockforge.blockchain.consensus import mine_header
from blockforge.blockchain.transaction import Transaction
from blockforge.blockchain.validation import validate_chain
from blockforge.blockchain.wallet import Wallet
from tests.helpers import make_cfg, mine, mine_unadded

CID = "test-chain"


def setup_pair(tmp_path, **kw):
    alice, bob, carol = Wallet.generate(), Wallet.generate(), Wallet.generate()
    cfg = make_cfg(tmp_path, genesis_allocations={alice.address: 1000}, **kw)
    return alice, bob, carol, cfg


def test_valid_chain_with_transactions(tmp_path):
    alice, bob, carol, cfg = setup_pair(tmp_path)
    chain = Blockchain(cfg)
    mine(chain, bob.address)
    assert chain.submit_transaction(alice.create_transaction(carol.address, 100, 3, 0, CID)) is None
    mine(chain, bob.address)
    assert chain.state.balance(alice.address) == 1000 - 103
    assert chain.state.balance(carol.address) == 100
    assert chain.state.balance(bob.address) == 50 + 50 + 3
    assert chain.state.nonce(alice.address) == 1
    rep = chain.validate()
    assert rep["valid"] and rep["checked_blocks"] == 3 and rep["tip_hash"] == chain.tip_hash()
    assert rep["errors"] == []
    assert len(chain.mempool) == 0
    info = chain.find_tx(chain.tip().transactions[1].tx_id)
    assert info["confirmations"] == 1 and info["block_height"] == 2
    root = info["merkle_root"]
    assert merkle.verify_proof(info["transaction"]["tx_id"], info["merkle_proof"], root)
    assert chain.account(alice.address)["nonce"] == 1
    assert chain.find_tx("00" * 32) is None


def test_pending_tx_lookup_and_account(tmp_path):
    alice, bob, carol, cfg = setup_pair(tmp_path)
    chain = Blockchain(cfg)
    tx = alice.create_transaction(bob.address, 5, 1, 0, CID)
    assert chain.submit_transaction(tx) is None
    assert chain.find_tx(tx.tx_id)["status"] == "pending"
    acct = chain.account(alice.address)
    assert acct["next_nonce"] == 1 and acct["pending_transactions"] == 1


def test_tampered_chain_reports_all_errors(tmp_path):
    alice, bob, carol, cfg = setup_pair(tmp_path)
    chain = Blockchain(cfg)
    mine(chain, bob.address)
    chain.submit_transaction(alice.create_transaction(carol.address, 100, 3, 0, CID))
    mine(chain, bob.address)
    mine(chain, bob.address)
    mine(chain, bob.address)
    blocks = [chain.blocks[h] for h in chain.main]
    assert validate_chain(blocks, cfg, int(chain.clock()))["valid"]

    tampered = list(blocks)
    # (1) change the amount inside block 2's transaction
    b2 = blocks[2]
    evil = dataclasses.replace(b2.transactions[1], amount=900)
    tampered[2] = Block(b2.header, (b2.transactions[0], evil), claimed_hash=b2.hash)
    # (2) corrupt block 3's state root (changes its hash, so block 4's prev_hash no longer links)
    b3 = blocks[3]
    tampered[3] = Block(dataclasses.replace(b3.header, state_root="ab" * 32), b3.transactions,
                        claimed_hash=b3.hash)
    rep = validate_chain(tampered, cfg, int(chain.clock()))
    assert not rep["valid"]
    by_height = {}
    for e in rep["errors"]:
        by_height.setdefault(e["height"], set()).add(e["code"])
    assert {"MALFORMED", "BAD_STATE_ROOT"} <= by_height[2]   # tx body no longer matches its id; state differs
    assert {"BAD_HASH", "BAD_STATE_ROOT"} <= by_height[3]
    assert "BAD_PREV_HASH" in by_height[4]                    # child of the altered block
    assert 1 not in by_height
    assert rep["checked_blocks"] == 5
    assert all({"height", "code", "message"} <= set(e) for e in rep["errors"])


def test_bad_genesis_and_empty():
    cfg = make_cfg()
    other = make_cfg(difficulty_bits=7)
    from blockforge.blockchain.validation import build_genesis
    g = build_genesis(other)[0]
    rep = validate_chain([g], cfg, 2_000_000_000)
    assert [e["code"] for e in rep["errors"]] == ["BAD_GENESIS"]
    assert validate_chain([], cfg, 1)["errors"][0]["code"] == "NO_GENESIS"


def test_state_replay_stops_when_txs_do_not_apply(tmp_path):
    alice, bob, carol, cfg = setup_pair(tmp_path)
    chain = Blockchain(cfg)
    mine(chain, bob.address)
    chain.submit_transaction(alice.create_transaction(carol.address, 100, 3, 0, CID))
    mine(chain, bob.address)
    blocks = [chain.blocks[h] for h in chain.main]
    b2 = blocks[2]
    overspend = alice.create_transaction(carol.address, 5000, 1, 0, CID)   # signed, but unaffordable
    cb = Transaction.coinbase(bob.address, cfg.block_reward + 1, 2, b2.header.timestamp, CID)
    ids = [cb.tx_id, overspend.tx_id]
    hdr = dataclasses.replace(b2.header, merkle_root=merkle.merkle_root(ids))
    bad = Block(mine_header(hdr), (cb, overspend))
    rep = validate_chain([blocks[0], blocks[1], bad], cfg, int(chain.clock()))
    codes = {e["code"] for e in rep["errors"]}
    assert "INSUFFICIENT_BALANCE" in codes and "STATE_REPLAY_STOPPED" in codes


def test_replay_attacks_rejected_in_chain(tmp_path):
    alice, bob, carol, cfg = setup_pair(tmp_path)
    chain = Blockchain(cfg)
    tx = alice.create_transaction(bob.address, 10, 1, 0, CID)
    assert chain.submit_transaction(tx) is None
    assert chain.submit_transaction(tx).code == "DUPLICATE"
    mine(chain, carol.address)
    assert chain.submit_transaction(tx).code == "BAD_NONCE"        # replay after confirmation
    other = alice.create_transaction(bob.address, 10, 1, 0, "other-chain")
    assert chain.submit_transaction(other).code == "WRONG_CHAIN"
    dbl = alice.create_transaction(carol.address, 900, 1, 1, CID)
    assert chain.submit_transaction(dbl) is None
    spend2 = alice.create_transaction(carol.address, 900, 1, 2, CID)
    assert chain.submit_transaction(spend2).code == "INSUFFICIENT_BALANCE"


def build_competing(tmp_path, a_blocks, b_blocks, **kw):
    alice, bob, carol, cfg = setup_pair(tmp_path, **kw)
    A, B = Blockchain(cfg), Blockchain(cfg)
    ma, mb = Wallet.generate().address, Wallet.generate().address
    return alice, bob, carol, cfg, A, B, ma, mb


def test_fork_choice_by_work_and_reorg(tmp_path):
    alice, bob, carol, cfg, A, B, ma, mb = build_competing(tmp_path, 2, 3)
    pay = alice.create_transaction(carol.address, 100, 5, 0, CID)
    assert A.submit_transaction(pay) is None
    a_blocks = [mine(A, ma), mine(A, ma)]
    assert pay.tx_id not in A.mempool and A.state.balance(carol.address) == 100
    b_blocks = [mine(B, mb) for _ in range(3)]
    assert A.tip_hash() != B.tip_hash()

    # equal work: tie keeps current chain
    assert A.add_block(b_blocks[0]).status == "side"
    res = A.add_block(b_blocks[1])
    assert res.status == "side" and A.tip_hash() == a_blocks[1].hash
    # strictly more work: reorg
    res = A.add_block(b_blocks[2])
    assert res.status == "reorg"
    ev = res.fork_event
    assert ev["ancestor_height"] == 0 and ev["depth"] == 2 and ev["new_branch_length"] == 3
    assert ev["old_tip"] == a_blocks[1].hash and ev["new_tip"] == b_blocks[2].hash
    assert ev["orphaned_txs"] == 1 and ev["orphaned_txs_returned"] == 1
    assert A.tip_hash() == B.tip_hash()
    assert A.state.root() == B.state.root()
    assert A.state.balance(carol.address) == 0                 # rolled back
    assert A.state.balance(ma) == 0 and A.state.balance(mb) == 150
    assert pay.tx_id in A.mempool                              # orphaned tx returned
    assert A.fork_events == [ev]
    assert A.validate()["valid"]
    assert A.find_tx(pay.tx_id)["status"] == "pending"
    # the old branch now mines on top of... nothing: re-adding its tip is a duplicate
    assert A.add_block(a_blocks[1]).status == "duplicate"
    # and the returned tx can be mined into the new chain
    mine(A, ma)
    assert A.state.balance(carol.address) == 100 and pay.tx_id not in A.mempool


def test_same_blocks_same_state_root_on_independent_chains(tmp_path):
    alice, bob, carol, cfg, A, B, ma, mb = build_competing(tmp_path, 0, 0)
    A.submit_transaction(alice.create_transaction(bob.address, 40, 2, 0, CID))
    mine(A, ma)
    A.submit_transaction(alice.create_transaction(carol.address, 10, 1, 1, CID))
    mine(A, ma)
    for h in A.main[1:]:
        assert B.add_block(A.blocks[h]).status == "accepted"
    assert A.state.root() == B.state.root() and A.tip_hash() == B.tip_hash()


def test_orphan_blocks_are_adopted_when_parent_arrives(tmp_path):
    alice, bob, carol, cfg, A, B, ma, mb = build_competing(tmp_path, 0, 0)
    b1, b2, b3 = mine(B, mb), mine(B, mb), mine(B, mb)
    r = A.add_block(b3)
    assert r.status == "orphan" and r.missing_parent == b2.hash
    assert A.add_block(b2).status == "orphan"
    assert A.add_block(b1).status == "accepted"
    assert A.tip_hash() == b3.hash and not A.orphans


def test_invalid_branch_is_rolled_back(tmp_path):
    alice, bob, carol, cfg, A, B, ma, mb = build_competing(tmp_path, 2, 3)
    mine(A, ma)
    mine(A, ma)
    root_before, tip_before = A.state.root(), A.tip_hash()
    b1 = mine(B, mb)
    good2 = mine_unadded(B, mb)
    B.add_block(good2)
    bad2 = Block(mine_header(dataclasses.replace(good2.header, state_root="cd" * 32)), good2.transactions)
    ts = bad2.header.timestamp
    cb = Transaction.coinbase(mb, cfg.block_reward, 3, ts, CID)
    h3 = BlockHeader(3, bad2.hash, merkle.merkle_root([cb.tx_id]), "ef" * 32, ts, cfg.difficulty_bits, 0, mb)
    bad3 = Block(mine_header(h3), (cb,))
    assert A.add_block(b1).status == "side"
    assert A.add_block(bad2).status == "side"
    res = A.add_block(bad3)
    assert res.status == "rejected" and "BAD_STATE_ROOT" in {e.code for e in res.errors}
    assert A.tip_hash() == tip_before and A.state.root() == root_before
    assert bad2.hash in A.invalid and bad3.hash in A.invalid
    assert A.add_block(bad3).errors[0].code == "KNOWN_INVALID"
    child_of_bad = A.add_block(Block(mine_header(dataclasses.replace(h3, prev_hash=bad2.hash, nonce=0,
                                                                         state_root="01" * 32)), (cb,)))
    assert child_of_bad.status == "rejected"
    assert A.validate()["valid"]


def test_max_reorg_depth_refused(tmp_path):
    alice, bob, carol, cfg, A, B, ma, mb = build_competing(tmp_path, 2, 3, max_reorg_depth=1)
    mine(A, ma)
    mine(A, ma)
    blocks = [mine(B, mb) for _ in range(3)]
    A.add_block(blocks[0])
    A.add_block(blocks[1])
    res = A.add_block(blocks[2])
    assert res.status == "side" and res.errors[0].code == "REORG_TOO_DEEP"
    assert A.fork_events == []


def test_rejected_blocks_do_not_change_state(tmp_path):
    alice, bob, carol, cfg, A, B, ma, mb = build_competing(tmp_path, 1, 1)
    blk = mine_unadded(A, ma)
    bad = Block(mine_header(dataclasses.replace(blk.header, state_root="12" * 32)), blk.transactions)
    root = A.state.root()
    res = A.add_block(bad)
    assert res.status == "rejected" and A.state.root() == root and A.height() == 0
    assert A.add_block(blk).status == "accepted"
    events = []
    A.listeners.append(lambda b, r: events.append(r.status))
    mine(A, ma)
    assert events == ["accepted"]
    A.listeners.append(lambda b, r: 1 / 0)       # a faulty listener must not break consensus
    mine(A, ma)
    assert A.height() == 3


def test_block_listing_queries(tmp_path):
    alice, bob, carol, cfg, A, B, ma, mb = build_competing(tmp_path, 1, 1)
    for _ in range(4):
        mine(A, ma)
    assert [b.height for b in A.list_blocks(2, 0)] == [4, 3]
    assert [b.height for b in A.list_blocks(10, 3)] == [1, 0]
    assert [b.height for b in A.main_blocks(2, 2)] == [2, 3]
    assert A.get_block("3").hash == A.main[3] == A.get_block(A.main[3]).hash
    assert A.get_block(99) is None and A.get_block("ab" * 32) is None
    assert A.is_main(A.main[2]) and not A.is_main("00" * 32)
    assert A.info()["height"] == 4 and A.info()["side_blocks"] == 0
