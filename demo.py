#!/usr/bin/env python3
"""BlockForge end-to-end demo. Starts real node processes, narrates, and ASSERTS every claim.

    python demo.py

Uses temp dirs and free ports; all node processes are cleaned up on exit, error or Ctrl+C.
Educational software, no real monetary value.
"""
from __future__ import annotations

import atexit
import dataclasses
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from blockforge.blockchain import merkle
from blockforge.blockchain.blockchain import validate_database
from blockforge.blockchain.wallet import Wallet, WalletStore
from blockforge.config import Config

ROOT = Path(__file__).resolve().parent
CHAIN_ID = "blockforge-demo"
DIFFICULTY = 10
TMP = Path(tempfile.mkdtemp(prefix="blockforge-demo-"))
PROCS: list["NodeProc"] = []


# ------------------------------------------------------------------ narration
def banner(n: int, title: str) -> None:
    print(f"\n{'=' * 78}\n STEP {n}: {title}\n{'=' * 78}", flush=True)


def say(msg: str) -> None:
    print(f"  - {msg}", flush=True)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(f"DEMO CHECK FAILED: {msg}")
    print(f"  [ok] {msg}", flush=True)


def short(h: str) -> str:
    return h[:12] + "..."


def wait_until(cond: Callable[[], bool], timeout: float = 45.0, msg: str = "condition") -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for: {msg}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------- nodes
class NodeProc:
    def __init__(self, name: str, alloc: dict[str, int], peers: list[str] | None = None) -> None:
        self.name = name
        self.port = free_port()
        self.addr = f"127.0.0.1:{self.port}"
        self.url = f"http://{self.addr}"
        self.data_dir = TMP / name
        self.db_path = self.data_dir / f"node-{self.port}.db"
        self.cmd = [sys.executable, str(ROOT / "run.py"), "node", "--port", str(self.port),
                    "--data-dir", str(self.data_dir), "--difficulty", str(DIFFICULTY),
                    "--chain-id", CHAIN_ID, "--log-level", "WARNING", "--test-hooks"]
        for p in peers or []:
            self.cmd += ["--peers", p]
        for a, n in alloc.items():
            self.cmd += ["--genesis-alloc", f"{a}={n}"]
        self.proc: subprocess.Popen | None = None
        self.http = httpx.Client(base_url=self.url, timeout=60)
        PROCS.append(self)

    def start(self) -> "NodeProc":
        env = {**os.environ, "BLOCKFORGE_HEALTH_CHECK_INTERVAL": "1"}
        self.proc = subprocess.Popen(self.cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        wait_until(lambda: self.http.get("/api/v1/node").status_code == 200, msg=f"{self.name} to start")
        return self

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc = None

    def get(self, path: str) -> Any:
        r = self.http.get(path)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict | None = None) -> httpx.Response:
        return self.http.post(path, json=body)

    def info(self) -> dict:
        return self.get("/api/v1/chain/info")

    def balance(self, addr: str) -> int:
        return self.get(f"/api/v1/accounts/{addr}")["balance"]

    def next_nonce(self, addr: str) -> int:
        return self.get(f"/api/v1/accounts/{addr}")["next_nonce"]

    def mine(self, addr: str, n: int = 1) -> None:
        for _ in range(n):
            r = self.post("/api/v1/mine", {"address": addr})
            assert r.status_code == 200, r.text

    def send(self, w: Wallet, to: str, amount: int, fee: int, nonce: int | None = None,
             chain_id: str = CHAIN_ID) -> httpx.Response:
        nonce = self.next_nonce(w.address) if nonce is None else nonce
        return self.post("/api/v1/transactions", w.create_transaction(to, amount, fee, nonce, chain_id).to_dict())


def cleanup() -> None:
    for p in PROCS:
        try:
            p.stop()
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)


def converged(*nodes: NodeProc) -> bool:
    return len({(n.info()["tip_hash"], n.info()["state_root"]) for n in nodes}) == 1


# ------------------------------------------------------------------------ demo
def main() -> None:
    t0 = time.monotonic()
    print(f"BlockForge demo (temp dir {TMP})\nEducational blockchain: no real monetary value.")

    # ------------------------------------------------------------------ 1
    banner(1, "Create wallets, start 3 nodes, connect them")
    store = WalletStore(TMP / "wallets", scrypt_n=2 ** 14)
    for name in ("alice", "bob", "carol", "miner"):
        store.create(name, "demo-passphrase")
    alice, bob, carol, miner = (store.load(n, "demo-passphrase") for n in ("alice", "bob", "carol", "miner"))
    for n, w in (("alice", alice), ("bob", bob), ("carol", carol), ("miner", miner)):
        say(f"{n:<6} {w.address}   (keystore encrypted with scrypt + AES-256-GCM)")
    ks_text = (TMP / "wallets" / "alice.json").read_text()
    check(alice.private_hex() not in ks_text, "private key is not present in the keystore file")
    alloc = {alice.address: 1000, bob.address: 500}
    n1 = NodeProc("n1", alloc).start()
    n2 = NodeProc("n2", alloc, peers=[n1.addr]).start()
    n3 = NodeProc("n3", alloc, peers=[n1.addr]).start()
    nodes = [n1, n2, n3]
    wait_until(lambda: all(len(n.get("/api/v1/peers")["peers"]) == 2 for n in nodes),
               msg="full mesh via peer-list exchange")
    check(len({n.info()["genesis_hash"] for n in nodes}) == 1, "all nodes share the same genesis hash")
    say(f"genesis {short(n1.info()['genesis_hash'])}; every node sees 2 peers (n3 discovered n2 through n1)")

    # ------------------------------------------------------------------ 2
    banner(2, "Mine, send signed transactions, watch propagation and convergence")
    n1.mine(miner.address)
    wait_until(lambda: converged(*nodes) and n3.info()["height"] == 1, msg="block 1 propagation")
    check(True, f"block 1 mined on n1 reached n2 and n3 (tip {short(n1.info()['tip_hash'])})")
    tx1 = alice.create_transaction(carol.address, 100, 2, 0, CHAIN_ID)
    tx2 = bob.create_transaction(carol.address, 50, 1, 0, CHAIN_ID)
    check(n2.post("/api/v1/transactions", tx1.to_dict()).status_code == 202, "alice -> carol (100, fee 2) accepted by n2")
    check(n3.post("/api/v1/transactions", tx2.to_dict()).status_code == 202, "bob -> carol (50, fee 1) accepted by n3")
    wait_until(lambda: all(n.get("/api/v1/mempool")["count"] == 2 for n in nodes), msg="tx gossip")
    check(True, "both transactions gossiped to every node's mempool")
    n2.mine(miner.address)
    wait_until(lambda: converged(*nodes) and n1.info()["height"] == 2, msg="block 2 convergence")
    infos = [n.info() for n in nodes]
    check(len({i["tip_hash"] for i in infos}) == 1 and len({i["state_root"] for i in infos}) == 1,
          f"all 3 nodes: same tip {short(infos[0]['tip_hash'])} and same state root {short(infos[0]['state_root'])}")
    check(n3.balance(carol.address) == 150, "carol holds 150")
    check(n1.balance(alice.address) == 1000 - 102 and n1.balance(bob.address) == 500 - 51, "senders paid amount + fee")
    check(n1.balance(miner.address) == 50 + 50 + 3, "miner earned 2 rewards + 3 in fees")
    check(all(n.get("/api/v1/mempool")["count"] == 0 for n in nodes), "mempools emptied after inclusion")

    # ------------------------------------------------------------------ 3
    banner(3, "Merkle proof: generate, verify, tamper")
    proof = n1.get(f"/api/v1/merkle/proof/{tx1.tx_id}")
    say(f"tx {short(tx1.tx_id)} in block {proof['block_height']}, merkle root {short(proof['merkle_root'])}, "
        f"{len(proof['proof'])} proof step(s)")
    v = n1.post("/api/v1/merkle/verify", {"tx_id": tx1.tx_id, "proof": proof["proof"], "root": proof["merkle_root"]}).json()
    check(v["valid"] is True, "API verifies the genuine proof")
    check(merkle.verify_proof(tx1.tx_id, proof["proof"], proof["merkle_root"]), "standalone verify_proof() agrees")
    bad_proof = [dict(s) for s in proof["proof"]]
    bad_proof[0]["hash"] = "ab" * 32
    v = n1.post("/api/v1/merkle/verify", {"tx_id": tx1.tx_id, "proof": bad_proof, "root": proof["merkle_root"]}).json()
    check(v["valid"] is False, "a tampered proof (altered sibling hash) FAILS verification")
    v = n1.post("/api/v1/merkle/verify", {"tx_id": tx2.tx_id, "proof": proof["proof"], "root": proof["merkle_root"]}).json()
    check(v["valid"] is False, "a genuine proof presented for a different transaction FAILS")

    # ------------------------------------------------------------------ 4
    banner(4, "validate_chain(), then tamper with a block in a COPY of a database")
    rep = n1.get("/api/v1/chain/validate")
    check(rep["valid"] and rep["checked_blocks"] == 3 and not rep["errors"],
          f"live chain valid: {rep['checked_blocks']} blocks replayed from genesis, tip {short(rep['tip_hash'])}")
    cfg = Config(difficulty_bits=DIFFICULTY, chain_id=CHAIN_ID, genesis_allocations=alloc, data_dir=str(TMP))
    copy_path = TMP / "tampered-copy.db"
    src, dst = sqlite3.connect(n1.db_path), sqlite3.connect(copy_path)
    src.backup(dst)                                        # consistent snapshot while the node runs
    src.close()
    check(validate_database(cfg, str(copy_path))["valid"], "the untouched copy validates")
    (data,) = dst.execute("SELECT data FROM blocks WHERE height=2").fetchone()
    block = json.loads(data)
    victim = block["transactions"][1]
    say(f"attacker edits block 2: tx {short(victim['tx_id'])} amount {victim['amount']} -> {victim['amount'] * 10}")
    victim["amount"] *= 10
    dst.execute("UPDATE blocks SET data=? WHERE height=2", (json.dumps(block),))
    dst.commit()
    dst.close()
    report = validate_database(cfg, str(copy_path))
    check(not report["valid"] and report["errors"], f"tampering detected: {len(report['errors'])} error(s)")
    for e in report["errors"]:
        say(f"height {e['height']}  {e['code']:<22} {e['message']}")
    check(any(e["height"] == 2 for e in report["errors"]), "report pinpoints block 2")
    check(n1.get("/api/v1/chain/validate")["valid"], "the live node's chain is unaffected")

    # ------------------------------------------------------------------ 5
    banner(5, "Attack demos")
    nonce = n1.next_nonce(alice.address)
    r1 = n1.send(alice, bob.address, 800, 1, nonce)
    r2 = n1.send(alice, carol.address, 800, 1, nonce)
    check(r1.status_code == 202, "(a) first spend of alice's funds accepted")
    check(r2.status_code == 400 and r2.json()["error"]["code"] == "BAD_NONCE",
          f"(a) DOUBLE-SPEND rejected: {r2.json()['error']['code']} - {r2.json()['error']['message']}")
    r3 = n1.send(alice, carol.address, 800, 1)
    check(r3.status_code == 400 and r3.json()["error"]["code"] == "INSUFFICIENT_BALANCE",
          "(a) over-spend (balance already committed) rejected: INSUFFICIENT_BALANCE")
    replay = n1.post("/api/v1/transactions", tx1.to_dict())
    check(replay.status_code == 400 and replay.json()["error"]["code"] == "BAD_NONCE",
          f"(b) REPLAY of confirmed tx {short(tx1.tx_id)} rejected: {replay.json()['error']['code']}")
    wrong = n1.send(carol, bob.address, 5, 1, chain_id="some-other-chain")
    check(wrong.status_code == 400 and wrong.json()["error"]["code"] == "WRONG_CHAIN",
          f"(c) tx signed for another chain_id rejected: {wrong.json()['error']['code']}")
    good = carol.create_transaction(bob.address, 5, 1, n1.next_nonce(carol.address), CHAIN_ID)
    forged = good.to_dict()
    forged["signature"] = bob.sign_message(good.body_bytes())      # valid signature, but by the wrong key
    f = n1.post("/api/v1/transactions", forged)
    check(f.status_code == 400 and f.json()["error"]["code"] == "INVALID_SIGNATURE",
          f"(d) FORGED signature rejected: {f.json()['error']['code']}")
    stolen = dataclasses.replace(good, sender=alice.address)
    stolen = dataclasses.replace(stolen, tx_id=stolen.compute_id())
    s = n1.post("/api/v1/transactions", stolen.to_dict())
    check(s.status_code == 400 and s.json()["error"]["code"] == "BAD_OWNERSHIP",
          "(e) bonus: claiming alice's address with carol's key rejected: BAD_OWNERSHIP")
    n1.mine(miner.address)                                          # confirm alice's legit spend
    wait_until(lambda: converged(*nodes), msg="post-attack convergence")
    check(n3.balance(bob.address) == 500 - 51 + 800, "only the single legitimate spend took effect")

    # ------------------------------------------------------------------ 6
    banner(6, "Fork demo: partition, compete, reconnect, resolve to the heavier chain")
    fa = NodeProc("fa", alloc).start()
    fb = NodeProc("fb", alloc, peers=[fa.addr]).start()
    wait_until(lambda: len(fa.get("/api/v1/peers")["peers"]) == 1 and len(fb.get("/api/v1/peers")["peers"]) == 1,
               msg="fa/fb connected")
    ma, mb = Wallet.generate(), Wallet.generate()
    fa.mine(ma.address)
    wait_until(lambda: converged(fa, fb), msg="common ancestor")
    base = fa.info()
    say(f"common ancestor: height {base['height']} {short(base['tip_hash'])}")
    check(fa.post("/p2p/partition", {"addrs": [fb.addr]}).status_code == 200 and
          fb.post("/p2p/partition", {"addrs": [fa.addr]}).status_code == 200, "network partitioned: fa | fb")
    wait_until(lambda: not fa.get("/api/v1/peers")["peers"] and not fb.get("/api/v1/peers")["peers"], msg="peers dropped")
    pay = alice.create_transaction(carol.address, 40, 3, 0, CHAIN_ID)
    check(fa.post("/api/v1/transactions", pay.to_dict()).status_code == 202, "alice -> carol 40 submitted ONLY on fa")
    fa.mine(ma.address, 2)
    fb.mine(mb.address, 3)
    ia, ib = fa.info(), fb.info()
    check(ia["height"] == 3 and ib["height"] == 4 and ia["tip_hash"] != ib["tip_hash"],
          f"competing chains: fa height {ia['height']} vs fb height {ib['height']}")
    check(fa.balance(carol.address) == 40 and fb.balance(carol.address) == 0, "fa's state includes the payment; fb's does not")
    fa_old_tip = ia["tip_hash"]
    fa.post("/p2p/heal")
    fb.post("/p2p/heal")
    r = fa.post("/api/v1/peers", {"address": fb.addr})
    check(r.status_code == 200, "partition healed; fa reconnected to fb")
    wait_until(lambda: converged(fa, fb), msg="fork resolution")
    ia, ib = fa.info(), fb.info()
    check(ia["tip_hash"] == ib["tip_hash"] == fb.info()["tip_hash"] and ia["height"] == 4,
          f"fa adopted the heavier chain: tip {short(ia['tip_hash'])}, height {ia['height']}")
    check(ia["state_root"] == ib["state_root"], "state roots identical after resolution")
    check(fa.balance(carol.address) == 0, "STATE ROLLBACK: carol's 40 from the orphaned block is gone")
    check(fa.get("/api/v1/mempool")["count"] == 1 and fa.get("/api/v1/mempool")["transactions"][0]["tx_id"] == pay.tx_id,
          "ORPHANED TX returned to fa's mempool")
    forks = fa.get("/api/v1/chain/forks")["forks"]
    check(len(forks) == 1, "fork event recorded")
    ev = forks[0]
    say(f"fork event: ancestor height {ev['ancestor_height']}, depth {ev['depth']}, new branch {ev['new_branch_length']}, "
        f"old tip {short(ev['old_tip'])} -> new tip {short(ev['new_tip'])}")
    check(ev["ancestor_height"] == 1 and ev["depth"] == 2 and ev["old_tip"] == fa_old_tip, "event fields are correct")
    check(fb.get("/api/v1/chain/forks")["forks"] == [], "the winning side saw no reorg")
    fa.mine(ma.address)
    wait_until(lambda: converged(fa, fb) and fb.balance(carol.address) == 40, msg="returned tx mined")
    check(True, "the returned tx was mined into the winning chain; carol has 40 on both nodes")

    # ------------------------------------------------------------------ 7
    banner(7, "Restart a node: chain, balances and peers persist")
    before = fb.info()
    bal_before = fb.balance(carol.address)
    fb.stop()
    check(fb.proc is None, "fb process stopped")
    fb.start()
    after = fb.info()
    check((after["tip_hash"], after["height"], after["state_root"]) ==
          (before["tip_hash"], before["height"], before["state_root"]), f"same tip {short(after['tip_hash'])} after restart")
    check(fb.balance(carol.address) == bal_before, "balances identical after restart")
    wait_until(lambda: any(p["addr"] == fa.addr for p in fb.get("/api/v1/peers")["peers"]), msg="persisted peer reconnect")
    check(True, f"peer {fa.addr} was reloaded from the database")
    check(fb.get("/api/v1/chain/validate")["valid"], "restarted node's chain validates")

    print(f"\n{'=' * 78}\nDEMO COMPLETE: every assertion passed in {time.monotonic() - t0:.0f}s.\n{'=' * 78}")


if __name__ == "__main__":
    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted; cleaning up node processes...", file=sys.stderr)
        sys.exit(130)
    except BaseException as exc:
        print(f"\nDEMO FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        cleanup()
