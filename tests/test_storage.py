import dataclasses
import sqlite3

import pytest

from blockforge.blockchain.block import Block
from blockforge.blockchain.blockchain import Blockchain, ChainIntegrityError, validate_database
from blockforge.blockchain.wallet import Wallet
from blockforge.storage.database import Database
from tests.helpers import make_cfg, mine, mine_unadded

CID = "test-chain"


def setup(tmp_path):
    alice, bob = Wallet.generate(), Wallet.generate()
    cfg = make_cfg(tmp_path, genesis_allocations={alice.address: 1000})
    return alice, bob, cfg, tmp_path / "chain.db"


def test_restart_reloads_tip_balances_peers_and_forks(tmp_path):
    alice, bob, cfg, path = setup(tmp_path)
    chain = Blockchain(cfg, Database(path))
    chain.submit_transaction(alice.create_transaction(bob.address, 30, 2, 0, CID))
    mine(chain, bob.address)
    mine(chain, bob.address)
    chain.db.save_peer("127.0.0.1:6000", "peer-1", 123)
    chain.db.save_peer("127.0.0.1:6001", None, 124)
    chain.db.remove_peer("127.0.0.1:6001")
    tip, root = chain.tip_hash(), chain.state.root()
    chain.db.close()

    again = Blockchain(cfg, Database(path))
    assert again.tip_hash() == tip and again.height() == 2
    assert again.state.root() == root
    assert again.state.balance(bob.address) == 30 + 2 + 100
    assert again.state.balance(alice.address) == 1000 - 32
    assert again.db.load_peers() == [{"addr": "127.0.0.1:6000", "node_id": "peer-1", "last_seen": 123}]
    assert again.validate()["valid"]
    assert again.find_tx(again.tip().transactions[0].tx_id)["status"] == "confirmed"


def test_reorg_persists(tmp_path):
    alice, bob, cfg, path = setup(tmp_path)
    A = Blockchain(cfg, Database(path))
    B = Blockchain(cfg)
    ma, mb = Wallet.generate().address, Wallet.generate().address
    mine(A, ma)
    bl = [mine(B, mb), mine(B, mb)]
    for b in bl:
        A.add_block(b)
    assert A.tip_hash() == B.tip_hash() and len(A.fork_events) == 1
    A.db.close()
    again = Blockchain(cfg, Database(path))
    assert again.tip_hash() == B.tip_hash() and again.state.root() == B.state.root()
    assert len(again.fork_events) == 1 and again.fork_events[0]["depth"] == 1
    assert again.height() == 2 and again.info()["side_blocks"] == 1


def test_commit_is_atomic(tmp_path):
    alice, bob, cfg, path = setup(tmp_path)
    db = Database(path)
    chain = Blockchain(cfg, db)
    blk = mine_unadded(chain, bob.address)

    def boom():
        raise RuntimeError("simulated crash mid-commit")

    with pytest.raises(RuntimeError):
        db.commit_main_block(blk, 123, _fail_hook=boom)
    assert db.get_tip() == chain.genesis.hash
    assert db.count_blocks() == 1                       # no partial block row
    db.close()
    assert Blockchain(cfg, Database(path)).height() == 0


def test_chain_state_unchanged_when_db_commit_fails(tmp_path):
    alice, bob, cfg, path = setup(tmp_path)
    db = Database(path)
    chain = Blockchain(cfg, db)
    root = chain.state.root()
    blk = mine_unadded(chain, bob.address)
    original = db.commit_main_block

    def failing(*a, **k):
        raise sqlite3.OperationalError("disk full")

    db.commit_main_block = failing
    with pytest.raises(sqlite3.OperationalError):
        chain.add_block(blk)
    db.commit_main_block = original
    assert chain.state.root() == root and chain.height() == 0
    assert chain.add_block(blk).status == "accepted"


def test_tampered_database_detected(tmp_path):
    alice, bob, cfg, path = setup(tmp_path)
    chain = Blockchain(cfg, Database(path))
    chain.submit_transaction(alice.create_transaction(bob.address, 30, 2, 0, CID))
    for _ in range(3):
        mine(chain, bob.address)
    chain.db.close()
    assert validate_database(cfg, str(path))["valid"]

    conn = sqlite3.connect(path)
    (data,) = conn.execute("SELECT data FROM blocks WHERE height=2").fetchone()
    import json
    d = json.loads(data)
    d["header"]["state_root"] = "9" * 64
    conn.execute("UPDATE blocks SET data=? WHERE height=2", (json.dumps(d),))
    conn.commit()
    conn.close()

    report = validate_database(cfg, str(path))
    assert not report["valid"]
    codes = {(e["height"], e["code"]) for e in report["errors"]}
    assert (2, "BAD_HASH") in codes and (2, "BAD_STATE_ROOT") in codes and (3, "BAD_PREV_HASH") in codes
    with pytest.raises(ChainIntegrityError) as ei:
        Blockchain(cfg, Database(path))
    assert ei.value.report["errors"]


def test_db_with_other_genesis_refused(tmp_path):
    alice, bob, cfg, path = setup(tmp_path)
    Blockchain(cfg, Database(path)).db.close()
    other = make_cfg(tmp_path, difficulty_bits=7)
    with pytest.raises(ChainIntegrityError):
        Blockchain(other, Database(path))


def test_parameterized_queries_resist_injection(tmp_path):
    db = Database(tmp_path / "x.db")
    evil = "x'); DROP TABLE blocks; --"
    db.save_peer(evil, "n", 1)
    assert db.load_peers()[0]["addr"] == evil
    assert db.count_blocks() == 0


def test_wal_mode_enabled(tmp_path):
    db = Database(tmp_path / "w.db")
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
