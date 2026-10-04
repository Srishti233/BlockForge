"""Protocol logic over an in-process transport (fast, deterministic)."""
import dataclasses

import pytest

from blockforge.blockchain.wallet import Wallet
from blockforge.network.peer import PeerTable, RateLimiter, SeenSet
from blockforge.network.protocol import ProtocolError, check_handshake, valid_peer_addr
from tests.helpers import LocalNetwork, make_node, mine, stop_nodes, wait_until

CID = "test-chain"


def pair(tmp_path, **kw):
    net = LocalNetwork()
    a = make_node(net, tmp_path / "a", 7001, **kw)
    b = make_node(net, tmp_path / "b", 7002, **kw)
    return net, a, b


def test_handshake_and_peer_exchange(tmp_path):
    net, a, b = pair(tmp_path)
    c = make_node(net, tmp_path / "c", 7003)
    try:
        assert a.connect_peer(b.addr) and b.peers.get(a.addr) is not None   # both sides registered
        assert c.connect_peer(b.addr)
        wait_until(lambda: c.peers.get(a.addr) is not None, msg="peer discovery via exchange")
        assert a.addr in b.peer_list() and c.addr in b.peer_list()
        assert not a.connect_peer(a.addr) and not a.connect_peer("not-an-address")
    finally:
        stop_nodes(a, b, c)


def test_handshake_rejects_genesis_and_chain_mismatch(tmp_path):
    net = LocalNetwork()
    a = make_node(net, tmp_path / "a", 7001)
    other_genesis = make_node(net, tmp_path / "b", 7002, difficulty_bits=7)
    other_chain = make_node(net, tmp_path / "c", 7003, chain_id="other")
    try:
        assert not a.connect_peer(other_genesis.addr)
        assert not other_genesis.connect_peer(a.addr)
        assert not a.connect_peer(other_chain.addr)
        assert a.peers.all() == [] and other_genesis.peers.all() == []
        with pytest.raises(ProtocolError) as ei:
            a.handle_handshake(other_genesis.handshake_info())
        assert ei.value.code == "GENESIS_MISMATCH"
        info = a.handshake_info()
        for bad, code in [({**info, "chain_id": "x"}, "WRONG_CHAIN"), ({**info, "addr": "nope"}, "BAD_HANDSHAKE"),
                          ({**info, "tip_height": -1}, "BAD_HANDSHAKE"), ({**info, "cumulative_work": "x"}, "BAD_HANDSHAKE"),
                          ({k: v for k, v in info.items() if k != "node_id"}, "BAD_HANDSHAKE"), ("str", "BAD_HANDSHAKE")]:
            with pytest.raises(ProtocolError) as ei:
                check_handshake(bad, chain_id=CID, genesis_hash=info["genesis_hash"])
            assert ei.value.code == code
    finally:
        stop_nodes(a, other_genesis, other_chain)


def test_tx_and_block_propagation(tmp_path):
    alice, bob = Wallet.generate(), Wallet.generate()
    net, a, b = pair(tmp_path, genesis_allocations={alice.address: 1000})
    try:
        assert a.connect_peer(b.addr)
        tx = alice.create_transaction(bob.address, 25, 2, 0, CID)
        assert a.submit_transaction(tx) is None
        wait_until(lambda: tx.tx_id in b.chain.mempool, msg="tx propagation")
        blk = a.mine_once(alice.address)
        wait_until(lambda: b.chain.tip_hash() == blk.hash, msg="block propagation")
        assert b.chain.state.root() == a.chain.state.root()
        wait_until(lambda: tx.tx_id not in b.chain.mempool, msg="mempool cleanup")
        assert b.chain.state.balance(bob.address) == 25
    finally:
        stop_nodes(a, b)


def test_gossip_does_not_loop(tmp_path):
    alice, bob = Wallet.generate(), Wallet.generate()
    net = LocalNetwork()
    nodes = [make_node(net, tmp_path / str(i), 7010 + i, genesis_allocations={alice.address: 100}) for i in range(3)]
    a, b, c = nodes
    try:
        a.connect_peer(b.addr); b.connect_peer(c.addr); c.connect_peer(a.addr)   # triangle
        tx = alice.create_transaction(bob.address, 5, 1, 0, CID)
        a.submit_transaction(tx)
        wait_until(lambda: all(tx.tx_id in n.chain.mempool for n in nodes))
        assert b.handle_tx(tx.to_dict(), a.addr) == {"status": "seen"}
        assert b.handle_tx({**tx.to_dict(), "tx_id": "0" * 64}, None)["status"] == "rejected"
        blk = a.mine_once(bob.address)
        wait_until(lambda: all(n.chain.tip_hash() == blk.hash for n in nodes))
        assert b.handle_block(blk.to_dict(), a.addr) == {"status": "seen"}
    finally:
        stop_nodes(*nodes)


def test_late_joiner_syncs_and_orphan_ancestor_fetch(tmp_path):
    net, a, b = pair(tmp_path)
    miner = Wallet.generate().address
    try:
        for _ in range(7):
            a.mine_once(miner)
        late = make_node(net, tmp_path / "late", 7020)
        assert late.connect_peer(a.addr)                    # sees higher work, syncs on connect
        assert late.chain.tip_hash() == a.chain.tip_hash() and late.chain.height() == 7
        assert late.chain.validate()["valid"]
        # b receives block 7 first (parent unknown) -> fetches ancestors by hash
        blk7 = a.chain.get_block_by_height(7)
        assert b.handle_block(blk7.to_dict(), a.addr)["status"] == "orphan"
        wait_until(lambda: b.chain.height() == 7, msg="ancestor fetch")
        assert b.chain.state.root() == a.chain.state.root()
        stop_nodes(late)
    finally:
        stop_nodes(a, b)


def test_sync_in_multiple_batches(tmp_path):
    import blockforge.network.sync as sync_mod
    old = sync_mod.SYNC_BATCH
    sync_mod.SYNC_BATCH = 3
    net, a, b = pair(tmp_path)
    try:
        for _ in range(8):
            a.mine_once(Wallet.generate().address)
        assert b.connect_peer(a.addr)
        assert b.chain.height() == 8
    finally:
        sync_mod.SYNC_BATCH = old
        stop_nodes(a, b)


def test_fork_resolution_across_nodes(tmp_path):
    alice, bob = Wallet.generate(), Wallet.generate()
    net, a, b = pair(tmp_path, genesis_allocations={alice.address: 1000})
    ma, mb = Wallet.generate().address, Wallet.generate().address
    try:
        assert a.connect_peer(b.addr)
        base = a.mine_once(ma)
        wait_until(lambda: b.chain.tip_hash() == base.hash)
        # partition: both sides refuse each other
        a.partition([b.addr]); b.partition([a.addr])
        assert a.peers.all() == [] and b.peers.all() == []
        pay = alice.create_transaction(bob.address, 40, 3, 0, CID)
        a.submit_transaction(pay)                     # lives only on A's side
        a_blocks = [a.mine_once(ma) for _ in range(2)]
        b_blocks = [b.mine_once(mb) for _ in range(3)]
        assert a.chain.state.balance(bob.address) == 40 and b.chain.state.balance(bob.address) == 0
        assert a.chain.tip_hash() != b.chain.tip_hash()
        # heal and reconnect: A must adopt B's heavier chain
        a.heal(); b.heal()
        assert a.connect_peer(b.addr)
        assert a.chain.tip_hash() == b.chain.tip_hash() == b_blocks[-1].hash
        assert a.chain.state.root() == b.chain.state.root()
        assert a.chain.state.balance(bob.address) == 0            # state rolled back
        assert pay.tx_id in a.chain.mempool                       # orphaned tx returned
        (ev,) = a.chain.fork_events
        assert ev["ancestor_height"] == 1 and ev["depth"] == 2 and ev["new_branch_length"] == 3
        assert ev["old_tip"] == a_blocks[-1].hash and ev["new_tip"] == b_blocks[-1].hash
        assert b.chain.fork_events == []
        # and the next block A mines includes the returned tx, propagating to B
        blk = a.mine_once(ma)
        wait_until(lambda: b.chain.tip_hash() == blk.hash)
        assert b.chain.state.balance(bob.address) == 40
    finally:
        stop_nodes(a, b)


def test_hostile_input_scoring_and_ban(tmp_path):
    net, a, b = pair(tmp_path)
    try:
        assert a.connect_peer(b.addr)
        for i in range(4):
            with pytest.raises(ProtocolError):
                a.handle_tx({"garbage": i}, b.addr)
        assert a.peers.get(b.addr).score == 80
        with pytest.raises(ProtocolError):
            a.handle_tx("not even an object", b.addr)          # crosses 100 -> banned + disconnected
        assert a.peers.is_banned(b.addr) and a.peers.get(b.addr) is None
        with pytest.raises(ProtocolError) as ei:
            a.handle_handshake(b.handshake_info())
        assert ei.value.code == "BANNED"
        assert not a.connect_peer(b.addr)
        assert b.addr in a.peers.bans()
    finally:
        stop_nodes(a, b)


def test_invalid_block_from_peer_penalized_and_rejected(tmp_path):
    net, a, b = pair(tmp_path)
    miner = Wallet.generate().address
    try:
        a.connect_peer(b.addr)
        good = a.chain.mine_block(miner)
        bad = dataclasses.replace(good, header=dataclasses.replace(good.header, state_root="aa" * 32))
        res = b.handle_block(bad.to_dict(), a.addr)
        assert res["status"] == "rejected" and b.chain.height() == 0
        assert b.peers.get(a.addr).score == 50
        with pytest.raises(ProtocolError):
            b.handle_block({"header": 1}, a.addr)
    finally:
        stop_nodes(a, b)


def test_health_check_prunes_dead_peer_and_persists_peers(tmp_path):
    net, a, b = pair(tmp_path)
    try:
        assert a.connect_peer(b.addr)
        assert a.db.load_peers()[0]["addr"] == b.addr
        del net.nodes[b.addr]
        for _ in range(3):
            a.check_peers()
        assert a.peers.all() == [] and a.db.load_peers() == []
    finally:
        stop_nodes(a, b)


def test_miner_thread_mines_and_is_interrupted(tmp_path):
    net, a, b = pair(tmp_path)
    miner = Wallet.generate().address
    try:
        a.connect_peer(b.addr)
        a.miner.start(miner)
        wait_until(lambda: a.chain.height() >= 2, msg="miner mining")
        wait_until(lambda: b.chain.height() >= 2, msg="mined blocks gossiped")
        assert a.info()["mining"]
        a.miner.stop()
        h = a.chain.height()
        assert not a.miner.running and a.miner.blocks_mined >= 2
        assert a.chain.height() == h
        with pytest.raises(ValueError):
            a.miner.start("bad")
    finally:
        stop_nodes(a, b)


def test_helpers_peer_table_seen_and_rate_limit():
    seen = SeenSet(2)
    assert seen.add("a") and not seen.add("a") and seen.add("b") and seen.add("c")
    assert "a" not in seen and "c" in seen
    t = [0.0]
    rl = RateLimiter(1.0, clock=lambda: t[0])      # capacity 2
    assert rl.allow("x") and rl.allow("x") and not rl.allow("x")
    t[0] += 1.0
    assert rl.allow("x") and not rl.allow("x") and rl.allow("y")
    clock = [0.0]
    tbl = PeerTable(clock=lambda: clock[0], max_peers=1)
    assert tbl.add("1.1.1.1:1", "n") is not None and tbl.add("2.2.2.2:2", "n") is None
    assert tbl.penalize("1.1.1.1:1", 100) and tbl.is_banned("1.1.1.1:1") and tbl.add("1.1.1.1:1", "n") is None
    clock[0] += 1000
    assert not tbl.is_banned("1.1.1.1:1") and tbl.add("1.1.1.1:1", "n") is not None
    assert valid_peer_addr("localhost:80") and not valid_peer_addr("a:0") and not valid_peer_addr("a:99999") and not valid_peer_addr(5)
