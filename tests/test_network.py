"""Real OS processes talking over real localhost sockets (no mocks, no shared memory)."""
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from blockforge.blockchain import merkle
from blockforge.blockchain.wallet import Wallet
from tests.helpers import free_port, wait_until

ROOT = Path(__file__).resolve().parents[1]
CID = "test-net"


class NodeProc:
    def __init__(self, tmp_path, name, *, peers=(), difficulty=6, alloc=None, miner=None, chain_id=CID):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.addr = f"127.0.0.1:{self.port}"
        self.cmd = [sys.executable, str(ROOT / "run.py"), "node", "--port", str(self.port),
                    "--data-dir", str(tmp_path / name), "--difficulty", str(difficulty),
                    "--chain-id", chain_id, "--log-level", "WARNING"]
        for p in peers:
            self.cmd += ["--peers", p]
        for a, n in (alloc or {}).items():
            self.cmd += ["--genesis-alloc", f"{a}={n}"]
        if miner:
            self.cmd += ["--miner", miner]
        self.proc = None
        self.client = httpx.Client(base_url=self.url, timeout=30)

    def start(self):
        self.proc = subprocess.Popen(self.cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env={**os.environ, "BLOCKFORGE_HEALTH_CHECK_INTERVAL": "1"})
        wait_until(self.alive, timeout=30, msg=f"node {self.port} up")
        return self

    def alive(self):
        try:
            return self.client.get("/api/v1/node").status_code == 200
        except httpx.HTTPError:
            return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.client.close()

    def get(self, path):
        return self.client.get(path).json()

    def post(self, path, **kw):
        return self.client.post(path, **kw)

    def info(self):
        return self.get("/api/v1/chain/info")

    def mine(self, addr, n=1):
        for _ in range(n):
            assert self.post("/api/v1/mine", json={"address": addr}).status_code == 200


@pytest.fixture
def cluster(tmp_path):
    procs = []

    def make(name, **kw):
        p = NodeProc(tmp_path, name, **kw).start()
        procs.append(p)
        return p
    yield make
    for p in procs:
        p.stop()


def converged(*nodes):
    infos = [n.info() for n in nodes]
    return len({(i["tip_hash"], i["state_root"]) for i in infos}) == 1


def test_handshake_rejects_genesis_mismatch(cluster):
    a = cluster("a")
    b = cluster("b", difficulty=7)                      # different genesis
    r = a.post("/api/v1/peers", json={"address": b.addr})
    assert r.status_code == 502
    assert a.get("/api/v1/peers")["peers"] == [] and b.get("/api/v1/peers")["peers"] == []
    hello = {k: v for k, v in b.get("/p2p/status").items() if k != "tip_hash"}   # status has one extra field
    r = httpx.post(f"{a.url}/p2p/handshake", json={**hello, "addr": b.addr})
    assert r.status_code == 400 and r.json()["error"]["code"] == "GENESIS_MISMATCH"


def test_tx_and_block_propagation_and_late_joiner(cluster):
    alice, bob = Wallet.generate(), Wallet.generate()
    alloc = {alice.address: 1000}
    a = cluster("a", alloc=alloc)
    b = cluster("b", alloc=alloc, peers=[a.addr])
    wait_until(lambda: len(a.get("/api/v1/peers")["peers"]) == 1, msg="a sees b")
    tx = alice.create_transaction(bob.address, 40, 2, 0, CID)
    assert a.post("/api/v1/transactions", json=tx.to_dict()).status_code == 202
    wait_until(lambda: b.get("/api/v1/mempool")["count"] == 1, msg="tx propagation")
    a.mine(alice.address, 3)
    wait_until(lambda: converged(a, b) and b.info()["height"] == 3, msg="block propagation")
    assert b.get(f"/api/v1/accounts/{bob.address}")["balance"] == 40
    assert b.get("/api/v1/mempool")["count"] == 0
    late = cluster("late", alloc=alloc, peers=[b.addr])        # joins after the fact
    wait_until(lambda: converged(a, late) and late.info()["height"] == 3, msg="late joiner sync")
    assert late.get("/api/v1/chain/validate")["valid"] is True
    p = late.get(f"/api/v1/merkle/proof/{tx.tx_id}")
    assert merkle.verify_proof(tx.tx_id, p["proof"], p["merkle_root"])


def test_fork_resolution_across_processes(cluster):
    alice, bob = Wallet.generate(), Wallet.generate()
    alloc = {alice.address: 1000}
    a = cluster("a", alloc=alloc)
    b = cluster("b", alloc=alloc)                               # not connected: independent chains
    ma, mb = Wallet.generate().address, Wallet.generate().address
    pay = alice.create_transaction(bob.address, 25, 1, 0, CID)
    assert a.post("/api/v1/transactions", json=pay.to_dict()).status_code == 202
    a.mine(ma, 2)
    b.mine(mb, 3)
    assert a.info()["tip_hash"] != b.info()["tip_hash"]
    assert a.get(f"/api/v1/accounts/{bob.address}")["balance"] == 25
    assert a.post("/api/v1/peers", json={"address": b.addr}).status_code == 200   # reconnect
    wait_until(lambda: converged(a, b), msg="fork resolution")
    assert a.info()["height"] == 3
    (ev,) = a.get("/api/v1/chain/forks")["forks"]
    assert ev["ancestor_height"] == 0 and ev["depth"] == 2 and ev["new_branch_length"] == 3
    assert a.get(f"/api/v1/accounts/{bob.address}")["balance"] == 0
    assert a.get("/api/v1/mempool")["count"] == 1               # orphaned tx returned
    assert b.get("/api/v1/chain/forks")["forks"] == []


def test_restart_persists_chain_and_peers(cluster, tmp_path):
    a = cluster("a")
    b = cluster("b", peers=[a.addr])
    wait_until(lambda: len(a.get("/api/v1/peers")["peers"]) == 1)
    m = Wallet.generate().address
    a.mine(m, 2)
    wait_until(lambda: converged(a, b))
    before = b.info()
    b.stop()
    b.client = httpx.Client(base_url=b.url, timeout=30)
    b.start()
    after = b.info()
    assert (after["tip_hash"], after["height"], after["state_root"]) == (before["tip_hash"], 2, before["state_root"])
    wait_until(lambda: len(b.get("/api/v1/peers")["peers"]) == 1, msg="persisted peer reloaded")


def test_cli_against_real_node(cluster, tmp_path):
    from tests.test_cli import run
    node = cluster("cli")
    d = str(tmp_path / "w")
    run("wallet", "create", "alice", "--data-dir", d)
    run("wallet", "create", "bob", "--data-dir", d)
    assert run("mine", "--address", "alice", "--node", node.url, "--data-dir", d).returncode == 0
    run("mine", "--address", "alice", "--node", node.url, "--data-dir", d)
    bal = run("balance", "alice", "--node", node.url, "--data-dir", d)
    assert '"balance": 100' in bal.stdout
    r = run("transaction", "send", "--from", "alice", "--to", "bob", "--amount", "30", "--fee", "2",
            "--node", node.url, "--data-dir", d)
    assert r.returncode == 0, r.stderr
    assert '"count": 1' in run("mempool", "--node", node.url).stdout
    run("mine", "--address", "alice", "--node", node.url, "--data-dir", d)
    assert '"balance": 30' in run("balance", "bob", "--node", node.url, "--data-dir", d).stdout
    assert run("validate", "--node", node.url).returncode == 0
    assert "height" in run("chain", "--node", node.url).stdout
    assert run("block", "1", "--node", node.url).returncode == 0
