"""Shared test helpers: low difficulty config, wallets, quick mining."""
from __future__ import annotations

import socket
import time
from typing import Callable

from blockforge.blockchain.blockchain import Blockchain
from blockforge.blockchain.wallet import Wallet
from blockforge.config import Config
from blockforge.storage.database import Database

TEST_BITS = 6


def make_cfg(tmp_path=None, **kw) -> Config:
    kw.setdefault("difficulty_bits", TEST_BITS)
    kw.setdefault("chain_id", "test-chain")
    if tmp_path is not None:
        kw.setdefault("data_dir", str(tmp_path))
    return Config(**kw).validate()


def new_chain(cfg: Config, path=None) -> Blockchain:
    return Blockchain(cfg, Database(path) if path is not None else None)


def mine(chain: Blockchain, miner: str):
    """Mine one block on top of chain's tip and add it. Returns the block."""
    blk = chain.mine_block(miner)
    assert blk is not None
    res = chain.add_block(blk)
    assert res.status == "accepted", (res.status, [e.to_dict() for e in res.errors])
    return blk


def mine_unadded(chain: Blockchain, miner: str):
    blk = chain.mine_block(miner)
    assert blk is not None
    return blk


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(cond: Callable[[], bool], timeout: float = 20.0, interval: float = 0.05,
               msg: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {msg}")


def funded(n: int = 2):
    """Return n fresh wallets."""
    return [Wallet.generate() for _ in range(n)]


# ------------------------------------------------------------ in-process transport
class LocalNetwork:
    """Routes PeerClient calls straight into other Node objects (no sockets).

    Used to test protocol logic deterministically; test_network.py covers real sockets.
    """

    def __init__(self) -> None:
        self.nodes: dict = {}

    def add(self, node) -> None:
        self.nodes[node.addr] = node
        node._factory = lambda addr: LocalClient(self, addr)


class LocalClient:
    def __init__(self, net: LocalNetwork, addr: str) -> None:
        self.net, self.addr = net, addr

    def _node(self):
        node = self.net.nodes.get(self.addr)
        if node is None:
            raise ConnectionError(f"no node at {self.addr}")
        return node

    def handshake(self, info):
        from blockforge.network.protocol import ProtocolError
        try:
            return self._node().handle_handshake(info)
        except ProtocolError as exc:   # over HTTP this is a 4xx -> generic client error
            raise RuntimeError(f"HTTP 400 {exc.code}") from exc

    def peers(self):
        return self._node().peer_list()

    def status(self):
        return self._node().status()

    def send_tx(self, tx, sender):
        return self._node().handle_tx(tx, sender)

    def send_block(self, block, sender):
        return self._node().handle_block(block, sender)

    def blocks(self, start, limit):
        return self._node().serve_blocks(start, limit)

    def block(self, block_hash):
        b = self._node().serve_block(block_hash)
        if b is None:
            raise RuntimeError("HTTP 404")
        return b


def make_node(net: LocalNetwork, tmp_path, port: int, **kw):
    from blockforge.network.node import Node
    cfg = make_cfg(tmp_path, port=port, health_check_interval=3600, **kw)
    node = Node(cfg)
    net.add(node)
    return node


def stop_nodes(*nodes) -> None:
    for n in nodes:
        n.stop()
