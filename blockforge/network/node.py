"""A BlockForge node: chain + mempool + peers + gossip + sync + miner.

All protocol logic lives in plain methods (handle_*, connect_peer, ...) so it can be driven
by the HTTP layer in blockforge.api.routes or, in tests, by an in-process transport.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from blockforge.blockchain.block import Block
from blockforge.blockchain.blockchain import AddResult, Blockchain
from blockforge.blockchain.crypto import is_valid_address
from blockforge.blockchain.transaction import MalformedError, Transaction, ValidationError
from blockforge.config import Config
from blockforge.logging_setup import log_event, set_node_id
from blockforge.network.peer import (
    HttpPeerClient,
    PeerClient,
    PeerTable,
    RateLimiter,
    SeenSet,
)
from blockforge.network.protocol import (
    MAX_BATCH,
    PENALTY_BAD_BLOCK,
    PENALTY_BAD_HANDSHAKE,
    PENALTY_BAD_TX,
    PENALTY_MALFORMED,
    PROTOCOL_VERSION,
    SEEN_CAPACITY,
    ProtocolError,
    check_handshake,
    valid_peer_addr,
)
from blockforge.network.sync import Syncer
from blockforge.storage.database import Database

log = logging.getLogger("blockforge.node")

ClientFactory = Callable[[str], PeerClient]
_BAD_TX_CODES = {"INVALID_SIGNATURE", "BAD_OWNERSHIP", "MALFORMED", "WRONG_CHAIN", "BAD_AMOUNT"}


class Miner:
    """Background mining thread; interrupted whenever a new block arrives."""

    def __init__(self, node: "Node") -> None:
        self.node = node
        self.address: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._interrupt = threading.Event()
        self.blocks_mined = 0

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def start(self, address: str) -> None:
        if not is_valid_address(address):
            raise ValueError("invalid miner address")
        if self.running:
            self.address = address
            return
        self.address = address
        self._running.set()
        self._interrupt.clear()
        self._thread = threading.Thread(target=self._loop, name="miner", daemon=True)
        self._thread.start()
        log_event(log, "miner_started", address=address[:10])

    def stop(self) -> None:
        self._running.clear()
        self._interrupt.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self._thread = None
        log_event(log, "miner_stopped")

    def interrupt(self) -> None:
        """Abort the current nonce search (a new block arrived); the loop restarts on a fresh template."""
        if self.running:
            self._interrupt.set()

    def _loop(self) -> None:
        while self._running.is_set():
            try:
                block = self.node.chain.mine_block(self.address or "", self._interrupt)
            except Exception:
                log.exception("mining failed")
                time.sleep(0.5)
                continue
            if block is None:
                self._interrupt.clear()
                continue
            res = self.node.chain.add_block(block)
            if res.status == "accepted":
                self.blocks_mined += 1
            else:
                time.sleep(0.05)   # stale (someone else won): loop and rebuild on the new tip


class Node:
    def __init__(self, cfg: Config, chain: Optional[Blockchain] = None,
                 client_factory: Optional[ClientFactory] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.clock = clock
        self.db: Optional[Database] = None
        if chain is None:
            self.db = Database(cfg.db_path)
            chain = Blockchain(cfg, self.db)
        else:
            self.db = chain.db
        self.chain = chain
        stored = self.db.get_meta("node_id") if self.db else None
        self.node_id = stored or secrets.token_hex(6)
        if self.db and not stored:
            self.db.set_meta("node_id", self.node_id)
        set_node_id(self.node_id)
        self.addr = cfg.advertised_addr
        self.started_at = int(clock())
        self.peers = PeerTable(self.db, clock)
        self.seen = SeenSet(SEEN_CAPACITY)
        self.limiter = RateLimiter(cfg.rate_limit_per_sec)
        self._factory: ClientFactory = client_factory or (lambda a: HttpPeerClient(a))
        self.syncer = Syncer(chain, self._penalize)
        self.miner = Miner(self)
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gossip")
        self._stop = threading.Event()
        self._health: Optional[threading.Thread] = None
        self._clients: dict[str, PeerClient] = {}
        self._clients_lock = threading.Lock()
        self._partitioned: set[str] = set()   # test/demo hook: addrs we refuse to talk to
        chain.listeners.append(self._on_block)

    # ================================================================= lifecycle
    def start(self) -> None:
        log_event(log, "node_started", addr=self.addr, height=self.chain.height(),
                  tip=self.chain.tip_hash()[:12], chain_id=self.cfg.chain_id)
        seeds = list(dict.fromkeys(self.cfg.peers + [p["addr"] for p in
                                                        (self.db.load_peers() if self.db else [])]))
        for addr in seeds:
            self._pool.submit(self.connect_peer, addr)
        self._health = threading.Thread(target=self._health_loop, name="health", daemon=True)
        self._health.start()
        if self.cfg.miner_address:
            self.miner.start(self.cfg.miner_address)

    def stop(self) -> None:
        self._stop.set()
        self.miner.stop()
        if self._health is not None:
            self._health.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)
        with self._clients_lock:
            for client in self._clients.values():
                close = getattr(client, "close", None)
                if close is not None:
                    close()
            self._clients.clear()
        if self.db is not None:
            self.db.close()
        log_event(log, "node_stopped")

    # ================================================================= identity
    def handshake_info(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "chain_id": self.cfg.chain_id,
            "genesis_hash": self.chain.genesis.hash, "tip_height": self.chain.height(),
            "cumulative_work": str(self.chain.cumulative_work()), "addr": self.addr,
            "version": PROTOCOL_VERSION,
        }

    def status(self) -> dict[str, Any]:
        info = self.handshake_info()
        info["tip_hash"] = self.chain.tip_hash()
        return info

    # ================================================================= peers
    def _client(self, addr: str) -> PeerClient:
        """One cached client per peer (httpx clients are thread-safe and pool connections)."""
        with self._clients_lock:
            client = self._clients.get(addr)
            if client is None:
                client = self._clients[addr] = self._factory(addr)
            return client

    def _penalize(self, addr: str, points: int) -> None:
        if self.peers.penalize(addr, points):
            log_event(log, "peer_banned", logging.WARNING, peer=addr)

    def _validate_remote(self, info: Any) -> dict[str, Any]:
        return check_handshake(info, chain_id=self.cfg.chain_id, genesis_hash=self.chain.genesis.hash)

    def connect_peer(self, addr: str) -> bool:
        """Outbound handshake + peer-list exchange + sync if the peer is ahead."""
        if addr == self.addr or not valid_peer_addr(addr) or addr in self._partitioned:
            return False
        if self.peers.is_banned(addr):
            return False
        try:
            client = self._client(addr)
            remote = self._validate_remote(client.handshake(self.handshake_info()))
        except ProtocolError as exc:
            log_event(log, "peer_rejected", logging.WARNING, peer=addr, code=exc.code, reason=exc.message)
            self._penalize(addr, PENALTY_BAD_HANDSHAKE)
            return False
        except Exception as exc:
            log_event(log, "peer_connect_failed", logging.INFO, peer=addr, error=type(exc).__name__)
            return False
        peer = self.peers.add(addr, remote["node_id"], remote["tip_height"], int(remote["cumulative_work"]))
        if peer is None:
            return False
        log_event(log, "peer_connected", peer=addr, peer_id=remote["node_id"], peer_height=remote["tip_height"])
        try:
            for other in client.peers()[:32]:
                if (valid_peer_addr(other) and other != self.addr and self.peers.get(other) is None
                        and not self.peers.is_banned(other)):
                    self._pool.submit(self.connect_peer, other)
        except Exception as exc:   # peer-list exchange is best effort
            log_event(log, "peer_exchange_failed", logging.DEBUG, peer=addr, error=type(exc).__name__)
        if int(remote["cumulative_work"]) > self.chain.cumulative_work():
            self.sync_from(addr, client)
        return True

    def disconnect_peer(self, addr: str, reason: str = "requested") -> None:
        if self.peers.remove(addr):
            log_event(log, "peer_disconnected", peer=addr, reason=reason)

    def handle_handshake(self, info: Any) -> dict[str, Any]:
        """Inbound handshake. Returns our info or raises ProtocolError."""
        remote = self._validate_remote(info)          # raises on genesis/chain mismatch
        addr = remote["addr"]
        if addr in self._partitioned:
            raise ProtocolError("PARTITIONED", "node is partitioned from this peer")
        if self.peers.is_banned(addr):
            raise ProtocolError("BANNED", "peer is temporarily banned")
        peer = self.peers.add(addr, remote["node_id"], remote["tip_height"], int(remote["cumulative_work"]))
        if peer is not None:
            log_event(log, "peer_connected", peer=addr, peer_id=remote["node_id"], inbound=True)
            if int(remote["cumulative_work"]) > self.chain.cumulative_work():
                self._pool.submit(self.sync_from, addr)
        return self.handshake_info()

    def peer_list(self) -> list[str]:
        return self.peers.addrs()

    def partition(self, addrs: list[str]) -> None:
        """Demo/test hook: drop and refuse these peers (simulates a network split)."""
        self._partitioned.update(addrs)
        for a in addrs:
            self.disconnect_peer(a, "partition")

    def heal(self) -> None:
        self._partitioned.clear()

    def is_partitioned(self, addr: str) -> bool:
        return addr in self._partitioned

    # ================================================================= gossip
    def _broadcast(self, kind: str, payload: dict[str, Any], exclude: Optional[str] = None) -> None:
        for addr in self.peers.addrs():
            if addr != exclude and addr not in self._partitioned:
                self._pool.submit(self._send, addr, kind, payload)

    def _send(self, addr: str, kind: str, payload: dict[str, Any]) -> None:
        try:
            client = self._client(addr)
            if kind == "tx":
                client.send_tx(payload, self.addr)
            else:
                client.send_block(payload, self.addr)
        except Exception as exc:
            n = self.peers.record_failure(addr)
            log_event(log, "gossip_failed", logging.DEBUG, peer=addr, kind=kind,
                      error=type(exc).__name__, failures=n)

    def _on_block(self, block: Block, result: AddResult) -> None:
        """Chain listener: a new block (ours or a peer's) was accepted."""
        self.seen.add(block.hash)
        self.miner.interrupt()
        self._broadcast("block", block.to_dict())

    def submit_transaction(self, tx: Transaction) -> Optional[ValidationError]:
        """Local submission (API/CLI): validate, queue, gossip."""
        err = self.chain.submit_transaction(tx)
        if err is None:
            self.seen.add(tx.tx_id)
            self._broadcast("tx", tx.to_dict())
        return err

    def handle_tx(self, tx_dict: Any, sender: Optional[str]) -> dict[str, Any]:
        try:
            tx = Transaction.from_dict(tx_dict)
        except (MalformedError, TypeError, ValueError) as exc:
            if sender:
                self._penalize(sender, PENALTY_MALFORMED)
            raise ProtocolError("MALFORMED", str(exc)) from exc
        if not self.seen.add(tx.tx_id):
            return {"status": "seen"}
        err = self.chain.submit_transaction(tx)
        if err is None:
            self._broadcast("tx", tx.to_dict(), exclude=sender)
            return {"status": "accepted"}
        if sender and err.code in _BAD_TX_CODES:
            self._penalize(sender, PENALTY_BAD_TX)
        return {"status": "rejected", "error": err.to_dict()}

    def handle_block(self, block_dict: Any, sender: Optional[str]) -> dict[str, Any]:
        try:
            block = Block.from_dict(block_dict)
        except (MalformedError, TypeError, ValueError) as exc:
            if sender:
                self._penalize(sender, PENALTY_MALFORMED)
            raise ProtocolError("MALFORMED", str(exc)) from exc
        if block.hash in self.seen and self.chain.has_block(block.hash):
            return {"status": "seen"}
        self.seen.add(block.hash)
        res = self.chain.add_block(block)
        if res.status == "rejected":
            if sender:
                self._penalize(sender, PENALTY_BAD_BLOCK)
            return {"status": "rejected", "errors": [e.to_dict() for e in res.errors]}
        if res.status == "orphan" and sender:
            self._pool.submit(self._resolve_orphan, sender, block)
        return {"status": res.status}

    def _resolve_orphan(self, sender: str, block: Block) -> None:
        try:
            client = self._client(sender)
            if not self.syncer.fetch_ancestors(sender, client, block):
                self.syncer.sync_from(sender, client)
        except Exception as exc:
            log_event(log, "orphan_resolution_failed", logging.INFO, peer=sender, error=type(exc).__name__)

    def serve_blocks(self, start: int, limit: int) -> list[dict[str, Any]]:
        limit = max(0, min(limit, MAX_BATCH))
        return [b.to_dict() for b in self.chain.main_blocks(max(0, start), limit)]

    def serve_block(self, block_hash: str) -> Optional[dict[str, Any]]:
        blk = self.chain.blocks.get(block_hash)
        return blk.to_dict() if blk is not None else None

    # ================================================================= sync / health
    def sync_from(self, addr: str, client: Optional[PeerClient] = None) -> int:
        if addr in self._partitioned:
            return 0
        try:
            return self.syncer.sync_from(addr, client or self._client(addr))
        except Exception as exc:
            log_event(log, "sync_failed", logging.WARNING, peer=addr, error=type(exc).__name__)
            self.peers.record_failure(addr)
            return 0

    def check_peers(self) -> None:
        """One health-check pass: refresh status, prune dead peers, sync from stronger ones."""
        for peer in self.peers.all():
            if peer.addr in self._partitioned:
                continue
            try:
                client = self._client(peer.addr)
                st = client.status()
                if st.get("genesis_hash") != self.chain.genesis.hash:
                    raise ProtocolError("GENESIS_MISMATCH", "peer changed genesis")
                self.peers.add(peer.addr, st.get("node_id"), int(st["tip_height"]),
                               int(st["cumulative_work"]))
                if int(st["cumulative_work"]) > self.chain.cumulative_work():
                    self.sync_from(peer.addr, client)
            except Exception as exc:
                failures = self.peers.record_failure(peer.addr)
                if failures >= 3 or isinstance(exc, ProtocolError):
                    self.disconnect_peer(peer.addr, f"unhealthy ({type(exc).__name__})")
        for addr in self.cfg.peers:      # keep trying configured seeds
            if self.peers.get(addr) is None and addr not in self._partitioned:
                self.connect_peer(addr)

    def _health_loop(self) -> None:
        while not self._stop.wait(self.cfg.health_check_interval):
            try:
                self.check_peers()
            except Exception:
                log.exception("health check failed")

    # ================================================================= mining (API)
    def mine_once(self, address: str) -> Block:
        """Synchronously mine one block to `address` and add it to the chain."""
        if not is_valid_address(address):
            raise ValueError("invalid address")
        block = self.chain.mine_block(address)
        assert block is not None
        res = self.chain.add_block(block)
        if res.status != "accepted":
            raise RuntimeError(f"mined block was not accepted ({res.status}); the tip moved, retry")
        return block

    def info(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "addr": self.addr, "chain_id": self.cfg.chain_id,
            "height": self.chain.height(), "tip_hash": self.chain.tip_hash(),
            "mining": self.miner.running, "miner_address": self.miner.address,
            "peer_count": len(self.peers.all()), "uptime_seconds": int(self.clock()) - self.started_at,
            "version": PROTOCOL_VERSION,
        }
